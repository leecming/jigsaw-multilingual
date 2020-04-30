import os
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from scipy.stats import rankdata


def score_roc_auc(target_csv, predicted_csv):
    """
    Generates ROC AUC from CSVs with target and predicted toxic scores
    - assumes CSVs have id and toxic columns
    """
    train_df = pd.read_csv(target_csv).sort_values(by='id')
    compare_df = pd.read_csv(predicted_csv).sort_values(by='id')
    assert (train_df['id'].values == compare_df['id'].values).all()

    return roc_auc_score(train_df['toxic'].values,
                         compare_df['toxic'].values)


def ensemble_simple_avg_csv(list_csv):
    """ Given a list of CSVs with id/toxic columns, outputs a single CSV with averaged toxicity """
    base_df = pd.read_csv(list_csv[0]).sort_values('id').set_index('id')
    for i in range(1, len(list_csv)):
        base_df['toxic'] += pd.read_csv(list_csv[i]).sort_values('id').set_index('id')['toxic']
    base_df['toxic'] /= len(list_csv)
    base_df.reset_index().to_csv('data/es_preds.csv', index=False)


def ensemble_power_avg_csv(list_csv, power):
    """ Power averaging of input CSV toxicity """
    base_df = pd.read_csv(list_csv[0]).sort_values('id').set_index('id')
    base_df['toxic'] = base_df['toxic'] ** power
    for i in range(1, len(list_csv)):
        base_df['toxic'] += pd.read_csv(list_csv[i]).sort_values('id').set_index('id')['toxic'].values ** power
    base_df['toxic'] /= len(list_csv)
    base_df = base_df.reset_index()
    base_df[['id', 'toxic']].to_csv('data/power_ensemble_{}.csv'.format(len(list_csv)), index=False)


def ensemble_rank_avg_csv(list_csv):
    """ Rank averaging given list of CSVs """
    predict_list = [pd.read_csv(curr_csv).sort_values('id').set_index('id') for curr_csv in list_csv]
    predictions = np.zeros_like(predict_list[0]['toxic'])
    for predict in predict_list:
        curr_preds = rankdata(predict['toxic'].values) / predictions.shape[0]
        predictions = np.add(predictions, curr_preds)
    predictions /= len(predict_list)
    predict_list[0]['toxic'] = predictions
    predict_list[0].reset_index().to_csv('data/rank_ensemble_{}.csv'.format(len(list_csv)), index=False)


if __name__ == '__main__':
    x = sorted([os.path.join('data/outputs/test', x) for x in os.listdir('data/outputs/test')])
    x = [y for y in x if '9509es' in y]
    print(len(x))
    print(x)
    ensemble_simple_avg_csv(x)

    # ensemble_simple_avg_csv(['data/fr_camlarge_highconf_preds.csv',
    #                          'data/fr_flau_highconf_preds.csv'])
