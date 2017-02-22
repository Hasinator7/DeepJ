# Defines the models used in the experiments

import numpy as np
from keras.layers import Dense, Input, merge, Activation, Dropout, Flatten, Lambda
from keras.models import Model
from keras.layers.convolutional import AtrousConvolution1D, Convolution1D
from keras.layers.recurrent import GRU
from keras.layers.normalization import BatchNormalization
from keras import backend as K
from keras.utils.np_utils import conv_output_length

from util import one_hot
from constants import NUM_STYLES
from music import NUM_CLASSES, NOTES_PER_BAR, NUM_KEYS
from keras.models import load_model

class CausalAtrousConvolution1D(AtrousConvolution1D):
    def __init__(self, nb_filter, filter_length, init='glorot_uniform', activation=None, weights=None,
                 border_mode='valid', subsample_length=1, atrous_rate=1, W_regularizer=None, b_regularizer=None,
                 activity_regularizer=None, W_constraint=None, b_constraint=None, bias=True, causal=False, **kwargs):
        super(CausalAtrousConvolution1D, self).__init__(nb_filter, filter_length, init, activation, weights,
                                                        border_mode, subsample_length, atrous_rate, W_regularizer,
                                                        b_regularizer, activity_regularizer, W_constraint, b_constraint,
                                                        bias, **kwargs)
        self.causal = causal
        if self.causal and border_mode != 'valid':
            raise ValueError("Causal mode dictates border_mode=valid.")

    def get_output_shape_for(self, input_shape):
        input_length = input_shape[1]

        if self.causal:
            input_length += self.atrous_rate * (self.filter_length - 1)

        length = conv_output_length(input_length,
                                    self.filter_length,
                                    self.border_mode,
                                    self.subsample[0],
                                    dilation=self.atrous_rate)

        return (input_shape[0], length, self.nb_filter)

    def call(self, x, mask=None):
        if self.causal:
            x = K.asymmetric_temporal_padding(x, self.atrous_rate * (self.filter_length - 1), 0)
        return super(CausalAtrousConvolution1D, self).call(x, mask)

def residual_block(x, nb_filters, s, dilation):
    original_x = x
    # Tanh + Sigmoid gating
    """
    tanh_out = CausalAtrousConvolution1D(nb_filters, 2, atrous_rate=2 ** dilation, causal=True,
                                         name='dilated_conv_%d_tanh_s%d' % (2 ** dilation, s), activation='tanh')(x)
    tanh_out = BatchNormalization()(tanh_out)

    sigm_out = CausalAtrousConvolution1D(nb_filters, 2, atrous_rate=2 ** dilation, causal=True,
                                         name='dilated_conv_%d_sigm_s%d' % (2 ** dilation, s), activation='sigmoid')(x)
    sigm_out = BatchNormalization()(sigm_out)

    x = merge([tanh_out, sigm_out], mode='mul', name='gated_activation_%d_s%d' % (dilation, s))
    """
    # ReLU Alternative
    x = CausalAtrousConvolution1D(nb_filters, 2, atrous_rate=2 ** dilation, causal=True, name='dilated_conv_%d_tanh_s%d' % (2 ** dilation, s), activation='tanh')(x)
    x = BatchNormalization()(x)
    x = Activation('relu')(x)

    res_x = Convolution1D(nb_filters, 1, border_mode='same')(x)
    res_x = BatchNormalization()(res_x)
    skip_x = Convolution1D(nb_filters, 1, border_mode='same')(x)
    skip_x = BatchNormalization()(skip_x)

    res_x = merge([original_x, res_x], mode='sum')
    return res_x, skip_x

def supervised_model(time_steps, nb_stacks=4, dilation_depth=4, nb_filters=32, nb_output_bins=NUM_CLASSES):
    # Primary input
    note_input = Input(shape=(time_steps, NUM_CLASSES), name='note_input')

    # Context inputs
    # beat_input = Input(shape=(time_steps, NOTES_PER_BAR), name='beat_input')
    beat_input = Input(shape=(time_steps, 2), name='beat_input')
    completion_input = Input(shape=(time_steps, 1), name='completion_input')
    style_input = Input(shape=(time_steps, NUM_STYLES), name='style_input')
    context = merge([completion_input, beat_input, style_input], mode='concat')

    # Create a distributerd representation of context
    for i in range(2):
        context = GRU(nb_output_bins, return_sequences=True)(context)
        context = BatchNormalization()(context)
        context = Activation('relu')(context)

    out = note_input
    out = CausalAtrousConvolution1D(nb_filters, 2, atrous_rate=1, border_mode='valid',
                                    causal=True, name='initial_causal_conv')(out)
    skip_connections = []

    for s in range(nb_stacks):
        for i in range(dilation_depth + 1):
            out, skip_out = residual_block(out, nb_filters, s, i)
            skip_connections.append(skip_out)

    # TODO: This is optinal. Experiment with it...
    out = merge(skip_connections, mode='sum')

    for i in range(3):
        if i > 0:
            # Combine contextual inputs
            out = merge([context, out], mode='sum')
        out = Convolution1D(nb_output_bins, 1, border_mode='same')(out)
        context = BatchNormalization()(context)
        out = Activation('relu')(out)

    out = Convolution1D(nb_output_bins, 1, border_mode='same')(out)
    # TODO: Not efficient to learn one thing at a time.
    # out = Lambda(lambda x: x[:, -1, :], output_shape=(out._keras_shape[-1],))(out)
    out = Activation('softmax')(out)

    model = Model([note_input, beat_input, completion_input, style_input], out)
    model.compile(
        optimizer='adam',
        loss='categorical_crossentropy',
        metrics=['accuracy']
    )

    return model


def note_model(time_steps):
    inputs, x = pre_model(time_steps, False)

    # Multi-label
    policy = Dense(NUM_CLASSES, name='policy', activation='softmax')(x)
    value = Dense(1, name='value', activation='linear')(x)

    model = Model(inputs, [policy, value])
    #model.load_weights('data/supervised.h5', by_name=True)
    # Create value output
    return model

def note_preprocess(env, x):
    note, beat = x
    return (one_hot(note, NUM_CLASSES), one_hot(beat, NOTES_PER_BAR))
