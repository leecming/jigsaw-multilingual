import numpy as np
import pandas as pd
from functools import lru_cache
from sklearn.model_selection import KFold
from scipy.stats import truncnorm
from random import random

SEED = 1337
NUM_FOLDS = 4
TOXIC_TARGET_COLS = ['toxic', 'severe_toxic', 'obscene', 'threat', 'insult', 'identity_hate']
LANG_MAPPING = {lang: np.identity(7)[i] for i, lang in enumerate(['en', 'tr', 'pt', 'ru', 'fr', 'it', 'es'])}


def generate_train_kfolds_indices(input_df):
    """
    Seeded kfolds cross validation indices using just a range(len) call
    :return: (training index, validation index)-tuple list
    """
    seeded_kf = KFold(n_splits=NUM_FOLDS, shuffle=True)
    return [(train_index, val_index) for train_index, val_index in
            seeded_kf.split(range(len(input_df)))]


def get_id_text_label_from_csv(csv_path, text_col='comment_text',
                               sample_frac=1.,
                               add_label=None,
                               lang=None):
    """
    Load training data
    """
    raw_df = pd.read_csv(csv_path)
    if lang is not None:
        raw_df = raw_df[raw_df['lang'] == lang]
    if sample_frac < 1:
        raw_df = raw_df.sample(frac=sample_frac)
    if add_label is None:
        return raw_df['id'].values, list(raw_df[text_col].values), raw_df['toxic'].values
    else:
        return raw_df['id'].values, \
               list(raw_df[text_col].values), \
               raw_df['toxic'].values, np.full(raw_df.shape[0], add_label)


def get_translation_pair_from_csv(csv_path,
                                  raw_text_col='comment_text',
                                  en_text_col='comment_text_en',
                                  sample_frac=1.):
    """ Returns both raw and translated comments  """
    raw_df = pd.read_csv(csv_path)
    if sample_frac < 1:
        raw_df = raw_df.sample(frac=sample_frac)
    return (raw_df['id'].values,
            list(raw_df[raw_text_col].values),
            list(raw_df[en_text_col].values),
            raw_df['toxic'].values)


def get_balanced_id_text_label_from_csv(csv_path, text_col='comment_text',
                                        sample=None,
                                        add_label=None):
    """
    Load balanced dataset - 0.5 * sample from positives, 0.5 from negatives w/ replacement
    :param csv_path: path of csv with 'id' 'comment_text', 'toxic' columns present
    :param sample: NUMBER of samples to draw
    :return:
    """
    raw_df = pd.read_csv(csv_path)
    if sample is not None:
        positive_df, negative_df = raw_df[raw_df.toxic == 1], raw_df[raw_df.toxic == 0]
        raw_df = pd.concat([positive_df.sample(n=sample//2, replace=True),
                            negative_df.sample(n=sample//2, replace=True)])
    if add_label is None:
        return raw_df['id'].values, list(raw_df[text_col].values), raw_df['toxic'].values
    else:
        return raw_df['id'].values, \
               list(raw_df[text_col].values), \
               raw_df['toxic'].values, np.full(raw_df.shape[0], add_label)


def get_id_text_from_test_csv(csv_path, text_col):
    """
    Load test data
    :param csv_path: path of csv with 'id' 'comment_text' columns present
    :param text_col: column w/ test
    :return:
    """
    raw_pdf = pd.read_csv(csv_path)
    return raw_pdf['id'].values, list(raw_pdf[text_col].values)


@lru_cache(maxsize=None)
def generate_target_dist(mean, num_bins, low, high):
    """
    Generate discretized truncated norm prob distribution centered around mean
    :param mean: center of truncated norm
    :param num_bins: number of bins
    :param low: low end of truncated range
    :param high: top end of truncated range
    :return: (support, probabilities for support) tuple
    """
    radius = 0.5 * (high - low) / num_bins

    def trunc_norm_prob(center):
        """ get probability mass """
        return (truncnorm.cdf(center + radius,
                              a=(low - mean) / radius,
                              b=(high - mean) / radius,
                              loc=mean, scale=radius) -
                truncnorm.cdf(center - radius,
                              a=(low - mean) / radius,
                              b=(high - mean) / radius,
                              loc=mean, scale=radius))

    supports = np.array([x * (2 * radius) + radius + low for x in range(num_bins)])
    probs = np.array([trunc_norm_prob(support) for support in supports])
    return supports, probs


def tokenize(self, text):
    """
    Modified version of tokenize in transformers tokenization_bert.py
    - Monkeypatch by replacing a tokenizer instance's Wordpiece Tokenizer's tokenize function
    - to monkey patch:
        <tokenizer instance>.wordpiece_tokenizer.tokenize =
                tokenize.__get__(<instance>.wordpiece_tokenizer, WordpieceTokenizer)
    - implements BPE encode by failing substring to vocab matches with prob 0.1
    """
    def whitespace_tokenize(text):
        """Runs basic whitespace cleaning and splitting on a piece of text."""
        text = text.strip()
        if not text:
            return []
        tokens = text.split()
        return tokens

    output_tokens = []
    for token in whitespace_tokenize(text):
        chars = list(token)
        if len(chars) > self.max_input_chars_per_word:
            output_tokens.append(self.unk_token)
            continue

        is_bad = False
        start = 0
        sub_tokens = []
        while start < len(chars):
            end = len(chars)
            cur_substr = None
            while start < end:
                substr = "".join(chars[start:end])
                if start > 0:
                    substr = "##" + substr
                # Fail vocab lookup with prob 0.1
                if substr in self.vocab and random() > 0.1:
                    cur_substr = substr
                    break
                end -= 1
            if cur_substr is None:
                is_bad = True
                break
            sub_tokens.append(cur_substr)
            start = end

        if is_bad:
            output_tokens.append(self.unk_token)
        else:
            output_tokens.extend(sub_tokens)
    return output_tokens


if __name__ == '__main__':
    ids, strings, labels = get_balanced_id_text_label_from_csv('data/translated_2018/combined.csv', sample=100)
    print(pd.DataFrame({'id': ids, 'label': labels}))
