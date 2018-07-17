import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.utils.weight_norm as wn
from torch.nn.modules.batchnorm import _BatchNorm

import numpy as np
import pdb

from layers import * 
from utils import * 

'''
Abstract Classes to define common interface for invertible functions
'''
# Abstract Class for bijective functions
class Layer(nn.Module):
    def __init__(self):
        super(Layer, self).__init__()

    def forward_and_jacobian(self, x, objective):
        raise NotImplementedError

    def reverse_and_jacobian(self, y, objective):
        raise NotImplementedError

# Wrapper for stacking multiple layers 
class LayerList(Layer):
    def __init__(self, list_of_layers=None):
        super(LayerList, self).__init__()
        self.layers = nn.ModuleList(list_of_layers)

    def __getitem__(self, i):
        return self.layers[i]

    def forward_and_jacobian(self, x, objective):
        for layer in self.layers: 
            x, objective = layer.forward_and_jacobian(x, objective)
        return x, objective

    def reverse_and_jacobian(self, x, objective):
        for layer in reversed(self.layers): 
            x, objective = layer.reverse_and_jacobian(x, objective)
        return x, objective

###############################################################################

'''
Permutation Layers 
'''
# Shuffling on the channel axis
class Shuffle(Layer):
    def __init__(self, num_channels):
        super(Shuffle, self).__init__()
        indices = np.arange(num_channels)
        np.random.shuffle(indices)
        rev_indices = np.zeros_like(indices)
        for i in range(num_channels): 
            rev_indices[indices[i]] = i

        indices = torch.from_numpy(indices).long()
        rev_indices = torch.from_numpy(rev_indices).long()
        self.indices, self.rev_indices = indices.cuda(), rev_indices.cuda()

    def forward_and_jacobian(self, x, objective):
        return x[:, self.indices], objective

    def reverse_and_jacobian(self, x, objective):
        return x[:, self.rev_indices], objective
        

# Reversing on the channel axis
class Reverse(Shuffle):
    def __init__(self, num_channels):
        super(Reverse, self).__init__()
        indices = np.copy(np.arange(num_channel)[::-1])
        self.indices = torch.from_numpy(indices).long()
        self.indices = self.indices.cuda()
        self.rev_indices = self.indices

# Invertible 1x1 convolution
class Invertible1x1Conv(Layer, nn.Conv2d):
    def __init__(self, num_channels):
        super(Invertible1x1Conv, self).__init__()
        nn.Conv2d.__init__(self, num_channels, num_channels, 1, bias=False)
        self.num_channels = num_channels

    def reset_parameters(self):
        # initialization done with rotation matrix
        w_init = np.linalg.qr(np.random.randn((self.num_channels, self.num_channels)))[0]
        w_init = torch.from_numpy(w_init.astype('float32'))
        w_init = w_init.cuda()
        w_init = w_init.unsqueeze(-1).unsqueeze(-1)
        self.weight.copy_(w_init)

    def forward_and_jacobian(self, x, objective):
        dlogdet = torch.det(self.weight.squeeze()).abs().log() * x.size(-2) * x.size(-1)
        objective += dlogdet
        output = F.conv2d(x, self.weight, self.bias, self.stride, self.padding, \
                    self.dilation, self.groups)
        return output, objective

    def reverse_and_jacobian(self, x, objective):
        dlogdet = torch.det(self.weight.squeeze()).abs().log() * x.size(-2) * x.size(-1)
        objective -= dlogdet
        weight_inv = torch.inverse(self.weight.squeeze()).unsqueeze(-1).unsqueeze(-1)
        output = F.conv2d(x, weight_inv, self.bias, self.stride, self.padding, \
                    self.dilation, self.groups)
        return output, objective

###############################################################################

'''
Layers involving squeeze operations defined in RealNVP / Glow. 
'''
# Trades space for depth and vice versa
class Squeeze(Layer):
    def __init__(self, input_shape, factor=2):
        super(Squeeze, self).__init__()
        assert factor > 1 and isinstance(factor, int), 'no point of using this if factor <= 1'
        self.factor = factor
        self.input_shape = input_shape

    @property
    def output_shape(self):
        bs, c, h, w = self.input_shape
        return (bs, c * self.factor * self.factor, h // self.factor, w // self.factor)

    def squeeze_bchw(self, x):
        bs, c, h, w = x.size()
        assert h % self.factor == 0 and w % self.factor == 0, pdb.set_trace()
        
        # done as in GLOW repository
        x = x.transpose(3, 1).contiguous()
        x = x.reshape(-1, h // self.factor, self.factor, w // self.factor, self.factor, c)
        x = x.permute(0, 1, 3, 5, 2, 4)
        x = x.reshape(-1, h // self.factor, w // self.factor, c * self.factor ** 2)
        return x.transpose(3, 1).contiguous()
 
    def unsqueeze_bchw(self, x):
        bs, c, h, w = x.size()
        assert c >= 4 and c % 4 == 0

        # done as in GLOW repository
        x = x.transpose(3, 1).contiguous()
        x = x.view(-1, h, w, int(c / self.factor ** 2), self.factor, self.factor)
        x = x.permute(0, 1, 4, 2, 5, 3)
        x = x.view(-1, int(h * w), int(w * self.factor), int(c / self.factor ** 2))
        return x.transpose(3, 1).contiguous()
    
    def forward_and_jacobian(self, x, objective):
        if len(x.size()) != 4: 
            raise NotImplementedError # Maybe ValueError would be more appropriate

        return self.squeeze_bchw(x), objective
        
    def reverse_and_jacobian(self, x, objective):
        if len(x.size()) != 4: 
            raise NotImplementedError

        return self.unsqueeze_bchw(x), objective

###############################################################################

'''
Layers involving prior
'''
# Split Layer for multi-scale architecture. Factor of 2 hardcoded.
class Split(Squeeze):
    def __init__(self, input_shape):
        super(Split, self).__init__(input_shape)
        bs, c, h, w = input_shape
        self.conv_zero = Conv2dZeroInit(c // 2, c, 3, padding=(3 - 1) // 2)

    @property
    def output_shape(self):
        bs, c, h, w = self.input_shape
        return (bs, c * 2, h // 2, w // 2)

    def split2d_prior(self, x):
        h = self.conv_zero(x)
        mean, logs = h[:, 0::2], h[:, 1::2]
        return gaussian_diag(mean, logs)

    def forward_and_jacobian(self, x, objective):
        bs, c, h, w = x.size()
        z1, z2 = torch.chunk(x, 2, dim=1)
        pz = self.split2d_prior(z1)
        # TODO: modify this to keep batch if objective is tensor
        objective += pz.logp(z2).sum()
        z1 = self.squeeze_bchw(z1)
        return z1, objective

    def reverse_and_jacobian(self, x, objective):
        z1 = unsqueeze_bchw(x)
        pz = split2d_prior(z1)
        z2 = pz.sample
        z = torch.cat([z1, z2], dim=1)
        # TODO: is this correct ?
        objective -= pz.logp(z2)
        return z, objective

# Gaussian Prior that's compatible with the Layer framework
class GaussianPrior(Layer):
    def __init__(self, input_shape, args):
        super(GaussianPrior, self).__init__()
        if args.learntop: 
            self.conv = Conv2dZeroInit(input_shape[1], 2 * input_shape[1], 3, padding=(3 - 1) // 2)
        else: 
            self.conv = None

    def forward_and_jacobian(self, x, objective):
        mean_and_logsd = torch.cat([torch.zeros_like(x) for _ in range(2)], dim=1)
        
        if self.conv: 
            mean_and_logsd = self.conv(mean_and_logsd)

        mean, logsd = torch.chunk(mean_and_logsd, 2, dim=1)

        pz = gaussian_diag(mean, logsd)
        objective += pz.logp(x).sum()

        return None, objective

    def backward_and_jacobian(self, x, objective):
        assert x is None
        objective = objective or 0.
        mean_and_logsd = torch.cat(*[torch.zeros_like(x) for _ in range(2)], dim=1)
        
        if self.conv: 
            mean_and_logsd = self.conv(mean_and_logsd)

        mean, logsd = torch.chunk(mean_and_logsd, 2, dim=1)
        pz = gaussian_diag(mean, logsd)

        return pz.sample, objective
         
###############################################################################

'''
Coupling Layers
'''
# Additive Coupling Layer
class AdditiveCoupling(Layer):
    def __init__(self, num_features):
        super(AdditiveCoupling, self).__init__()
        assert num_features % 2 == 0
        self.NN = NN(num_features // 2)

    def forward_and_jacobian(self, x, objective):
        z1, z2 = torch.chunk(x, 2, dim=1)
        z2 += self.NN(z1)
        return torch.cat([z1, z2], dim=1), objective

    def reverse_and_jacobian(self, x, objective):
        z1, z2 = torch.chunk(x, 2, dim=1)
        z2 -= self.NN(z1)
        return torch.cat([z1, z2], dim=1), objective

# Additive Coupling Layer
class AffineCoupling(Layer):
    def __init__(self, num_features):
        super(AffineCoupling, self).__init__()
        # assert num_features % 2 == 0
        self.NN = NN(num_features // 2, channels_out=num_features)

    def forward_and_jacobian(self, x, objective):
        z1, z2 = torch.chunk(x, 2, dim=1)
        h = self.NN(z1)
        shift = h[:, 0::2]
        scale = F.sigmoid(h[:, 1::2] + 2.)
        z2 += shift
        z2 *= scale
        # TODO: check if should keep batch axis
        try: objective += torch.sum(torch.log(scale))
        except: pdb.set_trace()

        return torch.cat([z1, z2], dim=1), objective

    def reverse_and_jacobian(self, x, objective):
        z1, z2 = torch.chunk(x, 2, dim=1)
        h = self.NN(z1)
        shift = h[:, 0::2]
        scale = F.sigmoid(h[:, 1::2] + 2.)
        z2 /= scale
        z2 -= shift
        # TODO: check if should keep batch axis
        objective -= torch.sum(torch.log(scale))
        return torch.cat([z1, z2], dim=1), objective

###############################################################################

'''
Normalizing Layers
'''
# ActNorm Layer with data-dependant init
class ActNorm(Layer):
    def __init__(self):
        raise NotImplementedError

    def forward_and_jacobian(self, x, objective):
        pass

    def reverse_and_jacobian(self, x, objective):
        pass

# Batch Normalization, with no affine parameters, and log_det
class BatchNorm(Layer, _BatchNorm):
    def __init__(self, num_features, eps=1e-5, momentum=0.1):
        # this transformation keeps track of running stats, but is not affine. 
        _BatchNorm.__init__(self, num_features, eps=eps, momentum=momentum, affine=False)
        # Layer.__init__(self) 

    def forward_and_jacobian(self, input, objective):
        output = F.batch_norm(input, self.running_mean, self.running_var, None, None, 
            self.training, self.momentum, self.eps)
        
        # Since there is no way to fetch the actual std used during batch norm, we 
        # must recalculate it. It **sems** the following (commented) code replicates the 

        input_shape = input.size()
        input = input.view(input_shape[0], input_shape[1], -1)

        # 2) Compute the sum and the square-sum
        sum_size = input.size(0) * input.size(-1)
        input_sum = input.sum(dim=0).sum(dim=-1)
        input_ssum = (input ** 2).sum(dim=0).sum(dim=-1)

        # 3) compute mean and variance
        mean = input_sum / sum_size
        sumvar = input_ssum - input_sum * mean
        var = (sumvar / sum_size).clamp(self.eps)
        inv_std = var ** -0.5

        # TODO: make sure this is equivalent to REAL_NVP's batch_norm_log_diff
        # TODO: if keep batch axis, do not multiply by batch shape
        log_det_jacobian = torch.log(torch.abs(inv_std)) * input.size(0) * input.size(-1)
        objective += log_det_jacobian.sum()

        return output, objective

    def reverse(self, input, objective):
        assert not self.training, 'reverse pass should only be used for sampling'

        # 1) Resize the input ot (B, C, -1)
        input_shape = input.size()
        input = input.view(input_shape[0], input_shape[1], -1)

        # 2) Compute the sum and the square-sum
        sum_size = input.size(0) * input.size(-1)
        input_sum = input.sum(dim=0).sum(dim=-1)
        input_ssum = (input ** 2).sum(dim=0).sum(dim=-1)

        # 3) compute mean and variance
        mean = input_sum / sum_size
        sumvar = input_ssum - input_sum * mean
        var = (sumvar / sum_size).clamp(1e-5)
        unbias_var = sumvar / (sum_size - 1)
        inv_std = var ** -0.5

        # 4) normalize, but the other way around
        unsqueeze_ft = lambda x: x.unsqueeze(0).unsqueeze(-1).detach()
        output = input * var ** 0.5
        output = output + mean
        
        log_det_jacobian = torch.log(torch.abs(inv_std)) * input.size(0) * input.size(2) * input.size(3)
        # TODO: make sure this is equivalent to REAL_NVP's batch_norm_log_diff
        # TODO: if keep batch axis, do not multiply by batch shape
        objective -= log_det_jacobian

        return output.view(input_shape), objective

###############################################################################

'''
Stacked Layers
'''
# 1 step of the flow (see Figure 2 a) in the original paper)
class RevNetStep(LayerList):
    def __init__(self, num_channels, args):
        super(RevNetStep, self).__init__()
        self.args = args
        layers = []
        if args.norm == 'actnorm': 
            layers += [ActNorm()]
        elif args.norm == 'batchnorm':
            layers += [BatchNorm(num_channels)]
        else: 
            assert not args.norm	       
 
        if args.permutation == 'reverse':
            layers += [Reverse(num_channels)]
        elif args.permutation == 'shuffle': 
            layers += [Shuffle(num_channels)]
        elif args.permutation == 'conv':
            layers += [Invertible1x1Conv(num_channels)]
        else: 
            raise ValueError

        if args.coupling == 'additive': 
            layers += [AdditiveCoupling(num_channels)]
        elif args.coupling == 'affine':
            layers += [AffineCoupling(num_channels)]
        else: 
            raise ValueError

        self.layers = nn.ModuleList(layers)

# 1 "scale" i.e. stacking of multiple steps. See Figure 2 b) in the original paper
class RevNet(LayerList):
    def __init__(self, input_shape, args):
        super(RevNet, self).__init__()
        bs, c, h, w = input_shape 
        self.layers = nn.ModuleList([RevNetStep(c, args) for _ in range(args.depth)])


# Full model
class Codec(LayerList):
    def __init__(self, input_shape, args):
        super(Codec, self).__init__()
        layers = [Squeeze(input_shape)] 
        for i in range(args.n_levels):
            input_shape = layers[-1].output_shape
            layers += [RevNet(input_shape, args)]
            
            if i < args.n_levels - 1: 
                layers += [Split(input_shape)]
        
        layers += [GaussianPrior(input_shape, args)]
        self.layers = nn.ModuleList(layers)
    
