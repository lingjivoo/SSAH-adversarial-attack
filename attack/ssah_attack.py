import torch
import torch.nn as nn
from utils.auxiliary_utils import *
import torch.nn.functional as F
from attack.DWT import *
from torch.autograd import Variable
import torch.optim as optim


class SSAH(nn.Module):
    """"
    Parameters:
    -----------

    """

    def __init__(self,
                 model: nn.Module,
                 num_iteration: int = 150,
                 learning_rate: float = 0.001,
                 device: torch.device = torch.device('cuda'),
                 Targeted: bool = False,
                 dataset: str = 'cifar10',
                 m: float = 0.2,
                 alpha: float = 1,
                 beta: float = 0.1) -> None:
        super(SSAH, self).__init__()
        self.model = model
        self.device = device
        self.lr = learning_rate
        self.target = Targeted
        self.num_iteration = num_iteration
        self.dataset = dataset
        self.m = m
        self.alpha = alpha
        self.beta = beta

        self.encoder_fea = nn.Sequential(*list(self.model.children())[:-1]).to(self.device)
        self.encoder_fea= nn.DataParallel(self.encoder_fea)
        self.avg_pool = nn.AdaptiveAvgPool2d((1, 1)).to(self.device)
        self.model = nn.DataParallel(self.model)

        self.normalize_fn = normalize_fn(self.dataset)

        self.DWT = DWT_2D_tiny(wavename='haar')
        self.IDWT = IDWT_2D_tiny(wavename='haar')

    def fea_extract(self, inputs: torch.Tensor) -> torch.Tensor:
        fea = self.encoder_fea(inputs)
        b, c, h, w = fea.shape
        fea = self.avg_pool(fea).view(b, c)
        return fea

    def cal_sim(self, adv, inputs):
        adv = F.normalize(adv, dim=1)
        inputs = F.normalize(inputs, dim=1)

        r, c = inputs.shape
        sim_matrix = torch.matmul(adv, inputs.T)
        mask = torch.eye(r, dtype=torch.bool).to(self.device)
        pos_sim = sim_matrix[mask].view(r, -1)
        neg_sim = sim_matrix.view(r, -1)
        return pos_sim, neg_sim

    def select_setp1(self, pos_sim, neg_sim):
        neg_sim, indices = torch.sort(neg_sim, descending=True)
        pos_neg_sim = torch.cat([pos_sim, neg_sim[:, -1].view(pos_sim.shape[0], -1)], dim=1)
        return pos_neg_sim, indices

    def select_step2(self, pos_sim, neg_sim, indices):
        hard_sample = indices[:, -1]
        ones = torch.sparse.torch.eye(neg_sim.shape[1]).to(self.device)
        hard_one_hot = ones.index_select(0, hard_sample).bool()
        hard_sim = neg_sim[hard_one_hot].view(neg_sim.shape[0], -1)
        pos_neg_sim = torch.cat([pos_sim, hard_sim], dim=1)
        return pos_neg_sim

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:

        with torch.no_grad():
            inputs_fea = self.fea_extract(self.normalize_fn(inputs))

        # low frequency component
        inputs_ll = self.DWT(inputs)
        inputs_ll = self.IDWT(inputs_ll)

        # changes of variables
        eps = 3e-7
        modifier = torch.arctanh(inputs * (2 - eps * 2) - 1 + eps)
        modifier = Variable(modifier, requires_grad=True)
        modifier = modifier.to(self.device)
        optimizer = optim.Adam([modifier], lr=self.lr)

        lowFre_loss = nn.SmoothL1Loss(reduction='sum')

        for step in range(self.num_iteration):
            optimizer.zero_grad()
            self.encoder_fea.zero_grad()

            adv = 0.5 * (torch.tanh(modifier) + 1)
            adv_fea = self.fea_extract(self.normalize_fn(adv))

            adv_ll = self.DWT(adv)
            adv_ll = self.IDWT(adv_ll)

            pos_sim, neg_sim = self.cal_sim(adv_fea, inputs_fea)
            if step == 0:
                pos_neg_sim, indices = self.select_setp1(pos_sim, neg_sim)
            else:
                pos_neg_sim = self.select_step2(pos_sim, neg_sim, indices)

            sim_pos = pos_neg_sim[:, 0]
            sim_neg = pos_neg_sim[:, -1]

            w_p = torch.clamp_min(sim_pos.detach() - self.m, min=0)
            w_n = torch.clamp_min(1 + self.m - sim_neg.detach(), min=0)

            adv_cost = self.alpha * torch.sum(torch.clamp(w_p * sim_pos - w_n * sim_neg, min=0))
            lowFre_cost = self.beta * lowFre_loss(adv_ll, inputs_ll)
            total_cost = adv_cost + lowFre_cost

            optimizer.zero_grad()
            total_cost.backward()
            optimizer.step()

        adv = 0.5 * (torch.tanh(modifier.detach()) + 1)
        return adv
