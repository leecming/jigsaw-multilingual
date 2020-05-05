"""
"""
import os
import time
from random import shuffle
from functools import partial
import multiprocessing as mp
import pandas as pd
import numpy as np
import torch
from transformers import AutoTokenizer, AutoModel, AutoConfig
from sklearn.metrics import roc_auc_score
from apex import amp
from tqdm import trange
from preprocessor import get_id_text_label_from_csv, get_id_text_from_test_csv
from torch_helpers import save_model

RUN_NAME = '9536es_dual_model'  # added as prefix to file outputs
PREDICT = True  # Make predictions against TEST_CSV_PATH test features
SAVE_MODEL = False  # Saves model at end of every epoch to MODEL_SAVE_DIR
USE_VAL_LANG = 'es'  # if set to ISO lang str (e.g., "es") - only pulls that language's validation samples
PRETRAINED_MODEL_1 = 'mrm8488/distill-bert-base-spanish-wwm-cased-finetuned-spa-squad2-es'
PRETRAINED_MODEL_2 = 'dccuchile/bert-base-spanish-wwm-uncased'
TRAIN_SAMPLE_FRAC = 1.  # what proportion of training data (from TRAIN_CSV_PATH) to sample
TRAIN_CSV_PATH = 'data/es_all.csv'
TEST_CSV_PATH = 'data/es_test.csv'
VAL_CSV_PATH = 'data/validation.csv'
MODEL_SAVE_DIR = 'models/{}'.format(RUN_NAME)
MAX_CORES = 24  # limit MP calls to use this # cores at most; for tokenizing
BASE_MODEL_OUTPUT_DIM = 768 * 2  # hidden layer dimensions
NUM_OUTPUTS = 1  # Num of output units (should be 1 for Toxicity)
MAX_SEQ_LEN = 200  # max sequence length for input strings: gets padded/truncated
NUM_EPOCHS = 6
# Gradient Accumulation: updates every ACCUM_FOR steps so that effective BS = BATCH_SIZE * ACCUM_FOR
BATCH_SIZE = 64
ACCUM_FOR = 1
LR = 1e-5  # Learning rate - constant value

# For multi-gpu environments - make only 1 GPU visible to process
os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
os.environ['CUDA_VISIBLE_DEVICES'] = '1'


class ClassifierHead(torch.nn.Module):
    """
    Bert base with a Linear layer plopped on top of it
    - connects the max pool of the last hidden layer with the FC
    """

    def __init__(self, base_model_1, base_model_2):
        super(ClassifierHead, self).__init__()
        self.base_model_1 = base_model_1
        self.base_model_2 = base_model_2
        self.cnn = torch.nn.Conv1d(BASE_MODEL_OUTPUT_DIM, NUM_OUTPUTS, kernel_size=1)
        self.fc = torch.nn.Linear(BASE_MODEL_OUTPUT_DIM, NUM_OUTPUTS)

    def forward(self, x, y):
        output_1 = self.base_model_1(x)[0][:, 0, :]
        output_2 = self.base_model_2(y)[0][:, 0, :]
        combined_output = torch.cat((output_1, output_2), 1)
        logits = self.fc(combined_output)
        prob = torch.nn.Sigmoid()(logits)
        return prob


def train(model, train_tuple, loss_fn, opt, curr_epoch):
    """
    Trains against the train_tuple features for a single epoch
    """
    # Shuffle train indices for current epoch, batching
    all_features, all_labels, all_ids = train_tuple
    train_indices = list(range(len(all_labels)))

    shuffle(train_indices)
    train_features = [all_features[0][train_indices], all_features[1][train_indices]]
    train_labels = all_labels[train_indices]

    model.train()
    iter = 0
    running_total_loss = 0  # Display running average of loss across epoch
    with trange(0, len(train_indices), BATCH_SIZE,
                desc='Epoch {}'.format(curr_epoch)) as t:
        for batch_idx_start in t:
            iter += 1
            batch_idx_end = min(batch_idx_start + BATCH_SIZE, len(train_indices))

            batch_encoder_features = torch.tensor(train_features[0][batch_idx_start:batch_idx_end]).cuda()
            batch_decoder_features = torch.tensor(train_features[1][batch_idx_start:batch_idx_end]).cuda()
            batch_labels = torch.tensor(train_labels[batch_idx_start:batch_idx_end]).float().cuda().unsqueeze(-1)

            preds = model(batch_encoder_features, batch_decoder_features)
            loss = loss_fn(preds, batch_labels)
            loss = loss / ACCUM_FOR  # Normalize if we're doing GA

            with amp.scale_loss(loss, opt) as scaled_loss:
                scaled_loss.backward()

            running_total_loss += loss.detach().cpu().numpy()
            t.set_postfix(loss=running_total_loss / iter)

            if iter % ACCUM_FOR == 0:
                opt.step()
                opt.zero_grad()


def predict_evaluate(model, data_tuple, epoch, score=False):
    """
    Make predictions against either val or test set
    Saves output to csv in data/outputs/test or data/outputs/validation
    """
    val_score = None
    val_preds = []
    model.eval()
    with torch.no_grad():
        for batch_idx_start in range(0, len(data_tuple[-1]), BATCH_SIZE):
            batch_idx_end = min(batch_idx_start + BATCH_SIZE, len(data_tuple[-1]))
            batch_features_1 = torch.tensor(data_tuple[0][0][batch_idx_start:batch_idx_end]).cuda()
            batch_features_2 = torch.tensor(data_tuple[0][1][batch_idx_start:batch_idx_end]).cuda()
            batch_preds = model(batch_features_1, batch_features_2)
            val_preds.append(batch_preds.cpu().numpy().squeeze())

    val_preds = np.concatenate(val_preds)
    if score:
        val_score = roc_auc_score(np.round(data_tuple[1]), val_preds)

    save_folder = 'validation' if score else 'test'
    pd.DataFrame({'id': data_tuple[-1], 'toxic': val_preds}) \
        .to_csv('data/outputs/{}/{}_{}.csv'.format(save_folder,
                                                   RUN_NAME,
                                                   epoch),
                index=False)

    return val_score


def main_driver(train_tuple, val_tuple, test_tuple, tokenizer):
    pretrained_config = AutoConfig.from_pretrained(PRETRAINED_MODEL_1,
                                                   output_hidden_states=True)
    pretrained_base_1 = AutoModel.from_pretrained(PRETRAINED_MODEL_1).cuda()
    pretrained_base_2 = AutoModel.from_pretrained(PRETRAINED_MODEL_2).cuda()
    classifier = ClassifierHead(pretrained_base_1,
                                pretrained_base_2).cuda()
    loss_fn = torch.nn.BCELoss()
    opt = torch.optim.Adam(classifier.parameters(), lr=LR)

    amp.register_float_function(torch, 'sigmoid')
    classifier, opt = amp.initialize(classifier, opt, opt_level='O1', verbosity=0)
    list_auc = []

    current_tuple = train_tuple
    for curr_epoch in range(NUM_EPOCHS):
        # After half epochs, switch to training against validation set
        if curr_epoch == NUM_EPOCHS // 2:
            current_tuple = val_tuple
        train(classifier, current_tuple, loss_fn, opt, curr_epoch)

        epoch_raw_auc = predict_evaluate(classifier, val_tuple, curr_epoch, score=True)
        print('Epoch {} - Raw: {:.4f}'.format(curr_epoch, epoch_raw_auc))
        list_auc.append(epoch_raw_auc)

        if PREDICT:
            predict_evaluate(classifier, test_tuple, curr_epoch)

        if SAVE_MODEL:
            save_model(os.path.join(MODEL_SAVE_DIR, str(curr_epoch)), classifier, pretrained_config, tokenizer)

    with np.printoptions(precision=4, suppress=True):
        print(np.array(list_auc))

    pd.DataFrame({'val_auc': list_auc}).to_csv('data/outputs/results/{}.csv'.format(RUN_NAME), index=False)


if __name__ == '__main__':
    def cln(x):  # Truncates adjacent whitespaces to single whitespace
        return ' '.join(x.split())


    start_time = time.time()
    print(RUN_NAME)

    # Load train, validation, and pseudo-label data
    train_ids, train_strings, train_labels = get_id_text_label_from_csv(TRAIN_CSV_PATH,
                                                                        text_col='comment_text',
                                                                        sample_frac=TRAIN_SAMPLE_FRAC)
    print(train_strings[0])
    train_strings = [cln(x) for x in train_strings]
    print(train_strings[0])

    val_ids, val_strings, val_labels = get_id_text_label_from_csv(VAL_CSV_PATH,
                                                                  text_col='comment_text',
                                                                  lang=USE_VAL_LANG)
    val_strings = [cln(x) for x in val_strings]

    test_ids, test_strings = get_id_text_from_test_csv(TEST_CSV_PATH, text_col='comment_text')
    test_strings = [cln(x) for x in test_strings]

    # use MP to batch encode the raw feature strings into Bert token IDs
    tokenizer_1 = AutoTokenizer.from_pretrained(PRETRAINED_MODEL_1)
    encode_partial_1 = partial(tokenizer_1.encode,
                               max_length=MAX_SEQ_LEN,
                               pad_to_max_length=True,
                               add_special_tokens=True)

    tokenizer_2 = AutoTokenizer.from_pretrained(PRETRAINED_MODEL_2)
    encode_partial_2 = partial(tokenizer_2.encode,
                               max_length=MAX_SEQ_LEN,
                               pad_to_max_length=True,
                               add_special_tokens=True)
    print('Encoding raw strings into model-specific tokens')
    with mp.Pool(MAX_CORES) as p:
        train_features = [np.array(p.map(encode_partial_1, train_strings)),
                          np.array(p.map(encode_partial_2, train_strings))]
        val_features = [np.array(p.map(encode_partial_1, val_strings)),
                        np.array(p.map(encode_partial_2, val_strings))]
        test_features = [np.array(p.map(encode_partial_1, test_strings)),
                         np.array(p.map(encode_partial_2, test_strings))]

    print('Train size: {}, val size: {}'.format(len(train_ids), len(val_ids)))

    main_driver([train_features, train_labels, train_ids],
                [val_features, val_labels, val_ids],
                [test_features, test_ids],
                tokenizer_1)

    print('Elapsed time: {}'.format(time.time() - start_time))
