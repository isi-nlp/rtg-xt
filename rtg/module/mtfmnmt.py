#!/usr/bin/env python
#
# Author: Thamme Gowda [tg (at) isi (dot) edu] 
# Created: 2/15/19


import inspect
import copy
import torch
import math
import torch.nn as nn

from rtg.module.tfmnmt import (Encoder, EncoderLayer, PositionwiseFeedForward, PositionalEncoding,
                               Generator, MultiHeadedAttention, Embeddings, TransformerNMT,
                               TransformerTrainer)
from rtg import TranslationExperiment as Experiment, log
from rtg.dataprep import PAD_TOK_IDX as pad_idx

""""In NMT, DecoderLayer also has source attention.
But here, decoder layer is just like Encoder layer: self_attn and feed forward"""
from rtg.module.tfmnmt import EncoderLayer as MDecoderLayer
from rtg.module.tfmnmt import Encoder as MDecoder


class MEmbeddings(nn.Module):

    def __init__(self, d_model, vocab):
        super().__init__()
        self.lut = nn.Embedding(vocab, d_model, padding_idx=pad_idx)
        self.vocab = vocab
        self.d_model = d_model
        # merge [sent_repr; embedding] --> d_model
        self.merge = nn.Linear(2 * d_model, d_model)
        self.scaler = math.sqrt(self.d_model)

    def forward(self, x):
        # this gets wrapped in Sequential, which takes only one arg
        word_ids, sent_repr = x
        batch, max_len = word_ids.shape
        embs = self.lut(word_ids) * self.scaler

        # seq_lens = (word_ids != pad_idx).sum(dim=1, dtype=torch.float)
        # sent_repr = sent_repr * seq_lens.unsqueeze(1)
        # sent_repr = F.tanh(sent_repr)
        sent_repr = sent_repr * self.scaler

        embs = embs.view(batch, max_len, self.d_model)
        assert sent_repr.shape == (batch, self.d_model)
        sent_repr = sent_repr.view(batch, 1, self.d_model).expand_as(embs)
        concatd = torch.cat([sent_repr, embs], dim=-1)
        merged = self.merge(concatd)
        return merged


class MTransformerNMT(TransformerNMT):

    @property
    def model_type(self):
        return 'mtfmnmt'

    @classmethod
    def make_model(cls, src_vocab, tgt_vocab, n_layers=6, hid_size=512, ff_size=2048, n_heads=8,
                   dropout=0.1, tied_emb='three-way', exp: Experiment = None):
        """
        Helper: Construct a model from hyper parameters."
        :return: model, args
        """

        # get all args for reconstruction at a later phase
        _, _, _, args = inspect.getargvalues(inspect.currentframe())
        for exclusion in ['cls', 'exp']:
            del args[exclusion]  # exclude some args
        # In case you are wondering, why I didnt use **kwargs here:
        #   these args are read from conf file where user can introduce errors, so the parameter
        #   validation and default value assignment is implicitly done by function call for us :)

        c = copy.deepcopy
        attn = MultiHeadedAttention(n_heads, hid_size)
        ff = PositionwiseFeedForward(hid_size, ff_size, dropout)
        encoder = Encoder(EncoderLayer(hid_size, c(attn), c(ff), dropout), n_layers)
        decoder = MDecoder(MDecoderLayer(hid_size, c(attn), c(ff), dropout), n_layers)

        src_emb = nn.Sequential(Embeddings(hid_size, src_vocab),
                                PositionalEncoding(hid_size, dropout))
        tgt_emb = nn.Sequential(MEmbeddings(hid_size, tgt_vocab),
                                PositionalEncoding(hid_size, dropout))
        generator = Generator(hid_size, tgt_vocab)

        model = cls(encoder, decoder, src_emb, tgt_emb, generator)
        if tied_emb:
            model.tie_embeddings(tied_emb)

        # This was important from their code.
        # Initialize parameters with Glorot / fan_avg.
        for p in model.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        return model, args

    def forward(self, src, tgt, src_mask, tgt_mask, gen_probs=False, log_probs=True):
        "Take in and process masked src and target sequences."
        assert src.shape[0] == tgt.shape[0]
        sent_repr = self.encode(src, src_mask)
        feats = self.decode(sent_repr, tgt, tgt_mask)
        return self.generator(feats, log_probs=log_probs) if gen_probs else feats

    def encode(self, src, src_mask):
        batch_size = src.shape[0]
        # ADD CLS token
        #cls_col = torch.full((batch_size, 1), fill_value=cls_idx, device=device, dtype=torch.long)
        #src = torch.cat([cls_col, src], dim=1)
        # assuming first col of mask is proper
        #src_mask = torch.cat([src_mask[:, :, :1], src_mask], dim=-1)

        embs = self.src_embed(src)
        enc_feats = self.encoder(embs, src_mask)
        sent_repr = enc_feats[:, 0, :]  # CLS token features
        return sent_repr

    def decode(self, sent_repr, tgt, tgt_mask):
        embs = self.tgt_embed((tgt, sent_repr))
        return self.decoder(embs, tgt_mask)


class MTransformerTrainer(TransformerTrainer):

    def __init__(self, *args, model_factory=MTransformerNMT.make_model, **kwargs):
        super().__init__(*args, model_factory=model_factory, **kwargs)
        self.model: True = self.model  # type annotation


def __test_model__():
    from rtg.dummy import DummyExperiment
    from rtg import Batch, my_tensor as tensor

    vocab_size = 24
    args = {
        'src_vocab': vocab_size,
        'tgt_vocab': vocab_size,
        'n_layers': 4,
        'hid_size': 128,
        'ff_size': 256,
        'n_heads': 4
    }

    from rtg.module.decoder import Decoder

    exp = DummyExperiment("work.tmp.mtfmnmt", config={'model_type': 'mtfmnmt'}, read_only=True,
                          vocab_size=vocab_size)
    exp.model_args = args
    trainer = MTransformerTrainer(exp=exp, warmup_steps=200)
    decr = Decoder.new(exp, trainer.model)

    assert 2 == Batch.bos_val
    src = tensor([[4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
                  [13, 12, 11, 10, 9, 8, 7, 6, 5, 4]])
    src_lens = tensor([src.size(1)] * src.size(0))

    def check_pt_callback(**args):
        res = decr.greedy_decode(src, src_lens, max_len=12)
        for score, seq in res:
            log.info(f'{score:.4f} :: {seq}')

    batch_size = 50
    steps = 2000
    check_point = 50
    trainer.train(steps=steps, check_point=check_point, batch_size=batch_size,
                  check_pt_callback=check_pt_callback)


if __name__ == '__main__':
    __test_model__()