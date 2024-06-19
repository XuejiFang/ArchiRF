import torch
from torchvision.utils import make_grid, save_image
from accelerate import Accelerator
from diffusers.models import AutoencoderKL

from tqdm.auto import tqdm
from contextlib import contextmanager
from ema import LitEma
from calc_fid_score import FIDEvaluation

import os
from loguru import logger
import moviepy.editor as mpy
from pathlib import Path
import datetime
import wandb


def num_to_groups(num, divisor):
    groups = num // divisor
    remainder = num % divisor
    arr = [divisor] * groups
    if remainder > 0:
        arr.append(remainder)
    return arr


class Trainer:
    def __init__(
        self,
        config,
        accelerator: Accelerator,
        model,
        optimizer,
        dl,
        lr_scheduler=None,
        # hyperparameters
        batch_size=1,
        grad_accumulate_every=1,
        max_grad_norm=torch.inf,
        n_steps=1,
        use_ema=True,
        ema_decay=0.9999,
        # misc
        log_dir=None,
        sample_batch_size=1,
        sampling_steps=100,
        save_and_sample_interval=1000,
        n_per_class=10,
        fid_eval_interval=10000,
        fid_stats_dir="./results",
        fid_cfg_scale=1.0,
        num_fid_samples=50000,
        logger_kwargs={},
        **kwargs,
    ):
        self.config = config
        self.accelerator = accelerator
        self.device = accelerator.device
        model.device = self.device
        self.model, self.opt, self.dl = accelerator.prepare(model, optimizer, dl)
        self.lr_scheduler = lr_scheduler

        self.batch_size = batch_size
        self.grad_accumulate_every = grad_accumulate_every
        self.max_grad_norm = max_grad_norm
        self.n_steps = n_steps

        self.sample_batch_size = sample_batch_size
        self.sampling_steps = sampling_steps
        self.save_and_sample_interval = save_and_sample_interval
        self.n_per_class = n_per_class

        self.use_ema = use_ema
        if self.use_ema:
            self.model_ema = LitEma(self.model, decay=ema_decay)
        
        self.fid_eval_interval = fid_eval_interval
        self.num_fid_samples = num_fid_samples
        self.fid_cfg_scale = fid_cfg_scale
        self.fid_scorer = FIDEvaluation(
            self.sample_batch_size,
            self.dl,
            self.model,
            self.model.channels,
            stats_dir=fid_stats_dir,
            device=self.device,
            num_fid_samples=self.num_fid_samples,
        )

        self.logger_kwargs = logger_kwargs
        self.step = 0

        current_datetime = datetime.datetime.now()
        date_string = current_datetime.strftime("%Y-%m-%d")
        time_string = current_datetime.strftime("%H-%M-%S")
        self.log_dir = Path(f"{log_dir}/{date_string}/{time_string}")
        if not self.log_dir.exists():
            self.log_dir.mkdir(parents=True, exist_ok=True)
        self.ckpt_paths = []

    @contextmanager
    def ema_scope(self):
        if self.use_ema:
            self.model_ema.store(self.model.parameters())
            self.model_ema.copy_to(self.model)
        try:
            yield None
        finally:
            if self.use_ema:
                self.model_ema.restore(self.model.parameters())

    def save_ckpt(self):
        state_dict = {
            "model": self.accelerator.get_state_dict(self.model),
            "opt": self.opt.state_dict(),
            "step": self.step,
        }
        if self.accelerator.scaler is not None:
            state_dict["scaler"] = self.accelerator.scaler.state_dict()
        if self.use_ema:
            state_dict["model_ema"] = self.model_ema.state_dict()

        ckpt_path = self.log_dir / f"model-step{self.step}.ckpt"
        torch.save(state_dict, ckpt_path)
        logger.info(f"Saved checkpoint at {ckpt_path}")
        self.ckpt_paths.append(ckpt_path)

        # Remove old checkpoints if more than 3 exist
        if len(self.ckpt_paths) > 3:
            os.remove(self.ckpt_paths.pop(0))

    def load_ckpt(self, ckpt_path):
        accelerator = self.accelerator
        device = accelerator.device

        logger.info(f"Loading checkpoint from {ckpt_path}")
        state_dict = torch.load(ckpt_path, map_location=device)

        model = accelerator.unwrap_model(self.model)
        model.load_state_dict(state_dict["model"])

        self.opt.load_state_dict(state_dict["opt"])
        self.step = state_dict["step"]
        if accelerator.scalar is not None:
            accelerator.scaler.load_state_dict(state_dict["scalar"])
        if self.use_ema:
            self.model_ema.load_state_dict(state_dict["model_ema"])

    def train(self):
        self.accelerator.init_trackers(**self.logger_kwargs, config=self.config)

        with tqdm(range(self.n_steps), desc="Training", dynamic_ncols=True) as pbar:
            pbar.update(self.step)

            for _ in pbar:
                self.opt.zero_grad()

                total_loss = 0.0
                for _ in range(self.grad_accumulate_every):
                    data = next(self.dl)
                    x, c = data
                    x, c = x.to(self.device), c.to(self.device)

                    with self.accelerator.autocast():
                        loss = self.model(x, c)
                        loss = loss / self.grad_accumulate_every
                        total_loss += loss.item()

                    self.accelerator.backward(loss)

                self.accelerator.clip_grad_norm_(
                    self.model.parameters(), self.max_grad_norm
                )
                self.opt.step()
                if self.use_ema:
                    self.model_ema(self.model)

                self.step += 1
                
                lr = self.opt.param_groups[0]["lr"]
                self.accelerator.log({"loss": total_loss, "step": self.step, "lr": lr}, self.step)
                pbar.set_postfix({"loss": total_loss, "lr": lr})
                
                if self.lr_scheduler is not None:
                    self.lr_scheduler.step()

                if self.step % self.save_and_sample_interval == 0:
                    self.model.eval()
                    with self.accelerator.autocast():
                        self.log_samples()
                    self.save_ckpt()
                    self.model.train()

                if self.step % self.fid_eval_interval == 0:
                    self.model.eval()
                    with self.accelerator.autocast():
                        with self.ema_scope():
                            fid_score = self.fid_scorer.fid_score(self.fid_cfg_scale)
                    self.model.train()
                    
                    logger.info(f"FID score at step {self.step}: {fid_score}")
                    self.accelerator.log({"FID": fid_score}, self.step)

        self.accelerator.end_training()

    def log_samples(self):
        with self.ema_scope():
            if self.model.use_cond:
                logging_images = []
                logging_animations = []
                for cfg_scale in [1.0, 1.25, 1.5]:
                    nrow = self.model.net.num_classes
                    y = torch.arange(0, self.model.net.num_classes).repeat(
                        self.n_per_class
                    )
                    batch_y = y.split(self.sample_batch_size)
                    samples = []
                    samples_each_step = []

                    for _y in batch_y:
                        _samples, _samples_each_step = self.model.cond_sample(
                            _y,
                            self.device,
                            sampling_steps=self.sampling_steps,
                            cfg_scale=cfg_scale,
                            return_all_steps=True,
                        )
                        
                        samples.append(_samples)
                        samples_each_step.append(_samples_each_step)

                    samples = torch.cat(samples)
                    samples = make_grid(samples, nrow=nrow)
                    save_image(
                        samples,
                        f"{self.log_dir}/samples_{self.step}_cfg{cfg_scale}.png",
                    )
                    logger.info(
                        f"Saved samples at {self.log_dir}/samples_{self.step}_cfg{cfg_scale}.png"
                    )

                    samples_each_step = torch.cat(samples_each_step)
                    frames = [
                        make_grid(s, nrow=nrow).permute(1, 2, 0).cpu().numpy() * 255
                        for s in samples_each_step
                    ]
                    clip = mpy.ImageSequenceClip(frames, fps=10)
                    clip.write_gif(
                        f"{self.log_dir}/samples_{self.step}_cfg{cfg_scale}.gif",
                        fps=len(frames) // 5,
                    )

                    if self.accelerator.get_tracker("wandb") is not None:
                        logging_images.append(
                            wandb.Image(
                                f"{self.log_dir}/samples_{self.step}_cfg{cfg_scale}.png",
                                caption=f"cfg_scale={cfg_scale}",
                            )
                        )
                        logging_animations.append(
                            wandb.Video(
                                f"{self.log_dir}/samples_{self.step}_cfg{cfg_scale}.gif",
                                caption=f"cfg_scale={cfg_scale}",
                            )
                        )
                if self.accelerator.get_tracker("wandb") is not None:
                    self.accelerator.log(
                        {
                            "samples/images": logging_images,
                            "samples/animations": logging_animations,
                        },
                        self.step,
                    )

            else:
                batch_batch_size = num_to_groups(100, self.sample_batch_size)
                samples = []
                samples_each_step = []

                for _b in batch_batch_size:
                    _samples, _samples_each_step = self.model.sample(
                        _b,
                        self.device,
                        sampling_steps=self.sampling_steps,
                        return_all_steps=True,
                    )
                    samples.append(_samples)
                    samples_each_step.append(_samples_each_step)

                samples = torch.cat(samples)
                samples_each_step = torch.cat(samples_each_step)
                samples = make_grid(samples, nrow=10)
                save_image(samples, f"{self.log_dir}/samples_{self.step}.png")
                logger.info(f"Saved samples at {self.log_dir}/samples_{self.step}.png")

                frames = [
                    make_grid(s, nrow=10).permute(1, 2, 0).cpu().numpy() * 255
                    for s in samples_each_step
                ]
                clip = mpy.ImageSequenceClip(frames, fps=10)
                clip.write_gif(f"{self.log_dir}/samples_{self.step}.gif", fps=10)

                if self.accelerator.get_tracker("wandb") is not None:
                    self.accelerator.log(
                        {
                            "samples/images": wandb.Image(
                                f"{self.log_dir}/samples_{self.step}.png"
                            ),
                            "samples/animations": wandb.Video(
                                f"{self.log_dir}/samples_{self.step}.gif"
                            ),
                        },
                        self.step,
                    )