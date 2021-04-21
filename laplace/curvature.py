from abc import ABC, abstractmethod
from math import sqrt
import torch
from torch.nn import MSELoss, CrossEntropyLoss

from backpack import backpack, extend
from backpack.extensions import DiagGGNExact, DiagGGNMC, KFAC, KFLR, SumGradSquared, BatchGrad

from laplace.jacobians import jacobians, last_layer_jacobians
from laplace.matrix import Kron


class CurvatureInterface(ABC):

    def __init__(self, model, likelihood):
        assert likelihood in ['regression', 'classification']
        self.likelihood = likelihood
        self.model = model
        if likelihood == 'regression':
            self.lossfunc = MSELoss(reduction='sum')
            self.factor = 0.5  # convert to standard Gauss. log N(y|f,1)
        else:
            self.lossfunc = CrossEntropyLoss(reduction='sum')
            self.factor = 1.

    @abstractmethod
    def full(self, X, y, **kwargs):
        pass

    @abstractmethod
    def kron(self, X, y, **kwargs):
        pass

    @abstractmethod
    def diag(self, X, y, **kwargs):
        pass

    def _get_full_ggn(self, Js, f, y):
        loss = self.factor * self.lossfunc(f, y)
        if self.likelihood == 'regression':
            H_ggn = torch.einsum('mkp,mkq->pq', Js, Js)
        else:
            # second derivative of log lik is diag(p) - pp^T
            ps = torch.softmax(f, dim=-1)
            H_lik = torch.diag_embed(ps) - torch.einsum('mk,mc->mck', ps, ps)
            H_ggn = torch.einsum('mcp,mck,mkq->pq', Js, H_lik, Js)
        return loss.detach(), H_ggn


class BackPackInterface(CurvatureInterface):

    def __init__(self, model, likelihood, last_layer=False):
        super().__init__(model, likelihood)
        self.last_layer = last_layer
        extend(self._model)
        extend(self.lossfunc)

    @property
    def _model(self):
        return self.model.last_layer if self.last_layer else self.model


class BackPackGGN(BackPackInterface):
    """[summary]

    MSELoss = |y-f|_2^2 -> d/df = -2(y-f)
    log N(y|f,1) propto 1/2|y-f|_2^2 -> d/df = -(y-f)
    --> factor for regression is 0.5 for loss and ggn
    """

    def __init__(self, model, likelihood, last_layer=False, stochastic=False):
        super().__init__(model, likelihood, last_layer)
        self.stochastic = stochastic

    def _get_diag_ggn(self):
        if self.stochastic:
            return torch.cat([p.diag_ggn_mc.data.flatten() for p in self._model.parameters()])
        else:
            return torch.cat([p.diag_ggn_exact.data.flatten() for p in self._model.parameters()])

    def _get_kron_factors(self):
        if self.stochastic:
            return Kron([p.kfac for p in self._model.parameters()])
        else:
            return Kron([p.kflr for p in self._model.parameters()])

    @staticmethod
    def _rescale_kron_factors(kron, M, N):
        # Renormalize Kronecker factor to sum up correctly over N data points with batches of M
        # for M=N (full-batch) just M/N=1
        for F in kron.kfacs:
            if len(F) == 2:
                F[1] *= M/N
        return kron

    def diag(self, X, y, **kwargs):
        context = DiagGGNMC if self.stochastic else DiagGGNExact
        f = self.model(X)
        loss = self.lossfunc(f, y)
        with backpack(context()):
            loss.backward()
        dggn = self._get_diag_ggn()

        return self.factor * loss.detach(), self.factor * dggn

    def kron(self, X, y, N, **wkwargs) -> [torch.Tensor, Kron]:
        context = KFAC if self.stochastic else KFLR
        f = self.model(X)
        loss = self.lossfunc(f, y)
        with backpack(context()):
            loss.backward()
        kron = self._get_kron_factors()
        kron = self._rescale_kron_factors(kron, len(y), N)

        return self.factor * loss.detach(), self.factor * kron

    def full(self, X, y, **kwargs):
        if self.stochastic:
            raise ValueError('Stochastic approximation not implemented for full GGN.')

        if self.last_layer:
            Js, f = last_layer_jacobians(self.model, X)
        else:
            Js, f = jacobians(self.model, X)
        loss, H_ggn = self._get_full_ggn(Js, f, y)

        return loss, H_ggn


class BackPackEF(BackPackInterface):

    def _get_individual_gradients(self):
        return torch.cat([p.grad_batch.data.flatten(start_dim=1)
                          for p in self._model.parameters()], dim=1)

    def diag(self, X, y, **kwargs):
        f = self.model(X)
        loss = self.lossfunc(f, y)
        with backpack(SumGradSquared()):
            loss.backward()
        diag_EF = torch.cat([p.sum_grad_squared.data.flatten()
                             for p in self._model.parameters()])

        return self.factor * loss.detach(), self.factor * diag_EF

    def kron(self, X, y, **kwargs):
        raise NotImplementedError()

    def full(self, X, y, **kwargs):
        f = self.model(X)
        loss = self.lossfunc(f, y)
        with backpack(BatchGrad()):
            loss.backward()
        Gs = self._get_individual_gradients()
        H_ef = Gs.T @ Gs
        return self.factor * loss.detach(), self.factor * H_ef
