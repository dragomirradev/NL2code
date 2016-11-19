import theano
import theano.tensor as T
import numpy as np
import logging

from nn.layers.embeddings import Embedding
from nn.layers.core import Dense, Layer
from nn.layers.recurrent import BiLSTM, LSTM, CondAttLSTM
from nn.utils.theano_utils import ndim_itensor, tensor_right_shift, ndim_tensor, alloc_zeros_matrix, shared_zeros
import nn.initializations as initializations
import nn.activations as activations
import nn.optimizers as optimizers

from config import *
from grammar import *
from parse import *
from tree import *


class PointerNet(Layer):
    def __init__(self, name='PointerNet'):
        super(PointerNet, self).__init__()

        self.dense1_input = Dense(QUERY_DIM, POINTER_NET_HIDDEN_DIM, activation='linear', name='Dense1_input')

        self.dense1_h = Dense(LSTM_STATE_DIM, POINTER_NET_HIDDEN_DIM, activation='linear', name='Dense1_h')

        self.dense2 = Dense(POINTER_NET_HIDDEN_DIM, 1, activation='linear', name='Dense2')

        self.params += self.dense1_input.params + self.dense1_h.params + self.dense2.params

        self.set_name(name)

    def __call__(self, query_embed, query_token_embed_mask, decoder_hidden_states):
        query_embed_trans = self.dense1_input(query_embed)
        h_trans = self.dense1_h(decoder_hidden_states)

        query_embed_trans = query_embed_trans.dimshuffle((0, 'x', 1, 2))
        h_trans = h_trans.dimshuffle((0, 1, 'x', 2))

        # (batch_size, max_decode_step, query_token_num, ptr_net_hidden_dim)
        dense1_trans = T.tanh(query_embed_trans + h_trans)

        scores = self.dense2(dense1_trans).flatten(3)

        scores = T.exp(scores - T.max(scores, axis=-1, keepdims=True))
        scores *= query_token_embed_mask.dimshuffle((0, 'x', 1))
        scores = scores / T.sum(scores, axis=-1, keepdims=True)

        return scores

class Hyp:
    def __init__(self, tree=None):
        if not tree:
            self.tree = Tree('root')
        else:
            self.tree = tree

        self.score = 0.0

    def __repr__(self):
        return self.tree.__repr__()

    @staticmethod
    def can_expand(node):
        if node.holds_value and \
                (node.label and node.label.endswith('<eos>')):
            return False
        elif node.type == 'epsilon':
            return False
        elif is_terminal_ast_type(node.type):
            return False

        # if node.type == 'root':
        #     return True
        # elif inspect.isclass(node.type) and issubclass(node.type, ast.AST) and not is_terminal_ast_type(node.type):
        #     return True
        # elif node.holds_value and not node.label.endswith('<eos>'):
        #     return True

        return True

    def frontier_nt_helper(self, node):
        if node.is_leaf:
            if Hyp.can_expand(node):
                return node
            else:
                return None

        for child in node.children:
            result = self.frontier_nt_helper(child)
            if result:
                return result

        return None

    def frontier_nt(self):
        return self.frontier_nt_helper(self.tree)

class CondAttLSTM(Layer):
    """
    Conditional LSTM with Attention
    """
    def __init__(self, input_dim, output_dim,
                 context_dim, att_hidden_dim,
                 init='glorot_uniform', inner_init='orthogonal', forget_bias_init='one',
                 activation='tanh', inner_activation='sigmoid', name='CondAttLSTM'):

        super(CondAttLSTM, self).__init__()

        self.output_dim = output_dim
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.forget_bias_init = initializations.get(forget_bias_init)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.context_dim = context_dim
        self.input_dim = input_dim

        # regular LSTM layer

        self.W_i = self.init((input_dim, self.output_dim))
        self.U_i = self.inner_init((self.output_dim, self.output_dim))
        self.C_i = self.inner_init((self.context_dim, self.output_dim))
        self.H_i = self.inner_init((self.output_dim, self.output_dim))
        self.b_i = shared_zeros((self.output_dim))

        self.W_f = self.init((input_dim, self.output_dim))
        self.U_f = self.inner_init((self.output_dim, self.output_dim))
        self.C_f = self.inner_init((self.context_dim, self.output_dim))
        self.H_f = self.inner_init((self.output_dim, self.output_dim))
        self.b_f = self.forget_bias_init((self.output_dim))

        self.W_c = self.init((input_dim, self.output_dim))
        self.U_c = self.inner_init((self.output_dim, self.output_dim))
        self.C_c = self.inner_init((self.context_dim, self.output_dim))
        self.H_c = self.inner_init((self.output_dim, self.output_dim))
        self.b_c = shared_zeros((self.output_dim))

        self.W_o = self.init((input_dim, self.output_dim))
        self.U_o = self.inner_init((self.output_dim, self.output_dim))
        self.C_o = self.inner_init((self.context_dim, self.output_dim))
        self.H_o = self.inner_init((self.output_dim, self.output_dim))
        self.b_o = shared_zeros((self.output_dim))

        self.params = [
            self.W_i, self.U_i, self.b_i, self.C_i, self.H_i,
            self.W_c, self.U_c, self.b_c, self.C_c, self.H_c,
            self.W_f, self.U_f, self.b_f, self.C_f, self.H_f,
            self.W_o, self.U_o, self.b_o, self.C_o, self.H_o
        ]

        # attention layer
        self.att_ctx_W1 = self.init((context_dim, att_hidden_dim))
        self.att_h_W1 = self.init((output_dim, att_hidden_dim))
        self.att_b1 = shared_zeros((att_hidden_dim))

        self.att_W2 = self.init((att_hidden_dim, 1))
        self.att_b2 = shared_zeros((1))

        self.params += [
            self.att_ctx_W1, self.att_h_W1, self.att_b1,
            self.att_W2, self.att_b2
        ]

        # attention over history
        self.hatt_h_W1 = self.init((output_dim, att_hidden_dim))
        self.hatt_hist_W1 = self.init((output_dim, att_hidden_dim))
        self.hatt_b1 = shared_zeros((att_hidden_dim))

        self.hatt_W2 = self.init((att_hidden_dim, 1))
        self.hatt_b2 = shared_zeros((1))

        self.params += [
            self.hatt_h_W1, self.hatt_hist_W1, self.hatt_b1,
            self.hatt_W2, self.hatt_b2
        ]

        self.set_name(name)

    def _step(self,
              xi_t, xf_t, xo_t, xc_t, mask_t,
              h_tm1, c_tm1,
              context, context_mask, context_att_trans,
              hist_h, hist_h_att_trans,
              b_u):

        # context: (batch_size, context_size, context_dim)

        # (batch_size, att_layer1_dim)
        h_tm1_att_trans = T.dot(h_tm1, self.att_h_W1)

        # (batch_size, context_size, att_layer1_dim)
        att_hidden = T.tanh(context_att_trans + h_tm1_att_trans[:, None, :])

        # (batch_size, context_size, 1)
        att_raw = T.dot(att_hidden, self.att_W2) + self.att_b2

        # (batch_size, context_size)
        ctx_att = T.exp(att_raw).reshape((att_raw.shape[0], att_raw.shape[1]))

        if context_mask:
            ctx_att = ctx_att * context_mask

        ctx_att = ctx_att / T.sum(ctx_att, axis=-1, keepdims=True)

        # (batch_size, context_dim)
        ctx_vec = T.sum(context * ctx_att[:, :, None], axis=1)

        ##### attention over history #####

        if hist_h:
            hist_h = T.stack(hist_h).dimshuffle((1, 0, 2))
            hist_h_att_trans = T.stack(hist_h_att_trans).dimshuffle((1, 0, 2))
            h_tm1_hatt_trans = T.dot(h_tm1, self.hatt_h_W1)

            hatt_hidden = T.tanh(hist_h_att_trans + h_tm1_hatt_trans[:, None, :])
            hatt_raw = T.dot(hatt_hidden, self.hatt_W2) + self.hatt_b2
            hatt_raw = hatt_raw.flatten(2)
            h_att_weights = T.nnet.softmax(hatt_raw)

            # (batch_size, output_dim)
            h_ctx_vec = T.sum(hist_h * h_att_weights[:, :, None], axis=1)
        else:
            h_ctx_vec = T.zeros_like(h_tm1)

        ##### attention over history #####

        i_t = self.inner_activation(xi_t + T.dot(h_tm1 * b_u[0], self.U_i) + T.dot(ctx_vec, self.C_i) + T.dot(h_ctx_vec, self.H_i))
        f_t = self.inner_activation(xf_t + T.dot(h_tm1 * b_u[1], self.U_f) + T.dot(ctx_vec, self.C_f) + T.dot(h_ctx_vec, self.H_f))
        c_t = f_t * c_tm1 + i_t * self.activation(xc_t + T.dot(h_tm1 * b_u[2], self.U_c) + T.dot(ctx_vec, self.C_c) + T.dot(h_ctx_vec, self.H_c))
        o_t = self.inner_activation(xo_t + T.dot(h_tm1 * b_u[3], self.U_o) + T.dot(ctx_vec, self.C_o) + T.dot(h_ctx_vec, self.H_o))
        h_t = o_t * self.activation(c_t)

        h_t = (1 - mask_t) * h_tm1 + mask_t * h_t
        c_t = (1 - mask_t) * c_tm1 + mask_t * c_t

        # ctx_vec = theano.printing.Print('ctx_vec')(ctx_vec)

        return h_t, c_t, ctx_vec

    def __call__(self, X, context, init_state=None, init_cell=None,
                 mask=None, context_mask=None,
                 dropout=0, train=True, srng=None,
                 timestep=1):
        assert context_mask.dtype == 'int8', 'context_mask is not int8, got %s' % context_mask.dtype

        # (n_timestep, batch_size)
        mask = self.get_mask(mask, X)
        # (n_timestep, batch_size, input_dim)
        X = X.dimshuffle((1, 0, 2))

        retain_prob = 1. - dropout
        B_w = np.ones((4,), dtype=theano.config.floatX)
        B_u = np.ones((4,), dtype=theano.config.floatX)
        if dropout > 0:
            logging.info('applying dropout with p = %f', dropout)
            if train:
                B_w = srng.binomial((4, X.shape[1], self.input_dim), p=retain_prob,
                                    dtype=theano.config.floatX)
                B_u = srng.binomial((4, X.shape[1], self.output_dim), p=retain_prob,
                                    dtype=theano.config.floatX)
            else:
                B_w *= retain_prob
                B_u *= retain_prob

        # (n_timestep, batch_size, output_dim)
        xi = T.dot(X * B_w[0], self.W_i) + self.b_i
        xf = T.dot(X * B_w[1], self.W_f) + self.b_f
        xc = T.dot(X * B_w[2], self.W_c) + self.b_c
        xo = T.dot(X * B_w[3], self.W_o) + self.b_o

        # (batch_size, context_size, att_layer1_dim)
        context_att_trans = T.dot(context, self.att_ctx_W1) + self.att_b1

        if init_state:
            # (batch_size, output_dim)
            first_state = T.unbroadcast(init_state, 1)
        else:
            first_state = T.unbroadcast(alloc_zeros_matrix(X.shape[1], self.output_dim), 1)

        if init_cell:
            # (batch_size, output_dim)
            first_cell = T.unbroadcast(init_cell, 1)
        else:
            first_cell = T.unbroadcast(alloc_zeros_matrix(X.shape[1], self.output_dim), 1)

        hist_h = []
        hist_h_att_trans = []
        ctx_vectors = []
        cells = []

        prev_state = first_state
        prev_cell = first_cell

        for t in xrange(timestep):
            xi_t = xi[t]
            xf_t = xf[t]
            xo_t = xo[t]
            xc_t = xc[t]
            mask_t = mask[t]

            h_t, c_t, ctx_vec = self._step(xi_t, xf_t, xo_t, xc_t, mask_t, prev_state, prev_cell,
                                           context, context_mask, context_att_trans,
                                           hist_h, hist_h_att_trans,
                                           B_u)

            h_att_trans = T.dot(h_t, self.hatt_hist_W1) + self.hatt_b1
            hist_h_att_trans.append(h_att_trans)

            hist_h.append(h_t)
            cells.append(c_t)
            ctx_vectors.append(ctx_vec)
            prev_state = h_t
            prev_cell = c_t

        outputs = T.stack(hist_h).dimshuffle((1, 0, 2))
        ctx_vectors = T.stack(ctx_vectors).dimshuffle((1, 0, 2))
        cells = T.stack(cells).dimshuffle((1, 0, 2))

        return outputs, cells, ctx_vectors
        # return outputs[-1]

    def get_mask(self, mask, X):
        if mask is None:
            mask = T.ones((X.shape[0], X.shape[1]))

        mask = T.shape_padright(mask)  # (nb_samples, time, 1)
        mask = T.addbroadcast(mask, -1)  # (time, nb_samples, 1) matrix.
        mask = mask.dimshuffle(1, 0, 2)  # (time, nb_samples, 1)
        mask = mask.astype('int8')

        return mask