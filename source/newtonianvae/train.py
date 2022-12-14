import os
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Union

import numpy as np
import torch
from torch import nn, optim

import models.core
import mypython.ai.torchprob as tp
import tool.util
from models.core import NewtonianVAEFamily
from mypython.ai.util import SequenceDataLoader, print_module_params, reproduce
from mypython.pyutil import RemainingTime, s2dhms_str
from mypython.terminal import Color, Prompt
from tool import paramsmanager
from tool.util import Preferences, creator, dtype_device
from view.visualhandlerbase import VisualHandlerBase


def train(
    config: str,
    resume: bool,
    vh=VisualHandlerBase(),
):
    torch.set_grad_enabled(True)

    params = paramsmanager.Params(config)

    if params.train.seed is None:
        params.train.seed = np.random.randint(0, 2**16)
    reproduce(params.train.seed)

    dtype, device = dtype_device(
        dtype=params.train.dtype,
        device=params.train.device,
    )

    trainloader = SequenceDataLoader(
        root=Path(params.path.data_dir, "episodes"),
        names=["action", "observation", "delta"],
        start=params.train.data_start,
        stop=params.train.data_stop,
        batch_size=params.train.batch_size,
        dtype=dtype,
        device=device,
    )

    model, managed_dir, weight_dir, resume_weight_path = creator(
        root=params.path.saves_dir,
        model_place=models.core,
        model_name=params.model,
        model_params=params.model_params,
        resume=resume,
    )
    model: NewtonianVAEFamily
    model.type(dtype)
    model.to(device)
    model.train()
    # print_module_params(model)
    optimizer = optim.Adam(model.parameters(), params.train.learning_rate)

    params.path.resume_weight = resume_weight_path
    params.pid = os.getpid()
    params.save(Path(managed_dir, "params_saved.json5"))

    vh.title = managed_dir.stem
    vh.call_end_init()

    record_Loss = []
    record_NLL = []
    record_KL = []

    Preferences.put(managed_dir, "running", True)

    def end_process():
        Preferences.remove(managed_dir, "running")

        if len(list(weight_dir.glob("*"))) > 0:
            np.save(Path(managed_dir, "LOG_Loss.npy"), record_Loss)
            np.save(Path(managed_dir, "LOG_NLL.npy"), record_NLL)
            np.save(Path(managed_dir, "LOG_KL.npy"), record_KL)
        else:
            shutil.rmtree(managed_dir)

        print("\nEnd of train")

    try:
        tp.config.check_value = params.train.check_value  # if False, A little bit faster
        remaining = RemainingTime(max=params.train.epochs * len(trainloader))
        for epoch in range(1, params.train.epochs + 1):

            if params.train.kl_annealing:
                # Paper:
                # In the point mass experiments
                # we found it useful to anneal the KL term in the ELBO,
                # starting with a value of 0.001 and increasing it linearly
                # to 1.0 between epochs 30 and 60.
                if epoch < 30:
                    model.cell.kl_beta = 0.001
                elif 30 <= epoch and epoch <= 60:
                    model.cell.kl_beta = 0.001 + (1 - 0.001) * ((epoch - 30) / (60 - 30))
                else:
                    model.cell.kl_beta = 1

            for action, observation, delta in trainloader:
                delta.unsqueeze_(-1)
                # print(action.shape)
                # print(observation.shape)
                # print(delta.shape)

                E, E_ll, E_kl = model(action=action, observation=observation, delta=delta)

                L = -E
                optimizer.zero_grad()
                L.backward()
                # print_module_params(model, True)

                if params.train.grad_clip_norm is not None:
                    nn.utils.clip_grad_norm_(
                        model.parameters(), params.train.grad_clip_norm, norm_type=2
                    )

                optimizer.step()

                # === show progress ===

                L = L.cpu().item()
                E_ll = -E_ll.cpu().item()
                E_kl = E_kl.cpu().item()

                record_Loss.append(L)
                record_NLL.append(E_ll)
                record_KL.append(E_kl)

                vh.plot(L, E_ll, E_kl, epoch)
                if not vh.is_running:
                    vh.call_end()
                    end_process()
                    return

                remaining.update()
                Prompt.print_one_line(
                    (
                        f"Epoch: {epoch} | "
                        f"Loss: {L:.4f} | "
                        f"NLL: {E_ll:.4f} | "
                        f"KL: {E_kl:.4f} | "
                        f"Elapsed: {s2dhms_str(remaining.elapsed)} | "
                        f"Remaining: {s2dhms_str(remaining.time)} | "
                        f"ETA: {remaining.eta} "
                    )
                )

            if epoch % params.train.save_per_epoch == 0:
                torch.save(model.state_dict(), Path(weight_dir, f"{epoch}.pth"))
                print("saved")

    except KeyboardInterrupt:
        print("\nKeyboardInterrupt")
    except:
        print("=== traceback ===")
        print(traceback.format_exc())

    end_process()
