import pickle as pickle
import os
import pandas as pd
import torch
from collections import defaultdict
from transformers import Trainer
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import LambdaLR
import torch.nn.functional as F
import sklearn
from sklearn.metrics import accuracy_score, recall_score, precision_score, f1_score
import numpy as np
import re
import wandb

class RE_Dataset(torch.utils.data.Dataset):
  """ Dataset 구성을 위한 class."""
  def __init__(self, pair_dataset, labels):
    self.pair_dataset = pair_dataset # {'input_ids': ~, 'token_type_ids': ~, 'attention_mask': ~}
    self.labels = labels # [0, 1, 1, 0, 0, ...]

  def __getitem__(self, idx):
    item = {key: val[idx].clone().detach() for key, val in self.pair_dataset.items()}
    item['labels'] = torch.tensor(self.labels[idx])
    return item

  def __len__(self):
    return len(self.labels)

def preprocessing_dataset(dataset):
  """ 처음 불러온 csv 파일을 원하는 형태의 DataFrame으로 변경 시켜줍니다."""
  """ 현재 구현되어 있는 부분은 개체에 대한 단어만 데이터셋에 들어가게됨"""
  subject_entity = []
  object_entity = []
  subject_span = []
  object_span = []
  for i,j in zip(dataset['subject_entity'], dataset['object_entity']): # word, start_idx, end_idx, type
    sub_data = i[1:-1]
    obj_data = j[1:-1]

    sub_data_parsed = re.findall(r"'[^\']+'", sub_data)
    obj_data_parsed = re.findall(r"'[^\']+'", obj_data)

    sub_word = sub_data_parsed[1][1:-1]
    obj_word = obj_data_parsed[1][1:-1]

    sub_data = i[1:-1].split(', ')
    obj_data = j[1:-1].split(', ')
    for d in sub_data:
        if d.startswith("'start_idx'"):
            sub_start_idx = d.split(': ')[1]
        if d.startswith("'end_idx'"):
            sub_end_idx = d.split(': ')[1]
    sub_idx = (int(sub_start_idx), int(sub_end_idx))

    for d in obj_data:
        if d.startswith("'start_idx'"):
            obj_start_idx = d.split(': ')[1]
        if d.startswith("'end_idx'"):
            obj_end_idx = d.split(': ')[1]
    obj_idx = (int(obj_start_idx), int(obj_end_idx))

    subject_entity.append(sub_word)
    object_entity.append(obj_word)
    subject_span.append(sub_idx)
    object_span.append(obj_idx)
  out_dataset = pd.DataFrame({'id':dataset['id'], 'sentence':dataset['sentence'],'subject_entity':subject_entity,'object_entity':object_entity,
                              'subject_span':subject_span, 'object_span':object_span, 'label':dataset['label']})
  return out_dataset

def load_data(dataset_dir):
  """ csv 파일을 경로에 맡게 불러 옵니다. """
  alternate_delimiter = "\t"
  pd_dataset = pd.read_csv(dataset_dir, sep=alternate_delimiter, engine='python')
  dataset = preprocessing_dataset(pd_dataset)
  
  return dataset

# KBS) split함수
def split_train_valid_stratified(dataset, split_ratio=0.2):
    train_idx_list = [idx for idx in range(len(dataset['label']))]
    valid_idx_list = []
    indices_dict = defaultdict(list)
    for idx, label in enumerate(dataset['label']):
        indices_dict[label].append(idx)
    
    for key, idx_list in indices_dict.items():
        valid_idx_list.extend(idx_list[:int(len(idx_list) * split_ratio)])
    
    train_idx_list = list(set(train_idx_list) - set(valid_idx_list))
    train_dataset = dataset.iloc[train_idx_list]
    valid_dataset = dataset.iloc[valid_idx_list]

    return train_dataset, valid_dataset

def tokenized_dataset(dataset, tokenizer):
  """ tokenizer에 따라 sentence를 tokenizing 합니다."""
  original_sentence = list(dataset['sentence'])
  modified_sentence = []
  for idx, (subj, obj) in enumerate(zip(dataset['subject_span'], dataset['object_span'])):
      if subj[0] < obj[0]: # subject_entity가 먼저 출현
          modified_str = [original_sentence[idx][:subj[0]],
                          '[SUBJ]',
                          original_sentence[idx][subj[0]:subj[1]+1],
                          '[/SUBJ]',
                          original_sentence[idx][subj[1]+1:obj[0]],
                          '[OBJ]',
                          original_sentence[idx][obj[0]:obj[1]+1],
                          '[/OBJ]',
                          original_sentence[idx][obj[1]+1:]]
          modified_sentence.append(''.join(modified_str))
      else: # object_entity가 먼저 출현
          modified_str = [original_sentence[idx][:obj[0]],
                          '[OBJ]',
                          original_sentence[idx][obj[0]:obj[1]+1],
                          '[/OBJ]',
                          original_sentence[idx][obj[1]+1:subj[0]],
                          '[SUBJ]',
                          original_sentence[idx][subj[0]:subj[1]+1],
                          '[/SUBJ]',
                          original_sentence[idx][subj[1]+1:]]

          modified_sentence.append(''.join(modified_str))

  tokenized_sentences = tokenizer(
      modified_sentence,
      return_tensors="pt",
      padding=True,
      truncation=True,
      max_length=256,
      add_special_tokens=True,
      ) # [CLS]sentence..[SUBJ]subject[/SUBJ]..[OBJ]object[/OBJ]..[SEP][PAD][PAD][PAD]
        # => input_ids, token_type_ids, attention_mask
  return tokenized_sentences

# KBS) ImbalancedSamplerTrainer
# https://velog.io/@ks2515/transformers-custom-trainer-%EB%A7%8C%EB%93%A4%EA%B8%B0 참조
# https://www.dlology.com/blog/multi-class-classification-with-focal-loss-for-imbalanced-datasets/ 참조
class ImbalancedSamplerTrainer(Trainer):  
    def compute_loss(self, model, inputs, return_outputs=False):
        labels = inputs.get("labels")
        one_hot_labels = F.one_hot(labels, num_classes=30)
        # forward pass
        outputs = model(**inputs)
        logits = outputs.get("logits")
        with open('label_counters.pkl', 'rb') as f:
            alpha = torch.tensor(pickle.load(f)).to(one_hot_labels.get_device())
        alpha = one_hot_labels * alpha * 100.0
        alpha = torch.sum(alpha, dim=1, keepdim=True)
        gamma = 2.0

        log_prob = F.cross_entropy(logits, labels, reduction='none')
        prob = torch.exp(-log_prob)
        loss = alpha * ((1 - prob) ** gamma) * log_prob
        loss = loss.mean()
        return (loss, outputs) if return_outputs else loss

def focal_loss(model, inputs, return_outputs=False):
        labels = inputs.get("labels")
        one_hot_labels = F.one_hot(labels, num_classes=30)
        # forward pass
        outputs = model(**inputs)
        logits = outputs.get("logits")
        with open('label_counters.pkl', 'rb') as f:
            alpha = torch.tensor(pickle.load(f)).to(one_hot_labels.get_device())
        alpha = one_hot_labels * alpha * 100.0
        alpha = torch.sum(alpha, dim=1, keepdim=True)
        gamma = 2.0

        log_prob = F.cross_entropy(logits, labels, reduction='none')
        prob = torch.exp(-log_prob)
        loss = alpha * ((1 - prob) ** gamma) * log_prob
        loss = loss.mean()
        return (loss, outputs) if return_outputs else loss

def klue_re_micro_f1(preds, labels):
    """KLUE-RE micro f1 (except no_relation)"""
    label_list = ['no_relation', 'org:top_members/employees', 'org:members',
       'org:product', 'per:title', 'org:alternate_names',
       'per:employee_of', 'org:place_of_headquarters', 'per:product',
       'org:number_of_employees/members', 'per:children',
       'per:place_of_residence', 'per:alternate_names',
       'per:other_family', 'per:colleagues', 'per:origin', 'per:siblings',
       'per:spouse', 'org:founded', 'org:political/religious_affiliation',
       'org:member_of', 'per:parents', 'org:dissolved',
       'per:schools_attended', 'per:date_of_death', 'per:date_of_birth',
       'per:place_of_birth', 'per:place_of_death', 'org:founded_by',
       'per:religion']
    no_relation_label_idx = label_list.index("no_relation")
    label_indices = list(range(len(label_list)))
    label_indices.remove(no_relation_label_idx)
    return sklearn.metrics.f1_score(labels, preds, average="micro", labels=label_indices) * 100.0

def klue_re_auprc(probs, labels):
    """KLUE-RE AUPRC (with no_relation)"""
    labels = np.eye(30)[labels]

    score = np.zeros((30,))
    for c in range(30):
        targets_c = labels.take([c], axis=1).ravel()
        preds_c = probs.take([c], axis=1).ravel()
        precision, recall, _ = sklearn.metrics.precision_recall_curve(targets_c, preds_c)
        score[c] = sklearn.metrics.auc(recall, precision)
    return np.average(score) * 100.0

def compute_metrics(pred):
  """ validation을 위한 metrics function """
  labels = pred.label_ids
  preds = pred.predictions.argmax(-1)
  probs = pred.predictions

  # calculate accuracy using sklearn's function
  f1 = klue_re_micro_f1(preds, labels)
  auprc = klue_re_auprc(probs, labels)
  acc = accuracy_score(labels, preds) # 리더보드 평가에는 포함되지 않습니다.
  wandb.log({"micro f1": f1, "auprc": auprc, "acc": acc})

  return {
      'micro f1 score': f1,
      'auprc' : auprc,
      'accuracy': acc,
  }

def label_to_num(label):
  num_label = []
  with open('dict_label_to_num.pkl', 'rb') as f:
    dict_label_to_num = pickle.load(f)
  for v in label:
    num_label.append(dict_label_to_num[v])
  
  return num_label

def make_alternative_set():
    first = True
    alternate_delimiter = "\t"
    new_file = open("./dataset/train/alternate_train.csv", 'a')
    with open("./dataset/train/train.csv", 'r') as f:
        while True:
            line = f.readline()
            if first:
                line = line.replace(",", alternate_delimiter) # 맨 위의 column줄을 교체함
                first = False
                new_file.write(line)
                continue
            if not line:
                break
            line = re.sub(',', alternate_delimiter, line, 1) # 처음 만나는 comma를 교체함(id)
            for _ in range(2): # 맨 뒤에 2개의 comma를 교체함
                comma_idx = line.rfind(',')
                line = line[:comma_idx] + alternate_delimiter + line[comma_idx+1:]
            line = re.sub(",\"{'word':", alternate_delimiter + "\"{'word':", line) # 나머지 중간의 2개의 comma를 교체함
            new_file.write(line)

    new_file.close()

    first = True
    new_file = open("./dataset/test/alternate_test.csv", 'a')
    with open("./dataset/test/test_data.csv", 'r') as f:
        while True:
            line = f.readline()
            if first:
                line = line.replace(",", alternate_delimiter)
                first = False
                new_file.write(line)
                continue
            if not line:
                break
            line = re.sub(',', alternate_delimiter, line, 1) # 처음 만나는 comma를 교체함(id)
            for _ in range(2): # 맨 뒤에 2개의 comma를 교체함
                comma_idx = line.rfind(',')
                line = line[:comma_idx] + alternate_delimiter + line[comma_idx+1:]
            line = re.sub(",\"{'word':", alternate_delimiter + "\"{'word':", line) # 나머지 중간의 2개의 comma를 교체함
            new_file.write(line)

    new_file.close()
    print("Finished!")