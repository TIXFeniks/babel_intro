#!/usr/bin/env python3
import math
import numpy as np
import tensorflow as tf
from lib.layers import *
from lib.tensor_utils import *
from collections import namedtuple
from . import TranslateModel


class FFN:
    """
    Feed-forward layer
    """
    def __init__(self, name,
                 inp_size, hid_size, out_size,
                 relu_dropout):
        self.name = name
        self.relu_dropout = relu_dropout

        with tf.variable_scope(name):
            self.first_conv = Dense(
                'conv1',
                inp_size, hid_size,
                activation=tf.nn.relu,
                b=tf.zeros_initializer())

            self.second_conv = Dense(
                'conv2',
                hid_size, out_size,
                activation=lambda x: x,
                b=tf.zeros_initializer())

    def __call__(self, inputs, params_summary=None):
        """
        inp: [batch_size * ninp * inp_dim]
        ---------------------------------
        out: [batch_size * ninp * out_dim]
        """
        with tf.variable_scope(self.name):
            hidden = self.first_conv(inputs)
            if is_dropout_enabled():
                hidden = tf.nn.dropout(hidden, 1.0 - self.relu_dropout)

            outputs = self.second_conv(hidden)

            return outputs


class MultiHeadAttn:
    """
    Multihead scaled-dot-product attention with input/output transformations
    """
    ATTN_BIAS_VALUE = -1e9

    def __init__(
        self, name, inp_size,
        key_depth, value_depth, output_depth,
        num_heads, attn_dropout, attn_value_dropout, debug=False, _format='combined'
    ):
        self.name = name
        self.key_depth = key_depth
        self.value_depth = value_depth
        self.num_heads = num_heads
        self.attn_dropout = attn_dropout
        self.attn_value_dropout = attn_value_dropout
        self.debug = debug
        self.format = _format

        with tf.variable_scope(name):
            self.scope = tf.get_variable_scope()

            if self.format == 'use_kv':
                self.query_conv = Dense(
                    'query_conv',
                    inp_size, key_depth,
                    activation=lambda x: x,
                    b=tf.zeros_initializer(),
                )

                self.kv_conv = Dense(
                    'mem_conv',
                    inp_size, key_depth + value_depth,
                    activation=lambda x: x,
                    b=tf.zeros_initializer(),
                )

                self.combined_conv = Dense(
                    'combined_conv',
                    inp_size, key_depth * 2 + value_depth,
                    activation=lambda x: x,
                    W=tf.concat([self.query_conv.W, self.kv_conv.W], axis=1),
                    b=tf.concat([self.query_conv.b, self.kv_conv.b], axis=0),
                )

            elif self.format == 'combined':
                self.combined_conv = Dense(
                    'mem_conv',  # old name for compatibility
                    inp_size, key_depth * 2 + value_depth,
                    activation=lambda x: x,
                    b=tf.zeros_initializer())

                self.query_conv = Dense(
                    'query_conv',
                    inp_size, key_depth,
                    activation=lambda x: x,
                    W=self.combined_conv.W[:, :key_depth],
                    b=self.combined_conv.b[:key_depth],
                )

                self.kv_conv = Dense(
                    'kv_conv',
                    inp_size, key_depth + value_depth,
                    activation=lambda x: x,
                    W=self.combined_conv.W[:, key_depth:],
                    b=self.combined_conv.b[key_depth:],
                )
            else:
                raise Exception("Unexpected format: " + self.format)

            self.out_conv = Dense(
                'out_conv',
                value_depth, output_depth,
                activation=lambda x: x,
                b=tf.zeros_initializer())

    def __call__(self, query_inp, attn_mask, kv_inp=None, kv=None):
        """
        query_inp: [batch_size * n_q * inp_dim]
        attn_mask: [batch_size * 1 * n_q * n_kv]
        kv_inp: [batch_size * n_kv * inp_dim]
        -----------------------------------------------
        results: [batch_size * n_q * output_depth]
        """
        assert kv is None or kv_inp is None, "please only feed one of kv or kv_inp"
        with tf.name_scope(self.name) as scope:
            if kv_inp is not None or kv is not None:
                q = self.query_conv(query_inp)
                if kv is None:
                    kv = self.kv_conv(kv_inp)
                k, v = tf.split(kv, [self.key_depth, self.value_depth], axis=2)
            else:
                combined = self.combined_conv(query_inp)
                q, k, v = tf.split(combined, [self.key_depth, self.key_depth, self.value_depth], axis=2)
            q = self._split_heads(q)  # [batch_size * n_heads * n_q * (k_dim/n_heads)]
            k = self._split_heads(k)  # [batch_size * n_heads * n_kv * (k_dim/n_heads)]
            v = self._split_heads(v)  # [batch_size * n_heads * n_kv * (v_dim/n_heads)]

            key_depth_per_head = self.key_depth / self.num_heads
            q = q / math.sqrt(key_depth_per_head)

            # Dot-product attention
            # logits: (batch_size * n_heads * n_q * n_kv)
            attn_bias = MultiHeadAttn.ATTN_BIAS_VALUE * (1 - attn_mask)
            logits = tf.matmul(
                tf.transpose(q, perm=[0, 1, 2, 3]),
                tf.transpose(k, perm=[0, 1, 3, 2])) + attn_bias
            weights = tf.nn.softmax(logits)

            if is_dropout_enabled():
                weights = tf.nn.dropout(weights, 1.0 - self.attn_dropout)
            x = tf.matmul(
                weights,                         # [batch_size * n_heads * n_q * n_kv]
                tf.transpose(v, perm=[0, 1, 2, 3])  # [batch_size * n_heads * n_kv * (v_deph/n_heads)]
            )
            combined_x = self._combine_heads(x)

            if is_dropout_enabled():
                combined_x = tf.nn.dropout(combined_x, 1.0 - self.attn_value_dropout)

            outputs = self.out_conv(combined_x)
            return outputs

    def _split_heads(self, x):
        """
        Split channels (dimension 3) into multiple heads (dimension 1)
        input: (batch_size * ninp * inp_dim)
        output: (batch_size * n_heads * ninp * (inp_dim/n_heads))
        """
        old_shape = x.get_shape().dims
        dim_size = old_shape[-1]
        new_shape = old_shape[:-1] + [self.num_heads] + [dim_size // self.num_heads if dim_size else None]
        ret = tf.reshape(x, tf.concat([tf.shape(x)[:-1], [self.num_heads, -1]], 0))
        ret.set_shape(new_shape)
        return tf.transpose(ret, [0, 2, 1, 3])  # [batch_size * n_heads * ninp * (hid_dim//n_heads)]

    def _combine_heads(self, x):
        """
        Inverse of split heads
        input: (batch_size * n_heads * ninp * (inp_dim/n_heads))
        out: (batch_size * ninp * inp_dim)
        """
        x = tf.transpose(x, [0, 2, 1, 3])
        old_shape = x.get_shape().dims
        a, b = old_shape[-2:]
        new_shape = old_shape[:-2] + [a * b if a and b else None]
        ret = tf.reshape(x, tf.concat([tf.shape(x)[:-2], [-1]], 0))
        ret.set_shape(new_shape)
        ret = tf.transpose(ret, perm=[0, 1, 2])
        return ret


class Transformer:
    def __init__(
            self, name,
            inp_voc, out_voc,
            *_args,
            emb_size=None, hid_size=512,
            key_size=None, value_size=None,
            inner_hid_size=None,  # DEPRECATED. Left for compatibility with older experiments
            ff_size=None,
            num_heads=8, num_layers=6,
            attn_dropout=0.0, attn_value_dropout=0.0, relu_dropout=0.0, res_dropout=0.1,
            debug=None, share_emb=False, inp_emb_bias=False, rescale_emb=False,
            dst_reverse=False, dst_rand_offset=False,
            res_steps='ldan', normalize_out=False, multihead_attn_format='v1', **_kwargs
    ):

        if isinstance(ff_size, str):
            ff_size = [int(i) for i in ff_size.split(':')]

        if _args:
            raise Exception("Unexpected positional arguments")

        emb_size = emb_size if emb_size else hid_size
        key_size = key_size if key_size else hid_size
        value_size = value_size if value_size else hid_size
        if key_size % num_heads != 0:
            raise Exception("Bad number of heads")
        if value_size % num_heads != 0:
            raise Exception("Bad number of heads")

        self.name = name
        self.num_layers_enc = num_layers
        self.num_layers_dec = num_layers
        self.res_dropout = res_dropout
        self.emb_size = emb_size
        self.hid_size = hid_size
        self.rescale_emb = rescale_emb
        self.dst_reverse = dst_reverse
        self.dst_rand_offset = dst_rand_offset
        self.normalize_out = normalize_out

        with tf.variable_scope(name):
            max_voc_size = max(len(inp_voc), len(out_voc))
            self.emb_inp = Embedding(
                'emb_inp', max_voc_size if share_emb else len(inp_voc), emb_size,
                initializer=tf.random_normal_initializer(0, emb_size**-.5))

            emb_out_matrix = None
            if share_emb:
                emb_out_matrix = self.emb_inp.mat
            self.emb_out = Embedding(
                'emb_out', max_voc_size if share_emb else len(out_voc), emb_size,
                matrix=emb_out_matrix,
                initializer=tf.random_normal_initializer(0, emb_size**-.5))

            self.emb_inp_bias = 0
            if inp_emb_bias:
                self.emb_inp_bias = tf.get_variable('emb_inp_bias', shape=[1, 1, emb_size])

            # Encoder Layers
            self.enc_attn = [ResidualLayerWrapper(
                'enc_attn-%i' % i,
                MultiHeadAttn(
                    'enc_attn-%i' % i,
                    inp_size=emb_size if i == 0 else hid_size,
                    key_depth=key_size,
                    value_depth=value_size,
                    output_depth=hid_size,
                    num_heads=num_heads,
                    attn_dropout=attn_dropout,
                    attn_value_dropout=attn_value_dropout,
                    debug=debug),
                inp_size=emb_size if i == 0 else hid_size,
                out_size=emb_size if i == 0 else hid_size,
                steps=res_steps,
                dropout=res_dropout)
                for i in range(self.num_layers_enc)]

            self.enc_ffn = [ResidualLayerWrapper(
                'enc_ffn-%i' % i,
                FFN(
                    'enc_ffn-%i' % i,
                    inp_size=emb_size if i == 0 else hid_size,
                    hid_size=ff_size if ff_size else (inner_hid_size if inner_hid_size else hid_size),
                    out_size=hid_size,
                    relu_dropout=relu_dropout),
                inp_size=emb_size if i == 0 else hid_size,
                out_size=hid_size,
                steps=res_steps,
                dropout=res_dropout)
                for i in range(self.num_layers_enc)]

            if self.normalize_out:
                self.enc_out_norm = LayerNorm('enc_out_norm', inp_size=emb_size if self.num_layers_enc == 0 else hid_size)

            # Decoder layers
            self.dec_attn = [ResidualLayerWrapper(
                'dec_attn-%i' % i,
                MultiHeadAttn(
                    'dec_attn-%i' % i,
                    inp_size=emb_size if i == 0 else hid_size,
                    key_depth=key_size,
                    value_depth=value_size,
                    output_depth=hid_size,
                    num_heads=num_heads,
                    attn_dropout=attn_dropout,
                    attn_value_dropout=attn_value_dropout,
                    debug=debug),
                inp_size=emb_size if i == 0 else hid_size,
                out_size=emb_size if i == 0 else hid_size,
                steps=res_steps,
                dropout=res_dropout)
                for i in range(self.num_layers_dec)]

            self.dec_enc_attn = [ResidualLayerWrapper(
                'dec_enc_attn-%i' % i,
                MultiHeadAttn(
                    'dec_enc_attn-%i' % i,
                    inp_size=emb_size if i == 0 else hid_size,
                    key_depth=key_size,
                    value_depth=value_size,
                    output_depth=hid_size,
                    num_heads=num_heads,
                    attn_dropout=attn_dropout,
                    attn_value_dropout=attn_value_dropout,
                    debug=debug,
                    _format='use_kv' if multihead_attn_format == 'v1' else 'combined',
                ),
                inp_size=emb_size if i == 0 else hid_size,
                out_size=emb_size if i == 0 else hid_size,
                steps=res_steps,
                dropout=res_dropout)
                for i in range(self.num_layers_dec)]

            self.dec_ffn = [ResidualLayerWrapper(
                'dec_ffn-%i' % i,
                FFN(
                    'dec_ffn-%i' % i,
                    inp_size=emb_size if i == 0 else hid_size,
                    hid_size=ff_size if ff_size else hid_size,
                    out_size=hid_size,
                    relu_dropout=relu_dropout),
                inp_size=emb_size if i == 0 else hid_size,
                out_size=hid_size,
                steps=res_steps,
                dropout=res_dropout)
                for i in range(self.num_layers_dec)]

            if self.normalize_out:
                self.dec_out_norm = LayerNorm('dec_out_norm', inp_size=emb_size if self.num_layers_dec == 0 else hid_size)

    def encode(self, inp, inp_len, is_train):
        with dropout_scope(is_train), tf.name_scope(self.name + '_enc') as scope:

            # Embeddings
            emb_inp = self.emb_inp(inp)  # [batch_size * ninp * emb_dim]
            if self.rescale_emb:
                emb_inp *= self.emb_size ** .5
            emb_inp += self.emb_inp_bias

            # Prepare decoder
            enc_attn_mask = self._make_enc_attn_mask(inp, inp_len)  # [batch_size * 1 * 1 * ninp]

            enc_inp = self._add_timing_signal(emb_inp)

            # Apply dropouts
            if is_dropout_enabled():
                enc_inp = tf.nn.dropout(enc_inp, 1.0 - self.res_dropout)

            # Encoder
            for layer in range(self.num_layers_enc):
                enc_inp = self.enc_attn[layer](enc_inp, enc_attn_mask)
                enc_inp = self.enc_ffn[layer](enc_inp)

            if self.normalize_out:
                enc_inp = self.enc_out_norm(enc_inp)

            enc_out = enc_inp

            return enc_out, enc_attn_mask

    def decode(self, out, out_len, out_reverse, enc_out, enc_attn_mask, is_train):
        with dropout_scope(is_train), tf.name_scope(self.name + '_dec') as scope:
            # Embeddings
            emb_out = self.emb_out(out)  # [batch_size * nout * emb_dim]
            if self.rescale_emb:
                emb_out *= self.emb_size ** .5

            # Shift right; drop embedding for last word
            emb_out = tf.pad(emb_out, [[0, 0], [1, 0], [0, 0]])[:, :-1, :]

            # Prepare decoder
            dec_attn_mask = self._make_dec_attn_mask(out)  # [1 * 1 * nout * nout]

            offset = 'random' if self.dst_rand_offset else 0
            dec_inp = self._add_timing_signal(emb_out, offset=offset, inp_reverse=out_reverse)
            # Apply dropouts
            if is_dropout_enabled():
                dec_inp = tf.nn.dropout(dec_inp, 1.0 - self.res_dropout)

            # bypass info from Encoder to avoid None gradients for num_layers_dec == 0
            if self.num_layers_dec == 0:
                inp_mask = tf.squeeze(tf.transpose(enc_attn_mask, perm=[3,1,2,0]),3)
                dec_inp += tf.reduce_mean(enc_out * inp_mask, axis=[0,1], keep_dims=True)

            # Decoder
            for layer in range(self.num_layers_dec):
                dec_inp = self.dec_attn[layer](dec_inp, dec_attn_mask)
                dec_inp = self.dec_enc_attn[layer](dec_inp, enc_attn_mask, enc_out)
                dec_inp = self.dec_ffn[layer](dec_inp)

            if self.normalize_out:
                dec_inp = self.dec_out_norm(dec_inp)

            dec_out = dec_inp
            return dec_out

    def _make_enc_attn_mask(self, inp, inp_len, dtype=tf.float32):
        """
        inp = [batch_size * ninp]
        inp_len = [batch_size]

        attn_mask = [batch_size * 1 * 1 * ninp]
        """
        with tf.variable_scope("make_enc_attn_mask"):
            inp_mask = tf.sequence_mask(inp_len, dtype=dtype, maxlen=tf.shape(inp)[1])

            attn_mask = inp_mask[:, None, None, :]
            return attn_mask

    def _make_dec_attn_mask(self, out, dtype=tf.float32):
        """
        out = [baatch_size * nout]

        attn_mask = [1 * 1 * nout * nout]
        """
        with tf.variable_scope("make_dec_attn_mask"):
            length = tf.shape(out)[1]
            lower_triangle = tf.matrix_band_part(tf.ones([length, length], dtype=dtype), -1, 0)
            attn_mask = tf.reshape(lower_triangle, [1, 1, length, length])
            return attn_mask

    def _add_timing_signal(self, inp, min_timescale=1.0, max_timescale=1.0e4, offset=0, inp_reverse=None):
        """
        inp: (batch_size * ninp * hid_dim)
        :param offset: add this number to all character positions.
            if offset == 'random', picks this number uniformly from [-32000,32000] integers
        :type offset: number, tf.Tensor or 'random'
        """
        with tf.variable_scope("add_timing_signal"):
            ninp = tf.shape(inp)[1]
            hid_size = tf.shape(inp)[2]

            position = tf.to_float(tf.range(ninp))[None, :, None]

            if offset == 'random':
                BIG_LEN = 32000
                offset = tf.random_uniform(tf.shape(position), minval=-BIG_LEN, maxval=BIG_LEN, dtype=tf.int32)

            # force broadcasting over batch axis
            if isinstance(offset * 1, tf.Tensor):  # multiply by 1 to also select variables, special generators, etc.
                assert offset.shape.ndims in (0, 1, 2)
                new_shape = [tf.shape(offset)[i] for i in range(offset.shape.ndims)]
                new_shape += [1] * (3 - len(new_shape))
                offset = tf.reshape(offset, new_shape)

            position += tf.to_float(offset)

            if inp_reverse is not None:
                position = tf.multiply(
                    position,
                    tf.where(
                        tf.equal(inp_reverse, 0),
                        tf.ones_like(inp_reverse, dtype=tf.float32),
                        -1.0 * tf.ones_like(inp_reverse, dtype=tf.float32)
                    )[:, None, None]  # (batch_size * ninp * dim)
                )
            num_timescales = hid_size // 2
            log_timescale_increment = (
                math.log(float(max_timescale) / float(min_timescale)) /
                (tf.to_float(num_timescales) - 1))
            inv_timescales = min_timescale * tf.exp(
                tf.to_float(tf.range(num_timescales)) * -log_timescale_increment)

            # scaled_time: [ninp * hid_dim]
            scaled_time = position * inv_timescales[None, None, :]
            signal = tf.concat([tf.sin(scaled_time), tf.cos(scaled_time)], axis=-1)
            signal = tf.pad(signal, [[0, 0], [0, 0], [0, tf.mod(hid_size, 2)]])
            return inp + signal


# ============================================================================
#                                  Transformer model

class Model(TranslateModel):

    DecState = namedtuple("transformer_state", ['enc_out', 'enc_attn_mask', 'attnP', 'rdo', 'out_seq', 'offset',
                                                'emb', 'dec_layers', 'dec_enc_kv', 'dec_dec_kv'])

    def __init__(self, name, inp_voc, out_voc, **hp):
        self.name = name
        self.inp_voc = inp_voc
        self.out_voc = out_voc
        self.hp = hp
        self.debug = hp.get('debug', None)

        # Parameters
        self.transformer = Transformer(name, inp_voc, out_voc, **hp)

        projection_matrix = None
        if hp.get('dwwt', False):
            projection_matrix = tf.transpose(self.transformer.emb_out.mat)

        with tf.variable_scope(name):
            self.logits = Dense('logits', self.transformer.hid_size, len(out_voc),
                                W=projection_matrix)

    # Train interface
    def symbolic_score(self, inp, out, is_train=False):
        inp_len = infer_length(inp, self.inp_voc.eos, time_major=False)
        out_len = infer_length(out, self.out_voc.eos, time_major=False)

        out_reverse = tf.zeros_like(inp_len)  # batch['out_reverse']

        # rdo: [batch_size * nout * hid_dim]
        enc_out, enc_attn_mask = self.transformer.encode(inp, inp_len, is_train)
        rdo = self.transformer.decode(out, out_len, out_reverse, enc_out, enc_attn_mask, is_train)

        return self.logits(rdo)

    def encode(self, batch, is_train=False, **kwargs):
        """
        :param batch: a dict containing 'inp':int32[batch_size * ninp] and optionally inp_len:int32[batch_size]
        :param is_train: if True, enables dropouts
        """
        inp = batch['inp']
        inp_len = batch.get('inp_len', infer_length(inp, self.inp_voc.eos, time_major=False))
        with dropout_scope(is_train), tf.name_scope(self.transformer.name):
            if self.debug:
                inp = tf.Print(inp, [tf.shape(inp), inp], message="encode(): inp", first_n=100, summarize=100)

            # Encode.
            enc_out, enc_attn_mask = self.transformer.encode(inp, inp_len, is_train=False)

            # Decoder dummy input/output
            ninp = tf.shape(inp)[1]
            batch_size = tf.shape(inp)[0]
            hid_size = tf.shape(enc_out)[-1]
            out_seq = tf.zeros([batch_size, 0], dtype=inp.dtype)
            rdo = tf.zeros([batch_size, hid_size], dtype=enc_out.dtype)

            attnP = tf.ones([batch_size, ninp]) / tf.to_float(inp_len)[:, None]

            offset = tf.zeros((batch_size,))
            if self.transformer.dst_rand_offset:
                BIG_LEN = 32000
                random_offset = tf.random_uniform(tf.shape(offset), minval=-BIG_LEN, maxval=BIG_LEN, dtype=tf.int32)
                offset += tf.to_float(random_offset)

            trans = self.transformer
            empty_emb = tf.zeros([batch_size, 0, trans.emb_size])
            empty_dec_layers = [tf.zeros([batch_size, 0, trans.hid_size])] * trans.num_layers_dec
            input_layers = [empty_emb] + empty_dec_layers[:-1]

            #prepare kv parts for all decoder attention layers. Note: we do not preprocess enc_out
            # for each layer because ResidualLayerWrapper only preprocesses first input (query)
            dec_enc_kv = [layer.kv_conv(enc_out)
                          for i, layer in enumerate(trans.dec_enc_attn)]
            dec_dec_kv = [layer.kv_conv(layer.preprocess(input_layers[i]))
                          for i, layer in enumerate(trans.dec_attn)]

            new_state = self.DecState(enc_out, enc_attn_mask, attnP, rdo, out_seq, offset,
                                      empty_emb, empty_dec_layers, dec_enc_kv, dec_dec_kv)

            # perform initial decode (instead of force_bos) with zero embeddings
            new_state = self.decode(new_state, is_train=is_train)
            return new_state

    def decode(self, dec_state, words=None, is_train=False, **kwargs):
        """
        Performs decoding step given words and previous state.
        Returns next state.

        :param words: previous output tokens, int32[batch_size]. if None, uses zero embeddings (first step)
        :param is_train: if True, enables dropouts
        """
        trans = self.transformer
        enc_out, enc_attn_mask, attnP, rdo, out_seq, offset, prev_emb = dec_state[:7]
        prev_dec_layers = dec_state.dec_layers
        dec_enc_kv = dec_state.dec_enc_kv
        dec_dec_kv = dec_state.dec_dec_kv

        batch_size = tf.shape(rdo)[0]
        if words is not None:
            out_seq = tf.concat([out_seq, tf.expand_dims(words, 1)], 1)

        with dropout_scope(is_train), tf.name_scope(trans.name):
            # Embeddings
            if words is None:
                # initial step: words are None
                emb_out = tf.zeros((batch_size, 1, trans.emb_size))
            else:
                emb_out = trans.emb_out(words[:, None])  # [batch_size * 1 * emb_dim]
                if trans.rescale_emb:
                    emb_out *= trans.emb_size ** .5

            # Prepare decoder
            dec_inp_t = trans._add_timing_signal(emb_out, offset=offset)
            # Apply dropouts
            if is_dropout_enabled():
                dec_inp_t = tf.nn.dropout(dec_inp_t, 1.0 - trans.res_dropout)

            # bypass info from Encoder to avoid None gradients for num_layers_dec == 0
            if trans.num_layers_dec == 0:
                inp_mask = tf.squeeze(tf.transpose(enc_attn_mask, perm=[3, 1, 2, 0]), 3)
                dec_inp_t += tf.reduce_mean(enc_out * inp_mask, axis=[0, 1], keep_dims=True)

            # Decoder
            new_emb = tf.concat([prev_emb, dec_inp_t], axis=1)
            _out = tf.pad(out_seq, [(0, 0), (0, 1)])
            dec_attn_mask = trans._make_dec_attn_mask(_out)[:, :, -1:, :]  # [1, 1, n_q=1, n_kv]

            new_dec_layers = []
            new_dec_dec_kv = []

            for layer in range(trans.num_layers_dec):
                # multi-head self-attention: use only the newest time-step as query,
                # but all time-steps up to newest one as keys/values
                next_dec_kv = trans.dec_attn[layer].kv_conv(trans.dec_attn[layer].preprocess(dec_inp_t))
                new_dec_dec_kv.append(tf.concat([dec_dec_kv[layer], next_dec_kv], axis=1))
                dec_inp_t = trans.dec_attn[layer](dec_inp_t, dec_attn_mask, kv=new_dec_dec_kv[layer])

                dec_inp_t = trans.dec_enc_attn[layer](dec_inp_t, enc_attn_mask, kv=dec_enc_kv[layer])
                dec_inp_t = trans.dec_ffn[layer](dec_inp_t)

                new_dec_inp = tf.concat([prev_dec_layers[layer], dec_inp_t], axis=1)
                new_dec_layers.append(new_dec_inp)

            if trans.normalize_out:
                dec_inp_t = trans.dec_out_norm(dec_inp_t)

            rdo = dec_inp_t[:, -1]

            new_state = self.DecState(enc_out, enc_attn_mask, attnP, rdo, out_seq, offset + 1,
                                      new_emb, new_dec_layers, dec_enc_kv, new_dec_dec_kv)
            return new_state

    def get_rdo(self, dec_state, **kwargs):
        return dec_state.rdo, dec_state.out_seq

    def get_attnP(self, dec_state, **kwargs):
        return dec_state.attnP

    def get_logits(self, dec_state, **flags):
        return self.logits(dec_state.rdo)
