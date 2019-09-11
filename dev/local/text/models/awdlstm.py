#AUTOGENERATED! DO NOT EDIT! File to edit: dev/32_text_models_awdlstm.ipynb (unless otherwise specified).

__all__ = ['dropout_mask', 'RNNDropout', 'WeightDropout', 'EmbeddingDropout', 'AWD_LSTM', 'awd_lstm_lm_split',
           'awd_lstm_lm_config', 'awd_lstm_clas_split', 'awd_lstm_clas_config', 'AWD_QRNN', 'awd_qrnn_lm_config',
           'awd_qrnn_clas_config']

#Cell
from ...torch_basics import *
from ...test import *
from ...core import *
from ...layers import *
from ...data.transform import *
from ...data.core import *
from ...data.source import *
from ...data.external import *
from ...data.pipeline import *
from ..core import *
from ...notebook.showdoc import show_doc

#Cell
def dropout_mask(x, sz, p):
    "Return a dropout mask of the same type as `x`, size `sz`, with probability `p` to cancel an element."
    return x.new(*sz).bernoulli_(1-p).div_(1-p)

#Cell
class RNNDropout(Module):
    "Dropout with probability `p` that is consistent on the seq_len dimension."
    def __init__(self, p=0.5): self.p=p

    def forward(self, x):
        if not self.training or self.p == 0.: return x
        return x * dropout_mask(x.data, (x.size(0), 1, x.size(2)), self.p)

#Cell
import warnings

#Cell
class WeightDropout(Module):
    "A module that warps another layer in which some weights will be replaced by 0 during training."

    def __init__(self, module, weight_p, layer_names='weight_hh_l0'):
        self.module,self.weight_p,self.layer_names = module,weight_p,L(layer_names)
        for layer in self.layer_names:
            #Makes a copy of the weights of the selected layers.
            w = getattr(self.module, layer)
            self.register_parameter(f'{layer}_raw', nn.Parameter(w.data))
            self.module._parameters[layer] = F.dropout(w, p=self.weight_p, training=False)

    def _setweights(self):
        "Apply dropout to the raw weights."
        for layer in self.layer_names:
            raw_w = getattr(self, f'{layer}_raw')
            self.module._parameters[layer] = F.dropout(raw_w, p=self.weight_p, training=self.training)

    def forward(self, *args):
        self._setweights()
        with warnings.catch_warnings():
            #To avoid the warning that comes because the weights aren't flattened.
            warnings.simplefilter("ignore")
            return self.module.forward(*args)

    def reset(self):
        for layer in self.layer_names:
            raw_w = getattr(self, f'{layer}_raw')
            self.module._parameters[layer] = F.dropout(raw_w, p=self.weight_p, training=False)
        if hasattr(self.module, 'reset'): self.module.reset()

#Cell
class EmbeddingDropout(Module):
    "Apply dropout with probabily `embed_p` to an embedding layer `emb`."

    def __init__(self, emb, embed_p):
        self.emb,self.embed_p = emb,embed_p

    def forward(self, words, scale=None):
        if self.training and self.embed_p != 0:
            size = (self.emb.weight.size(0),1)
            mask = dropout_mask(self.emb.weight.data, size, self.embed_p)
            masked_embed = self.emb.weight * mask
        else: masked_embed = self.emb.weight
        if scale: masked_embed.mul_(scale)
        return F.embedding(words, masked_embed, ifnone(self.emb.padding_idx, -1), self.emb.max_norm,
                           self.emb.norm_type, self.emb.scale_grad_by_freq, self.emb.sparse)

#Cell
class AWD_LSTM(Module):
    "AWD-LSTM inspired by https://arxiv.org/abs/1708.02182"
    initrange=0.1

    def __init__(self, vocab_sz, emb_sz, n_hid, n_layers, pad_token=1, hidden_p=0.2, input_p=0.6, embed_p=0.1,
                 weight_p=0.5, bidir=False):
        self.bs,self.emb_sz,self.n_hid,self.n_layers = 1,emb_sz,n_hid,n_layers
        self.n_dir = 2 if bidir else 1
        self.encoder = nn.Embedding(vocab_sz, emb_sz, padding_idx=pad_token)
        self.encoder_dp = EmbeddingDropout(self.encoder, embed_p)
        self.rnns = nn.ModuleList([self._one_rnn(emb_sz if l == 0 else n_hid, (n_hid if l != n_layers - 1 else emb_sz)//self.n_dir,
                                                 bidir, weight_p, l) for l in range(n_layers)])
        self.encoder.weight.data.uniform_(-self.initrange, self.initrange)
        self.input_dp = RNNDropout(input_p)
        self.hidden_dps = nn.ModuleList([RNNDropout(hidden_p) for l in range(n_layers)])

    def forward(self, inp, from_embeds=False):
        bs,sl = inp.shape[:2] if from_embeds else inp.shape
        if bs!=self.bs:
            self.bs=bs
            self.reset()
        raw_output = self.input_dp(inp if from_embeds else self.encoder_dp(inp))
        new_hidden,raw_outputs,outputs = [],[],[]
        for l, (rnn,hid_dp) in enumerate(zip(self.rnns, self.hidden_dps)):
            raw_output, new_h = rnn(raw_output, self.hidden[l])
            new_hidden.append(new_h)
            raw_outputs.append(raw_output)
            if l != self.n_layers - 1: raw_output = hid_dp(raw_output)
            outputs.append(raw_output)
        self.hidden = to_detach(new_hidden, cpu=False)
        return raw_outputs, outputs

    def _one_rnn(self, n_in, n_out, bidir, weight_p, l):
        "Return one of the inner rnn"
        rnn = nn.LSTM(n_in, n_out, 1, batch_first=True, bidirectional=bidir)
        return WeightDropout(rnn, weight_p)

    def _one_hidden(self, l):
        "Return one hidden state"
        nh = (self.n_hid if l != self.n_layers - 1 else self.emb_sz) // self.n_dir
        return (one_param(self).new_zeros(self.n_dir, self.bs, nh), one_param(self).new_zeros(self.n_dir, self.bs, nh))

    def reset(self):
        "Reset the hidden states"
        [r.reset() for r in self.rnns if hasattr(r, 'reset')]
        self.hidden = [self._one_hidden(l) for l in range(self.n_layers)]

#Cell
def awd_lstm_lm_split(model):
    "Split a RNN `model` in groups for differential learning rates."
    groups = [[rnn, dp] for rnn, dp in zip(model[0].rnns, model[0].hidden_dps)]
    return groups + [[model[0].encoder, model[0].encoder_dp, model[1]]]

#Cell
awd_lstm_lm_config = dict(emb_sz=400, n_hid=1152, n_layers=3, pad_token=1, bidir=False, output_p=0.1,
                          hidden_p=0.15, input_p=0.25, embed_p=0.02, weight_p=0.2, tie_weights=True, out_bias=True)

#Cell
def awd_lstm_clas_split(model):
    "Split a RNN `model` in groups for differential learning rates."
    groups = [[model[0].module.encoder, model[0].module.encoder_dp]]
    groups += [[rnn, dp] for rnn, dp in zip(model[0].module.rnns, model[0].module.hidden_dps)]
    return groups + [[model[1]]]

#Cell
awd_lstm_clas_config = dict(emb_sz=400, n_hid=1152, n_layers=3, pad_token=1, bidir=False, output_p=0.4,
                            hidden_p=0.3, input_p=0.4, embed_p=0.05, weight_p=0.5)

#Cell
class AWD_QRNN(AWD_LSTM):
    "Same as an AWD-LSTM, but using QRNNs instead of LSTMs"
    def _one_rnn(self, n_in, n_out, bidir, weight_p, l):
        from .qrnn import QRNN
        rnn = QRNN(n_in, n_out, 1, save_prev_x=True, zoneout=0, window=2 if l == 0 else 1, output_gate=True, bidirectional=bidir)
        rnn.layers[0].linear = WeightDropout(rnn.layers[0].linear, weight_p, layer_names='weight')
        return rnn

    def _one_hidden(self, l):
        "Return one hidden state"
        nh = (self.n_hid if l != self.n_layers - 1 else self.emb_sz) // self.n_dir
        return one_param(self).new_zeros(self.n_dir, self.bs, nh)

#Cell
awd_qrnn_lm_config = dict(emb_sz=400, n_hid=1552, n_layers=4, pad_token=1, bidir=False, output_p=0.1,
                          hidden_p=0.15, input_p=0.25, embed_p=0.02, weight_p=0.2, tie_weights=True, out_bias=True)

#Cell
awd_qrnn_clas_config = dict(emb_sz=400, n_hid=1552, n_layers=4, pad_token=1, bidir=False, output_p=0.4,
                            hidden_p=0.3, input_p=0.4, embed_p=0.05, weight_p=0.5)