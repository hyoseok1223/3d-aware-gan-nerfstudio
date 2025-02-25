# Copyright 2022 The Nerfstudio Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Special activation functions.
"""

import torch
from torch.autograd import Function
from torch.cuda.amp import custom_bwd, custom_fwd


class _TruncExp(Function):  # pylint: disable=abstract-method
    # Implementation from torch-ngp:
    # https://github.com/ashawkey/torch-ngp/blob/93b08a0d4ec1cc6e69d85df7f0acdfb99603b628/activation.py
    @staticmethod
    @custom_fwd(cast_inputs=torch.float32)
    def forward(ctx, x):  # pylint: disable=arguments-differ
        ctx.save_for_backward(x)
        return torch.exp(x)

    @staticmethod
    @custom_bwd
    def backward(ctx, g):  # pylint: disable=arguments-differ
        x = ctx.saved_tensors[0]
        return g * torch.exp(x.clamp(-15, 15))


trunc_exp = _TruncExp.apply
"""Same as torch.exp, but with the backward pass clipped to prevent vanishing/exploding
gradients."""

# # SIREN : Implicit Neural Representations with Periodic Activation Function
# # https://arxiv.org/abs/2006.09661
# class Sine(nn.Module):
#     """Sine Activation Function."""

#     def __init__(self):
#         super().__init__()
#     def forward(self, x):
#         return torch.sin(30. * x) # FIXME 30 나눠줘야 하는거 체크. 
 