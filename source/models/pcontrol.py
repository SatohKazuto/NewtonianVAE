from numbers import Real
from typing import Optional, Union

import torch
import torch.nn.functional as F
import torch.nn.init
from torch import Tensor, nn

import mypython.ai.torchprob as tp
from mypython.terminal import Color

from .cell import NewtonianVAECellFamily


class PurePControl:
    """
    Paper:
        we sample random starting and goal states, and successively
        apply the control law ut ∝ (x(Igoal) − xt(It)).
    """

    def __init__(
        self,
        Igoal: Tensor,
        alpha: float,
        cell: NewtonianVAECellFamily,
    ) -> None:
        self.alpha = alpha
        self.cell = cell

        dtype = next(cell.parameters()).dtype
        device = next(cell.parameters()).device

        Igoal = Igoal.to(dtype=dtype).to(device=device)

        self.x_goal = cell.q_encoder.given(Igoal).rsample()

    def step(self, x_t: Tensor):
        u_t = self.alpha * (self.x_goal - x_t)
        return u_t


class PControl(tp.GMM):
    r"""
    Eq (12)

    .. math::
        \begin{array}{ll}
            P(\u_t \mid \x_t) = \displaystyle \sum_{n=1}^N \pi_n(\x_t) \mathcal{N}(\u_t \mid K_n(\x_n^{goal} - \x_t), \sigma_n^2)
        \end{array}
    """

    def __init__(
        self,
        N: int,
        dim_x: int,
        dim_pi_middle: int,
        std: Optional[Real],
        each_k: bool = False,
    ) -> None:
        super().__init__()

        self.N = N

        self.pi_model = nn.Sequential(
            nn.Linear(dim_x, dim_pi_middle),
            nn.Mish(),
            nn.Linear(dim_pi_middle, dim_pi_middle),
            nn.Mish(),
            nn.Linear(dim_pi_middle, N),
            nn.Softmax(-1),
        )

        if std is not None:
            self.std: Tensor
            self.register_buffer("std", torch.tensor(std))
            self.std_function = lambda x: x
        else:
            # σ is not σ_t, so σ should not depend on x_t.
            self.std = nn.Parameter(torch.randn(1))
            self.std_function = F.softplus

        self.x_goal_n = nn.Parameter(torch.randn(N, dim_x))

        if each_k:
            self.K_n = nn.Parameter(torch.randn(N, dim_x))
        else:
            self.K_n = nn.Parameter(torch.randn(N, 1))

    def pi(self, x_t: Tensor) -> Tensor:
        """
        Returns:
            pi: (BS, N)
        """
        pi = self.pi_model(x_t)
        return pi

    def forward(self, x_t: Tensor):
        """
        x_t: shape (BS, dim_x)
        """
        pi = self.pi(x_t)
        # Color.print(pi.shape, c=Color.red)

        x_t = x_t.unsqueeze(-2)
        mu = self.K_n * (self.x_goal_n - x_t)  # (BS, N, dim_x)
        sigma = self.std_function(self.std)
        return pi, mu, sigma

    def step(self, x_t: Tensor):
        u_t = self.given(x_t).sample()
        return u_t
