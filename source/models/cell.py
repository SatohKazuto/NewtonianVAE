import dataclasses
from numbers import Real
from typing import Optional, Tuple, Union

import numpy as np
import torch
import torch.nn.functional as F
from torch import NumberType, Tensor, nn

import mypython.ai.torchprob as tp
import mypython.ai.torchprob.debug as tp_debug
from mypython.ai.util import find_function, swap01
from mypython.terminal import Color

from .component import Decoder, Encoder, Pxhat, Transition, Velocity


class NewtonianVAECellBase(nn.Module):
    """
    Classes that inherit from this class must not store variables from the previous time internally,
    as well as
    `RNNCell <https://pytorch.org/docs/stable/generated/torch.nn.RNNCell.html>`_,
    `LSTMCell <https://pytorch.org/docs/stable/generated/torch.nn.LSTMCell.html>`_,
    and `GRUCell <https://pytorch.org/docs/stable/generated/torch.nn.GRUCell.html>`_.
    """

    def __init__(
        self,
        regularization: bool,
        velocity: dict,
        transition: dict,
        encoder: dict,
    ):
        super().__init__()

        self.regularization = regularization
        self.kl_beta = 1
        self.force_training = False  # for add_graph of tensorboard

        self.f_velocity = Velocity(**velocity)
        self.p_transition = Transition(**transition)
        self.q_encoder = Encoder(**encoder)
        self.dim_x = self.q_encoder.dim_x

    @staticmethod
    def img_reduction(x: Tensor):
        # dim=0 : batch axis
        return x.sum(dim=(1, 2, 3)).mean(dim=0)

    @staticmethod
    def vec_reduction(x: Tensor):
        return x.sum(dim=1).mean(dim=0)

    @dataclasses.dataclass
    class Pack:
        E: Tensor  # Use for training
        E_ll: Tensor
        E_kl: Tensor
        x_t: Tensor  # Use for training
        v_t: Tensor  # Use for training

    def __call__(self, *args, **kwargs) -> Pack:
        return super().__call__(*args, **kwargs)


class NewtonianVAECell(NewtonianVAECellBase):
    """
    Eq (11)
    """

    def __init__(self, decoder: dict, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # p(I_t | x_t)
        self.p_decoder = Decoder(**decoder)

    def forward(self, I_t: Tensor, x_tn1: Tensor, u_tn1: Tensor, v_tn1: Tensor, dt: Tensor):
        """"""

        if self.training or self.force_training:
            v_t = self.f_velocity(x_tn1, u_tn1, v_tn1, dt)
            x_t = self.p_transition.given(x_tn1, v_t, dt).rsample()
            E_ll = self.img_reduction(tp.log(self.p_decoder, I_t).given(x_t))
            E_kl = self.vec_reduction(tp.KLdiv(self.q_encoder.given(I_t), self.p_transition))
            E = E_ll - self.kl_beta * E_kl

            if self.regularization:
                E -= self.vec_reduction(tp.KLdiv(self.q_encoder, tp.Normal01))

            return super().Pack(E=E, E_ll=E_ll.detach(), E_kl=E_kl.detach(), x_t=x_t, v_t=v_t)

        else:
            x_t = self.q_encoder.given(I_t).rsample()
            self.p_decoder.given(x_t)  # for cell.p_decoder.decode()
            v_t = (x_t - x_tn1) / dt  # for only visualize
            return super().Pack(E=0, E_ll=0, E_kl=0, x_t=x_t, v_t=v_t)


class NewtonianVAEDerivationCell(NewtonianVAECellBase):
    """
    Eq (23)
    """

    def __init__(self, decoder: dict, pxhat: dict, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # p(I_t | xhat_t)
        self.p_decoder = Decoder(**decoder)
        self.dim_xhat = self.p_decoder.dim_x

        # p(xhat_t | x_{t-1}, u_{t-1})
        self.p_xhat = Pxhat(**pxhat)

    def forward(self, I_t: Tensor, x_tn1: Tensor, u_tn1: Tensor, v_tn1: Tensor, dt: Tensor):
        """"""

        if self.training or self.force_training:
            v_t = self.f_velocity(x_tn1, u_tn1, v_tn1, dt)
            xhat_t = self.p_xhat.given(x_tn1, u_tn1).rsample()
            E_ll = self.img_reduction(tp.log(self.p_decoder, I_t).given(xhat_t))
            E_kl = self.vec_reduction(
                tp.KLdiv(self.q_encoder.given(I_t), self.p_transition.given(x_tn1, v_t, dt))
            )
            E = E_ll - self.kl_beta * E_kl

            if self.regularization:
                E -= self.vec_reduction(tp.KLdiv(self.q_encoder, tp.Normal01))

            x_t = self.q_encoder.rsample()

            return super().Pack(
                E=E,
                E_ll=E_ll.detach(),
                E_kl=E_kl.detach(),
                x_t=x_t,
                v_t=v_t,
            )

        else:
            x_t = self.q_encoder.given(I_t).rsample()
            xhat_t = self.p_xhat.given(x_tn1, u_tn1).rsample()
            self.p_decoder.given(xhat_t)  # for cell.p_decoder.decode()
            v_t = (x_t - x_tn1) / dt  # for only visualize
            return super().Pack(E=0, E_ll=0, E_kl=0, x_t=x_t, v_t=v_t)


class NewtonianVAEV2CellBase(NewtonianVAECellBase):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    @dataclasses.dataclass
    class Pack:
        E: Tensor  # Use for training
        E_ll: Tensor
        E_kl: Tensor
        x_q_t: Tensor  # Use for training
        v_t: Tensor

    def __call__(self, *args, **kwargs) -> Pack:
        return super().__call__(*args, **kwargs)


class NewtonianVAEV2Cell(NewtonianVAEV2CellBase):
    def __init__(self, decoder: dict, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.p_decoder = Decoder(**decoder)

    def forward(self, I_t: Tensor, x_q_tn1: Tensor, x_q_tn2: Tensor, u_tn1: Tensor, dt: Tensor):
        """"""
        v_tn1 = (x_q_tn1 - x_q_tn2) / dt
        v_t = self.f_velocity(x_q_tn1, u_tn1, v_tn1, dt)
        x_p_t = self.p_transition.given(x_q_tn1, v_t, dt).rsample()
        E_ll = self.img_reduction(tp.log(self.p_decoder, I_t).given(x_p_t))
        E_kl = self.vec_reduction(tp.KLdiv(self.q_encoder.given(I_t), self.p_transition))
        E = E_ll - self.kl_beta * E_kl

        if self.regularization:
            E -= self.vec_reduction(tp.KLdiv(self.q_encoder, tp.Normal01))

        x_q_t = self.q_encoder.rsample()

        return super().Pack(
            E=E,
            x_q_t=x_q_t,
            E_ll=E_ll.detach(),
            E_kl=E_kl.detach(),
            v_t=v_t.detach(),
        )


class NewtonianVAEV2DerivationCell(NewtonianVAEV2CellBase):
    """
    Eq (23)
    """

    def __init__(self, decoder: dict, pxhat: dict, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # p(I_t | xhat_t)
        self.p_decoder = Decoder(**decoder)
        self.dim_xhat = self.p_decoder.dim_x

        # p(xhat_t | x_{t-1}, u_{t-1})
        self.p_xhat = Pxhat(**pxhat)

    def forward(self, I_t: Tensor, x_q_tn1: Tensor, x_q_tn2: Tensor, u_tn1: Tensor, dt: Tensor):
        """"""
        v_tn1 = (x_q_tn1 - x_q_tn2) / dt
        v_t = self.f_velocity(x_q_tn1, u_tn1, v_tn1, dt)
        xhat_t = self.p_xhat.given(x_q_tn1, u_tn1).rsample()
        E_ll = self.img_reduction(tp.log(self.p_decoder, I_t).given(xhat_t))
        E_kl = self.vec_reduction(
            tp.KLdiv(self.q_encoder.given(I_t), self.p_transition.given(x_q_tn1, v_t, dt))
        )
        E = E_ll - self.kl_beta * E_kl

        if self.regularization:
            E -= self.vec_reduction(tp.KLdiv(self.q_encoder, tp.Normal01))

        x_q_t = self.q_encoder.rsample()

        return super().Pack(
            E=E,
            x_q_t=x_q_t,
            E_ll=E_ll.detach(),
            E_kl=E_kl.detach(),
            v_t=v_t.detach(),
        )


NewtonianVAECellFamily = Union[
    NewtonianVAECell,
    NewtonianVAEDerivationCell,
    NewtonianVAEV2Cell,
    NewtonianVAEV2DerivationCell,
]


class CellWrap:
    def __init__(self, cell: NewtonianVAECellFamily) -> None:
        self.cell = cell

        self.reset()

    def step(self, action: Tensor, observation: Tensor):
        """
        action: u_tn1
        observation: I_t
        """

        x_t = self.cell.q_encoder.given(observation).sample()

        if hasattr(self.cell, "dim_xhat"):
            if self.x_tn1 is None:
                I_t_dec = torch.full_like(observation, torch.nan)
            else:
                xhat_t = self.cell.p_xhat.given(self.x_tn1, action).sample()
                I_t_dec = self.cell.p_decoder.given(xhat_t).decode()

        else:
            I_t_dec = self.cell.p_decoder.given(x_t).decode()

        self.x_tn1 = x_t

        return x_t, I_t_dec

    def reset(self):
        self.x_tn1: Tensor = None
