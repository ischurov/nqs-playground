# Copyright Tom Westerhout (c) 2019-2021
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#
#     * Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
#     * Neither the name of Tom Westerhout nor the names of other
#       contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from lattice_symmetries import Interaction, Operator
from loguru import logger
import numpy as np
import torch
from torch import Tensor

from ._extension import lib
from .core import as_spins_tensor, forward_with_batches

__all__ = [
    "heisenberg_interaction",
    "local_values",
    "reference_log_apply",
]


def heisenberg_interaction(
    edges: List[Tuple[int, int]], coupling: complex = 1.0, sign_rule: bool = False
) -> Interaction:
    coupling = complex(coupling)
    if coupling.imag != 0.0:
        logger.warn(
            "You are creating Heisenberg interaction term with a complex coupling constant. "
            "Please, be careful as it might lead to your Hamiltonian being non-Hermitian!"
        )
    # fmt: off
    if sign_rule:
        matrix = np.array([[1,  0,  0, 0],
                           [0, -1, -2, 0],
                           [0, -2, -1, 0],
                           [0,  0,  0, 1]], dtype=np.complex128)
    else:
        matrix = np.array([[1,  0,  0, 0],
                           [0, -1,  2, 0],
                           [0,  2, -1, 0],
                           [0,  0,  0, 1]], dtype=np.complex128)
    # fmt: on
    matrix *= coupling
    return Interaction(matrix, edges)


def _array_to_int(xs) -> int:
    r"""Convert an array of 8 int64 values (i.e. something like bits512) to a
    Python integer.
    """
    if not isinstance(xs, np.ndarray):
        xs = xs.cpu().numpy()
    xs = xs.view(np.uint64)
    if len(xs) == 0:
        return 0
    n = int(xs[-1])
    for i in reversed(range(0, len(xs) - 1)):
        n <<= 64
        n += int(xs[i])
    return n


def _reference_log_apply_one(spin, operator, log_psi, device):
    spins, coeffs = operator.apply(spin)
    spins = torch.from_numpy(spins.view(np.int64)).to(device)
    output = log_psi(spins)
    coeffs = torch.from_numpy(coeffs).to(device=device)
    output = output.to(dtype=coeffs.dtype)
    if output.dim() > 1:
        output.squeeze_(dim=1)
    scale = torch.max(output.real)
    output.real -= scale
    torch.exp_(output)
    # coeffs = torch.from_numpy(coeffs).to(device=device, dtype=output.dtype)
    return scale + torch.log(torch.dot(coeffs, output))


def reference_log_apply(spins, operator, log_psi, batch_size=None):
    r"""Reference implementation of _C.log_apply. It should be semantically the
    same, but orders of magnitude slower.
    """
    device = spins.device
    result = torch.empty(spins.size(0), dtype=torch.complex128)
    for (i, spin) in enumerate(spins.cpu()):
        result[i] = _reference_log_apply_one(_array_to_int(spin), operator, log_psi, device)
    return result.to(device)


def _isclose(a, b):
    return torch.isclose(a, b, rtol=5e-5, atol=5e-7)


@torch.no_grad()
def log_apply(
    spins: Tensor,
    hamiltonian: Operator,
    state: Callable[[Tensor], Tensor],
    batch_size: int = 128,
    debug: bool = False
) -> Tensor:
    batch_size = int(batch_size)
    if batch_size <= 0:
        raise ValueError("invalid batch_size: {}; expected a positive integer".format(batch_size))
    spins = as_spins_tensor(spins, force_width=True)
    # Flatten all dimensions except for the last
    original_shape = spins.size()[:-1]
    spins = spins.view(-1, spins.size(-1))
    # This is to help C++ recognize that we're dealing with a ScriptModule and
    # avoid going through Python for every forward pass
    if isinstance(state, torch.jit.ScriptModule):
        logger.debug("Using torch.jit.ScriptModule...")
        state = state._c._get_method("forward")
    else:
        logger.debug("Using a general Python function...")
    # Compute log(⟨s|H|ψ⟩) for all s.
    log_h_psi = lib.log_apply(spins, hamiltonian, state, batch_size)
    if debug:
        logger.debug("Checking against reference_log_apply...")
        _log_h_psi_py = reference_log_apply(spins, hamiltonian, state, batch_size)
        if not torch.all(torch.isclose(log_h_psi, _log_h_psi_py)):
        # if not torch.all(_isclose(log_h_psi, _log_h_psi_py)):
            for i, (e_cxx, e_py) in enumerate(zip(log_h_psi, _log_h_psi_py)):
                if not torch.isclose(e_cxx, e_py).item():
                    logger.error("C++ and Python produced different results for i={}: {} != {}", i, e_cxx.item(), e_py.item())
            assert False
    return log_h_psi.view(original_shape)


@torch.no_grad()
def local_values(
    spins: Tensor,
    hamiltonian: Operator,
    state: torch.jit.ScriptModule,
    log_psi: Optional[Tensor] = None,
    batch_size: int = 2048,
    debug: bool = False,
) -> Tensor:
    r"""Compute local values ``⟨s|H|ψ⟩/⟨s|ψ⟩`` for all ``s ∈ spins``.

    :param spins: Spin configurations ``{s}``. Must be either a
        ``numpy.ndarray`` of ``uint64`` or a ``torch.Tensor`` of ``int64``.
    :param hamiltonian: Hamiltonian.
    :param state: Quantum state ``ψ`` represented by a TorchScript module
        which predicts ``log(ψ(s))``.
    :param log_psi: Pre-computed ``log(ψ(s))`` for all ``s`` in ``spins``.
    :param batch_size: Batch size to use internally for forward propagationn
        through ``state``.
    """
    spins = as_spins_tensor(spins, force_width=True)
    log_h_psi = log_apply(spins, hamiltonian, state, batch_size, debug=debug)
    if log_psi is None:
        # Compute log(⟨s|ψ⟩) for all s.
        log_psi = forward_with_batches(state, spins.view(-1, spins.size(-1)), batch_size)
        log_psi = log_psi.view(spins.size()[:-1])
    # Compute ⟨s|H|ψ⟩/⟨s|ψ⟩
    log_psi = log_psi.to(log_h_psi.dtype)
    log_h_psi -= log_psi
    log_h_psi.exp_()
    return log_h_psi
