
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.nn.parameter as parameter

class PreEncoderLayer(nn.Module):
    def __init__(self):
        super(PreEncoderLayer,self).__init__()
        self.layer = nn.Linear(128, 128)
        
    def forward(self,x):
        x = F.elu(self.layer(x))
        return x


class PositionWiseFFN(nn.Module):
    def __init__(self, feature_size, num_units=[2048, 512]):
        super(PositionWiseFFN, self).__init__()
        self.ffn = self._build_ffn(feature_size, num_units)

    def _build_ffn(self, feature_size, num_units):
        layers = []
        features = feature_size
        for unit in num_units:
            layers.append(nn.Linear(features, unit))
            features = unit

        return nn.Sequential(*layers)

    def forward(self, X):
        # assert if the feature size of inputs not the same as
        # the last ffn layer, since we need both of them
        # the same for residual network
        assert X.size(-1) == self.ffn[-1].bias.size(-1)
        ffn = self.ffn(X)
        # residual network
        ffn += X

        return ffn

class ExtendedSequential(nn.Sequential):
    def __init__(self, *args):
        super(ExtendedSequential, self).__init__(*args)

    def forward(self, *inputs):
        for module in self._modules.values():
            if isinstance(inputs, list) or isinstance(inputs, tuple):
                inputs = module(*inputs)
            else:
                inputs = module(inputs)
        return inputs

class MultiHeadAttention(nn.Module):
    def __init__(self, query_dim, key_dim, num_units, dropout_p=0.5, h=2, is_masked=False):
        super(MultiHeadAttention, self).__init__()
        if query_dim != key_dim:
            raise ValueError("query_dim and key_dim must be the same")
        if num_units % h != 0:
            raise ValueError("num_units must be dividable by h")
        if query_dim != num_units:
            raise ValueError("to employ residual connection, the number of "
                             "query_dim and num_units must be the same")

        self._num_units = num_units
        self._h = h
        self._key_dim = Variable(torch.FloatTensor([key_dim]))
        self._dropout_p = dropout_p
        self._is_masked = is_masked

        self.query_layer = nn.Linear(query_dim, num_units, bias=False)
        self.key_layer = nn.Linear(key_dim, num_units, bias=False)
        self.value_layer = nn.Linear(key_dim, num_units, bias=False)
        self.bn = nn.BatchNorm1d(num_units)

    def forward(self, query, keys):
        Q = self.query_layer(query)
        K = self.key_layer(keys)
        V = self.value_layer(keys)

        # split each Q, K and V into h different values from dim 2
        # and then merge them back together in dim 0
        chunk_size = int(self._num_units / self._h)
        Q = torch.cat(Q.split(split_size=chunk_size, dim=2), dim=0)
        K = torch.cat(K.split(split_size=chunk_size, dim=2), dim=0)
        V = torch.cat(V.split(split_size=chunk_size, dim=2), dim=0)

        # calculate QK^T
        attention = torch.matmul(Q, K.transpose(1, 2))
        # normalize with sqrt(dk)
        attention = attention / torch.sqrt(self._key_dim)
        # use masking (usually for decoder) to prevent leftward
        # information flow and retains auto-regressive property
        # as said in the paper
        if self._is_masked:
            diag_vals = attention[0].sign().abs()
            diag_mat = diag_vals.tril()
            diag_mat = diag_mat.unsqueeze(0).expand(attention.size())
            # we need to enforce converting mask to Variable, since
            # in pytorch we can't do operation between Tensor and
            # Variable
            mask = Variable(
                torch.ones(diag_mat.size()) * (-2**32 + 1), requires_grad=False)
            # this is some trick that I use to combine the lower diagonal
            # matrix and its masking. (diag_mat-1).abs() will reverse the value
            # inside diag_mat, from 0 to 1 and 1 to zero. with this
            # we don't need loop operation andn could perform our calculation
            # faster
            attention = (attention * diag_mat) + (mask * (diag_mat-1).abs())
        # put it to softmax
        attention = F.softmax(attention, dim=-1)
        # apply dropout
        attention = F.dropout(attention, self._dropout_p)
        # multiplyt it with V
        attention = torch.matmul(attention, V)
        # convert attention back to its input original size
        restore_chunk_size = int(attention.size(0) / self._h)
        attention = torch.cat(
            attention.split(split_size=restore_chunk_size, dim=0), dim=2)
        # residual connection
        attention += query
        # apply batch normalization
        attention = self.bn(attention.transpose(1, 2)).transpose(1, 2)

        return attention
