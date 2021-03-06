"""
Monolingual classifier using Bidirectional GRU w/ pretrained FastText embeddings
"""
import time
import json
import os
import pandas as pd
import numpy as np
import tensorflow as tf
from tensorflow.keras.optimizers import Adam
import tensorflow.keras.layers as layers
from tensorflow.keras import Model
from tensorflow.python.keras.preprocessing.sequence import pad_sequences
from tensorflow.python.keras.preprocessing.text import Tokenizer
from sklearn.metrics import roc_auc_score
from preprocessor import get_id_text_label_from_csv, get_id_text_from_test_csv
from fasttext import load_model

with open('SETTINGS.json') as f:
    SETTINGS_DICT = json.load(f)

USE_LANG = 'es'  # select the right FastText language model file
TRAIN_CSV_PATH = os.path.join(SETTINGS_DICT['TRAIN_DATA_DIR'], 'curr_run_train.csv')
TEST_CSV_PATH = os.path.join(SETTINGS_DICT['TRAIN_DATA_DIR'], 'curr_run_test.csv')
VAL_CSV_PATH = os.path.join(SETTINGS_DICT['TRAIN_DATA_DIR'], 'curr_run_val.csv')
NUM_OUTPUTS = 1  # Number of targets
MAX_SEQ_LEN = 200  # max sequence length for input strings: gets padded/truncated
NUM_EPOCHS = 4
BATCH_SIZE = 32
VOCAB_SIZE = 100000  # Used to generate the embeddings matrix
EMBEDDING_DIMS = 300  # Dimensions of the FastText embedder (typically 300)
HIDDEN_UNITS = 128  # Hidden units for the Bidirectional GRU


def texts_to_padded_sequences(train_strings, val_strings, test_strings):
    """
    Use keras tokenizer set to defaults & specified vocab size to
    tokenize the training and test comments
    Then apply pre-padding with val 0.
    :return: tuple of keras Tokenizer and the train & test token sequences
    """
    tokenizer = Tokenizer(num_words=VOCAB_SIZE)
    train_val_test_comment_text = train_strings + val_strings + test_strings
    tokenizer.fit_on_texts(train_val_test_comment_text)

    train_sequences = tokenizer.texts_to_sequences(train_strings)
    train_sequences = pad_sequences(train_sequences, maxlen=MAX_SEQ_LEN)

    val_sequences = tokenizer.texts_to_sequences(val_strings)
    val_sequences = pad_sequences(val_sequences, maxlen=MAX_SEQ_LEN)

    test_sequences = tokenizer.texts_to_sequences(test_strings)
    test_sequences = pad_sequences(test_sequences, maxlen=MAX_SEQ_LEN)
    print('generated padded sequences...')

    return tokenizer, train_sequences, val_sequences, test_sequences


def generate_embedding_matrix(fitted_tokenizer):
    """
    Standard FastText sub-word wikipedia trained model
    :param fitted_tokenizer:
    :return:
    """
    ft_model = load_model(os.path.join(SETTINGS_DICT['FT_MODELS_DIR'],
                                       'cc.{}.300.bin'.format(USE_LANG)))

    embedding_matrix = np.zeros((VOCAB_SIZE + 1, EMBEDDING_DIMS))
    for i in range(1, VOCAB_SIZE + 1):
        try:
            embedding_matrix[i] = ft_model.get_word_vector(fitted_tokenizer.index_word[i])
        except KeyError:
            print('FastText OOV?')

    print('generated ft embeddings...')
    return embedding_matrix


def build_classifier_model(embedding_matrix):
    input = layers.Input(shape=(MAX_SEQ_LEN,), dtype=np.int32)

    embedding_layer = layers.Embedding(VOCAB_SIZE + 1,
                                       EMBEDDING_DIMS,
                                       weights=[embedding_matrix],
                                       trainable=False)
    embedded_input = embedding_layer(input)
    gru_output = layers.Bidirectional(layers.GRU(HIDDEN_UNITS,
                                                 return_sequences=True))(embedded_input)
    gru_output = layers.Bidirectional(layers.GRU(HIDDEN_UNITS))(gru_output)
    prob = layers.Dense(NUM_OUTPUTS, activation='sigmoid')(gru_output)

    bigru_model = Model(input, prob)
    print('generated bigru model...')
    return bigru_model


def train_driver(train_tuple,
                 val_tuple,
                 test_tuple,
                 embedding_matrix):
    train_features, train_labels = train_tuple
    val_features, val_labels = val_tuple
    test_features, test_ids = test_tuple

    classifier = build_classifier_model(embedding_matrix)
    opt = Adam()
    opt = tf.keras.mixed_precision.experimental.LossScaleOptimizer(opt, 'dynamic')
    classifier.compile(optimizer=opt, loss='binary_crossentropy')

    for curr_epoch in range(NUM_EPOCHS):
        classifier.fit(train_features, train_labels,
                       batch_size=BATCH_SIZE,
                       epochs=1,
                       verbose=1)

        if len(val_labels):
            val_preds = classifier.predict(val_features)
            val_roc_auc_score = roc_auc_score(val_labels, val_preds)
            print(val_roc_auc_score)

        test_preds = classifier.predict(test_features).squeeze()
        curr_test_path = os.path.join(SETTINGS_DICT['PREDICTION_DIR'],
                                      '{}.csv'.format(curr_epoch))
        pd.DataFrame({'id': test_ids, 'toxic': test_preds}) \
            .to_csv(curr_test_path,
                    index=False)


if __name__ == '__main__':
    start_time = time.time()
    # Load train, validation, and pseudo-label data
    train_ids, train_strings, train_labels = get_id_text_label_from_csv(TRAIN_CSV_PATH,
                                                                        text_col='comment_text')
    val_ids, val_strings, val_labels = get_id_text_label_from_csv(VAL_CSV_PATH,
                                                                  text_col='comment_text',
                                                                  lang=USE_LANG)
    test_ids, test_strings = get_id_text_from_test_csv(TEST_CSV_PATH, text_col='comment_text')

    (tokenizer, train_features, val_features, test_features) \
        = texts_to_padded_sequences(train_strings, val_strings, test_strings)

    print(train_features.shape, val_features.shape, test_features.shape)

    pretrained_embedding_matrix = generate_embedding_matrix(tokenizer)

    train_driver([train_features, train_labels],
                 [val_features, val_labels],
                 [test_features, test_ids],
                 pretrained_embedding_matrix)

    print('Elapsed time: {}'.format(time.time() - start_time))
