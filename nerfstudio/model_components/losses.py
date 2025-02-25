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
Collection of Losses.
"""
from enum import Enum

import torch
from torch import nn
from torchtyping import TensorType
from typing_extensions import Literal
from typing import List

from nerfstudio.model_components.op import conv2d_gradfix

from nerfstudio.cameras.rays import RaySamples
from nerfstudio.utils.math import masked_reduction, normalized_depth_scale_and_shift

L1Loss = nn.L1Loss
MSELoss = nn.MSELoss

LOSSES = {"L1": L1Loss, "MSE": MSELoss}

EPS = 1.0e-7

# Sigma scale factor from Urban Radiance Fields (Rematas et al., 2022)
URF_SIGMA_SCALE_FACTOR = 3.0


class DepthLossType(Enum):
    """Types of depth losses for depth supervision."""

    DS_NERF = 1
    URF = 2


def outer(
    t0_starts: TensorType[..., "num_samples_0"],
    t0_ends: TensorType[..., "num_samples_0"],
    t1_starts: TensorType[..., "num_samples_1"],
    t1_ends: TensorType[..., "num_samples_1"],
    y1: TensorType[..., "num_samples_1"],
) -> TensorType[..., "num_samples_0"]:
    """Faster version of

    https://github.com/kakaobrain/NeRF-Factory/blob/f61bb8744a5cb4820a4d968fb3bfbed777550f4a/src/model/mipnerf360/helper.py#L117
    https://github.com/google-research/multinerf/blob/b02228160d3179300c7d499dca28cb9ca3677f32/internal/stepfun.py#L64

    Args:
        t0_starts: start of the interval edges
        t0_ends: end of the interval edges
        t1_starts: start of the interval edges
        t1_ends: end of the interval edges
        y1: weights
    """
    cy1 = torch.cat([torch.zeros_like(y1[..., :1]), torch.cumsum(y1, dim=-1)], dim=-1)

    idx_lo = torch.searchsorted(t1_starts.contiguous(), t0_starts.contiguous(), side="right") - 1
    idx_lo = torch.clamp(idx_lo, min=0, max=y1.shape[-1] - 1)
    idx_hi = torch.searchsorted(t1_ends.contiguous(), t0_ends.contiguous(), side="right")
    idx_hi = torch.clamp(idx_hi, min=0, max=y1.shape[-1] - 1)
    cy1_lo = torch.take_along_dim(cy1[..., :-1], idx_lo, dim=-1)
    cy1_hi = torch.take_along_dim(cy1[..., 1:], idx_hi, dim=-1)
    y0_outer = cy1_hi - cy1_lo

    return y0_outer


def lossfun_outer(
    t: TensorType[..., "num_samples+1"],
    w: TensorType[..., "num_samples"],
    t_env: TensorType[..., "num_samples+1"],
    w_env: TensorType[..., "num_samples"],
):
    """
    https://github.com/kakaobrain/NeRF-Factory/blob/f61bb8744a5cb4820a4d968fb3bfbed777550f4a/src/model/mipnerf360/helper.py#L136
    https://github.com/google-research/multinerf/blob/b02228160d3179300c7d499dca28cb9ca3677f32/internal/stepfun.py#L80

    Args:
        t: interval edges
        w: weights
        t_env: interval edges of the upper bound enveloping histogram
        w_env: weights that should upper bound the inner (t,w) histogram
    """
    w_outer = outer(t[..., :-1], t[..., 1:], t_env[..., :-1], t_env[..., 1:], w_env)
    return torch.clip(w - w_outer, min=0) ** 2 / (w + EPS)


def ray_samples_to_sdist(ray_samples):
    """Convert ray samples to s space"""
    starts = ray_samples.spacing_starts
    ends = ray_samples.spacing_ends
    sdist = torch.cat([starts[..., 0], ends[..., -1:, 0]], dim=-1)  # (num_rays, num_samples + 1)
    return sdist


def interlevel_loss(weights_list, ray_samples_list):
    """Calculates the proposal loss in the MipNeRF-360 paper.

    https://github.com/kakaobrain/NeRF-Factory/blob/f61bb8744a5cb4820a4d968fb3bfbed777550f4a/src/model/mipnerf360/model.py#L515
    https://github.com/google-research/multinerf/blob/b02228160d3179300c7d499dca28cb9ca3677f32/internal/train_utils.py#L133
    """
    c = ray_samples_to_sdist(ray_samples_list[-1]).detach()
    w = weights_list[-1][..., 0].detach()
    loss_interlevel = 0.0
    for ray_samples, weights in zip(ray_samples_list[:-1], weights_list[:-1]):
        sdist = ray_samples_to_sdist(ray_samples)
        cp = sdist  # (num_rays, num_samples + 1)
        wp = weights[..., 0]  # (num_rays, num_samples)
        loss_interlevel += torch.mean(lossfun_outer(c, w, cp, wp))
    return loss_interlevel


# Verified
def lossfun_distortion(t, w):
    """
    https://github.com/kakaobrain/NeRF-Factory/blob/f61bb8744a5cb4820a4d968fb3bfbed777550f4a/src/model/mipnerf360/helper.py#L142
    https://github.com/google-research/multinerf/blob/b02228160d3179300c7d499dca28cb9ca3677f32/internal/stepfun.py#L266
    """
    ut = (t[..., 1:] + t[..., :-1]) / 2
    dut = torch.abs(ut[..., :, None] - ut[..., None, :])
    loss_inter = torch.sum(w * torch.sum(w[..., None, :] * dut, dim=-1), dim=-1)

    loss_intra = torch.sum(w**2 * (t[..., 1:] - t[..., :-1]), dim=-1) / 3

    return loss_inter + loss_intra


def distortion_loss(weights_list, ray_samples_list):
    """From mipnerf360"""
    c = ray_samples_to_sdist(ray_samples_list[-1])
    w = weights_list[-1][..., 0]
    loss = torch.mean(lossfun_distortion(c, w))
    return loss


def nerfstudio_distortion_loss(
    ray_samples: RaySamples,
    densities: TensorType["bs":..., "num_samples", 1] = None,
    weights: TensorType["bs":..., "num_samples", 1] = None,
) -> TensorType["bs":..., 1]:
    """Ray based distortion loss proposed in MipNeRF-360. Returns distortion Loss.

    .. math::

        \\mathcal{L}(\\mathbf{s}, \\mathbf{w}) =\\iint\\limits_{-\\infty}^{\\,\\,\\,\\infty}
        \\mathbf{w}_\\mathbf{s}(u)\\mathbf{w}_\\mathbf{s}(v)|u - v|\\,d_{u}\\,d_{v}

    where :math:`\\mathbf{w}_\\mathbf{s}(u)=\\sum_i w_i \\mathbb{1}_{[\\mathbf{s}_i, \\mathbf{s}_{i+1})}(u)`
    is the weight at location :math:`u` between bin locations :math:`s_i` and :math:`s_{i+1}`.

    Args:
        ray_samples: Ray samples to compute loss over
        densities: Predicted sample densities
        weights: Predicted weights from densities and sample locations
    """
    if torch.is_tensor(densities):
        assert not torch.is_tensor(weights), "Cannot use both densities and weights"
        # Compute the weight at each sample location
        weights = ray_samples.get_weights(densities)
    if torch.is_tensor(weights):
        assert not torch.is_tensor(densities), "Cannot use both densities and weights"

    starts = ray_samples.spacing_starts
    ends = ray_samples.spacing_ends

    assert starts is not None and ends is not None, "Ray samples must have spacing starts and ends"
    midpoints = (starts + ends) / 2.0  # (..., num_samples, 1)

    loss = (
        weights * weights[..., None, :, 0] * torch.abs(midpoints - midpoints[..., None, :, 0])
    )  # (..., num_samples, num_samples)
    loss = torch.sum(loss, dim=(-1, -2))[..., None]  # (..., num_samples)
    loss = loss + 1 / 3.0 * torch.sum(weights**2 * (ends - starts), dim=-2)

    return loss


def orientation_loss(
    weights: TensorType["bs":..., "num_samples", 1],
    normals: TensorType["bs":..., "num_samples", 3],
    viewdirs: TensorType["bs":..., 3],
):
    """Orientation loss proposed in Ref-NeRF.
    Loss that encourages that all visible normals are facing towards the camera.
    """
    w = weights
    n = normals
    v = viewdirs * -1
    n_dot_v = (n * v[..., None, :]).sum(axis=-1)
    return (w[..., 0] * torch.fmin(torch.zeros_like(n_dot_v), n_dot_v) ** 2).sum(dim=-1)


def pred_normal_loss(
    weights: TensorType["bs":..., "num_samples", 1],
    normals: TensorType["bs":..., "num_samples", 3],
    pred_normals: TensorType["bs":..., "num_samples", 3],
):
    """Loss between normals calculated from density and normals from prediction network."""
    return (weights[..., 0] * (1.0 - torch.sum(normals * pred_normals, dim=-1))).sum(dim=-1)


def ds_nerf_depth_loss(
    weights: TensorType[..., "num_samples", 1],
    termination_depth: TensorType[..., 1],
    steps: TensorType[..., "num_samples", 1],
    lengths: TensorType[..., "num_samples", 1],
    sigma: TensorType[0],
) -> TensorType[..., 1]:
    """Depth loss from Depth-supervised NeRF (Deng et al., 2022).

    Args:
        weights: Weights predicted for each sample.
        termination_depth: Ground truth depth of rays.
        steps: Sampling distances along rays.
        lengths: Distances between steps.
        sigma: Uncertainty around depth values.
    Returns:
        Depth loss scalar.
    """
    depth_mask = termination_depth > 0

    loss = -torch.log(weights + EPS) * torch.exp(-((steps - termination_depth[:, None]) ** 2) / (2 * sigma)) * lengths
    loss = loss.sum(-2) * depth_mask
    return torch.mean(loss)


def urban_radiance_field_depth_loss(
    weights: TensorType[..., "num_samples", 1],
    termination_depth: TensorType[..., 1],
    predicted_depth: TensorType[..., 1],
    steps: TensorType[..., "num_samples", 1],
    sigma: TensorType[0],
) -> TensorType[..., 1]:
    """Lidar losses from Urban Radiance Fields (Rematas et al., 2022).

    Args:
        weights: Weights predicted for each sample.
        termination_depth: Ground truth depth of rays.
        predicted_depth: Depth prediction from the network.
        steps: Sampling distances along rays.
        sigma: Uncertainty around depth values.
    Returns:
        Depth loss scalar.
    """
    depth_mask = termination_depth > 0

    # Expected depth loss
    expected_depth_loss = (termination_depth - predicted_depth) ** 2

    # Line of sight losses
    target_distribution = torch.distributions.normal.Normal(0.0, sigma / URF_SIGMA_SCALE_FACTOR)
    termination_depth = termination_depth[:, None]
    line_of_sight_loss_near_mask = torch.logical_and(
        steps <= termination_depth + sigma, steps >= termination_depth - sigma
    )
    line_of_sight_loss_near = (weights - torch.exp(target_distribution.log_prob(steps - termination_depth))) ** 2
    line_of_sight_loss_near = (line_of_sight_loss_near_mask * line_of_sight_loss_near).sum(-2)
    line_of_sight_loss_empty_mask = steps < termination_depth - sigma
    line_of_sight_loss_empty = (line_of_sight_loss_empty_mask * weights**2).sum(-2)
    line_of_sight_loss = line_of_sight_loss_near + line_of_sight_loss_empty

    loss = (expected_depth_loss + line_of_sight_loss) * depth_mask
    return torch.mean(loss)


def depth_loss(
    weights: TensorType[..., "num_samples", 1],
    ray_samples: RaySamples,
    termination_depth: TensorType[..., 1],
    predicted_depth: TensorType[..., 1],
    sigma: TensorType[0],
    directions_norm: TensorType[..., 1],
    is_euclidean: bool,
    depth_loss_type: DepthLossType,
) -> TensorType[0]:
    """Implementation of depth losses.

    Args:
        weights: Weights predicted for each sample.
        ray_samples: Samples along rays corresponding to weights.
        termination_depth: Ground truth depth of rays.
        predicted_depth: Depth prediction from the network.
        sigma: Uncertainty around depth value.
        directions_norm: Norms of ray direction vectors in the camera frame.
        is_euclidean: Whether ground truth depths corresponds to normalized direction vectors.
        depth_loss_type: Type of depth loss to apply.

    Returns:
        Depth loss scalar.
    """
    if not is_euclidean:
        termination_depth = termination_depth * directions_norm
    steps = (ray_samples.frustums.starts + ray_samples.frustums.ends) / 2

    if depth_loss_type == DepthLossType.DS_NERF:
        lengths = ray_samples.frustums.ends - ray_samples.frustums.starts
        return ds_nerf_depth_loss(weights, termination_depth, steps, lengths, sigma)

    if depth_loss_type == DepthLossType.URF:
        return urban_radiance_field_depth_loss(weights, termination_depth, predicted_depth, steps, sigma)

    raise NotImplementedError("Provided depth loss type not implemented.")


def monosdf_normal_loss(
    normal_pred: TensorType["num_samples", 3], normal_gt: TensorType["num_samples", 3]
) -> TensorType[0]:
    """
    Normal consistency loss proposed in monosdf - https://niujinshuchong.github.io/monosdf/
    Enforces consistency between the volume rendered normal and the predicted monocular normal.
    With both angluar and L1 loss. Eq 14 https://arxiv.org/pdf/2206.00665.pdf
    Args:
        normal_pred: volume rendered normal
        normal_gt: monocular normal
    """
    normal_gt = torch.nn.functional.normalize(normal_gt, p=2, dim=-1)
    normal_pred = torch.nn.functional.normalize(normal_pred, p=2, dim=-1)
    l1 = torch.abs(normal_pred - normal_gt).sum(dim=-1).mean()
    cos = (1.0 - torch.sum(normal_pred * normal_gt, dim=-1)).mean()
    return l1 + cos


class MiDaSMSELoss(nn.Module):
    """
    data term from MiDaS paper
    """

    def __init__(self, reduction_type: Literal["image", "batch"] = "batch"):
        super().__init__()

        self.reduction_type: Literal["image", "batch"] = reduction_type
        # reduction here is different from the image/batch-based reduction. This is either "mean" or "sum"
        self.mse_loss = MSELoss(reduction="none")

    def forward(
        self, prediction: TensorType[1, 32, "mult"], target: TensorType[1, 32, "mult"], mask: TensorType[1, 32, "mult"]
    ) -> TensorType[0]:
        """
        Args:
            prediction: predicted depth map
            target: ground truth depth map
            mask: mask of valid pixels
        Returns:
            mse loss based on reduction function
        """
        summed_mask = torch.sum(mask, (1, 2))
        image_loss = torch.sum(self.mse_loss(prediction, target) * mask, (1, 2))
        # multiply by 2 magic number?
        image_loss = masked_reduction(image_loss, 2 * summed_mask, self.reduction_type)

        return image_loss


# losses based on https://github.com/autonomousvision/monosdf/blob/main/code/model/loss.py
class GradientLoss(nn.Module):
    """
    multiscale, scale-invariant gradient matching term to the disparity space.
    This term biases discontinuities to be sharp and to coincide with discontinuities in the ground truth
    More info here https://arxiv.org/pdf/1907.01341.pdf Equation 11
    """

    def __init__(self, scales: int = 4, reduction_type: Literal["image", "batch"] = "batch"):
        """
        Args:
            scales: number of scales to use
            reduction_type: either "batch" or "image"
        """
        super().__init__()
        self.reduction_type = reduction_type
        self.__scales = scales

    def forward(
        self, prediction: TensorType[1, 32, "mult"], target: TensorType[1, 32, "mult"], mask: TensorType[1, 32, "mult"]
    ) -> TensorType[0]:
        """
        Args:
            prediction: predicted depth map
            target: ground truth depth map
            mask: mask of valid pixels
        Returns:
            gradient loss based on reduction function
        """
        total = 0

        for scale in range(self.__scales):
            step = pow(2, scale)

            grad_loss = self.gradient_loss(
                prediction[:, ::step, ::step],
                target[:, ::step, ::step],
                mask[:, ::step, ::step],
            )
            total += grad_loss

        return total

    def gradient_loss(
        self, prediction: TensorType[1, 32, "mult"], target: TensorType[1, 32, "mult"], mask: TensorType[1, 32, "mult"]
    ) -> TensorType[0]:
        """
        multiscale, scale-invariant gradient matching term to the disparity space.
        This term biases discontinuities to be sharp and to coincide with discontinuities in the ground truth
        More info here https://arxiv.org/pdf/1907.01341.pdf Equation 11
        Args:
            prediction: predicted depth map
            target: ground truth depth map
            reduction: reduction function, either reduction_batch_based or reduction_image_based
        Returns:
            gradient loss based on reduction function
        """
        summed_mask = torch.sum(mask, (1, 2))
        diff = prediction - target
        diff = torch.mul(mask, diff)

        grad_x = torch.abs(diff[:, :, 1:] - diff[:, :, :-1])
        mask_x = torch.mul(mask[:, :, 1:], mask[:, :, :-1])
        grad_x = torch.mul(mask_x, grad_x)

        grad_y = torch.abs(diff[:, 1:, :] - diff[:, :-1, :])
        mask_y = torch.mul(mask[:, 1:, :], mask[:, :-1, :])
        grad_y = torch.mul(mask_y, grad_y)

        image_loss = torch.sum(grad_x, (1, 2)) + torch.sum(grad_y, (1, 2))
        image_loss = masked_reduction(image_loss, summed_mask, self.reduction_type)

        return image_loss


class ScaleAndShiftInvariantLoss(nn.Module):
    """
    Scale and shift invariant loss as described in
    "Towards Robust Monocular Depth Estimation: Mixing Datasets for Zero-shot Cross-dataset Transfer"
    https://arxiv.org/pdf/1907.01341.pdf
    """

    def __init__(self, alpha: float = 0.5, scales: int = 4, reduction_type: Literal["image", "batch"] = "batch"):
        """
        Args:
            alpha: weight of the regularization term
            scales: number of scales to use
            reduction_type: either "batch" or "image"
        """
        super().__init__()
        self.__data_loss = MiDaSMSELoss(reduction_type=reduction_type)
        self.__regularization_loss = GradientLoss(scales=scales, reduction_type=reduction_type)
        self.__alpha = alpha

        self.__prediction_ssi = None

    def forward(
        self, prediction: TensorType[1, 32, "mult"], target: TensorType[1, 32, "mult"], mask: TensorType[1, 32, "mult"]
    ) -> TensorType[0]:
        """
        Args:
            prediction: predicted depth map (unnormalized)
            target: ground truth depth map (normalized)
            mask: mask of valid pixels
        Returns:
            scale and shift invariant loss
        """
        scale, shift = normalized_depth_scale_and_shift(prediction, target, mask)
        self.__prediction_ssi = scale.view(-1, 1, 1) * prediction + shift.view(-1, 1, 1)

        total = self.__data_loss(self.__prediction_ssi, target, mask)
        if self.__alpha > 0:
            total += self.__alpha * self.__regularization_loss(self.__prediction_ssi, target, mask)

        return total

    def __get_prediction_ssi(self):
        """
        scale and shift invariant prediction
        from https://arxiv.org/pdf/1907.01341.pdf equation 1
        """
        return self.__prediction_ssi

    prediction_ssi = property(__get_prediction_ssi)

# NOTE - D : nonstaturating loss / gradient penalty - R1 Regularization / (latent penalty , position penalty 같은거? 논문에서도 못봤는데) 
# NOTE - G : nonstaturating loss / path length regularization ( PathLengthRegularization()  NOTE - 딱히 pigan에서는 안쓰고 있어서 우선 안쓰는 걸로. )  / (latent penalty , position penalty 같은거? 논문에서도 못봤는데) 

# NOTE - Literal 클래스 : 변수가 가질 수 있는 값의 범위를 명시적으로 제한 -> Literal[1,2,3,4] 의 의미는 갑싱 가질 수 있는 값의 범위가 이 것들 중 하나라는 것이다. 

# GAN Loss
class GANLoss(nn.Module):
    def __init__(self, m_type: Literal['G', 'D'], r1_gamma = 0.2, reg_step : int = 16): 
        """
        Args:
            m_type: according to module type (m_type) determine gan loss  ( gnerator : 'G' or discriminator : 'D')
        """
        super().__init__()
        self.m_type = m_type

        self.non_saturating_loss = torch.nn.functional.softplus
        self.r1_gamma = r1_gamma #NOTE - Config로부터 가져올 것. 
        self.reg_step = reg_step


    def forward(
        self, preds: List[TensorType['batch', 1]], **kwargs
        # real_pred: TensorType['batch', 1], fake_pred: TensorType['batch', 1], **kwargs # real_img = []
        # self, prediction: TensorType[1, 32, "mult"], target: TensorType[1, 32, "mult"], mask: TensorType[1, 32, "mult"] # 여기도 바뀌어야하는게 입력이 real / fake임.
    ) -> TensorType[0]:


        if self.m_type == 'G':
            fake_pred = preds[0]
            loss_Gmain = self.non_saturating_loss(-fake_pred).mean()
            loss = loss_Gmain

        elif self.m_type == 'D':
            fake_pred, real_pred = preds
            loss_Dmain = self.non_saturating_loss(-real_pred).mean() + self.non_saturating_loss(fake_pred).mean()
            loss = loss_Dmain
            if step % self.reg_step == 0:
                loss_r1 = self.r1_regularization(real_pred,real_img) * (self.r1_gamma/2) # *args같은거 쓰면 될 듯.
                loss += loss_r1
        return loss
            

    def r1_regularization(self, real_pred, real_img):
        with conv2d_gradfix.no_weight_gradients():
            grad_real = torch.autograd.grad(outputs = real_pred.sum(), inputs=real_img, create_graph=True)
            grad_penalty = grad_real.pow(2).reshape(grad_real.shape[0], -1).sum(1).mean()
        return grad_penalty
         

