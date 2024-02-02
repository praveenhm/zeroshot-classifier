import os
## load packages
import pandas as pd
import numpy as np
import os
from datasets import load_dataset
import re
import time
import random
import tqdm

import sys
import torch
from torch.utils.data import DataLoader

import transformers
from transformers import AutoModelForSequenceClassification, AutoTokenizer
from transformers import TrainingArguments, Trainer
from datasets import ClassLabel
from datasets import load_dataset, load_metric, Dataset, DatasetDict, concatenate_datasets, list_metrics

from sklearn.metrics import balanced_accuracy_score, precision_recall_fscore_support, accuracy_score, classification_report

import gc
from accelerate.utils import release_memory

import wandb
import json
from datetime import datetime
import argparse
from mdutils import MdUtils

# for versioning of experiments with W&B
DATE = 20240131
SEED_GLOBAL = 42

np.random.seed(SEED_GLOBAL)
torch.manual_seed(SEED_GLOBAL)
random.seed(SEED_GLOBAL)

# print(os.getcwd())

# local config.py file with tokens
import config

## set main arguments
# setup this way to also enable submitting the script via HPC systems
parser = argparse.ArgumentParser(description='Pass arguments via terminal')

# make sure that parsing of boolian args is done correctly
def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

# main args
parser.add_argument('-ds_heldout', '--dataset_name_heldout', type=str,
                    help='names for datasets not to include in training. For heldout testing')
parser.add_argument('-downsample', '--downsample', type=str2bool, default=True,
                    help='Downsample and restrict to 1000 entries')
parser.add_argument('-train', '--do_train', type=str2bool, default=True,
                    help='Do training of flag set. Otherwise only evaluation')
parser.add_argument('-upload', '--upload_to_hub',type=str2bool, default=False,
                    help='Upload model to HF hub if flag is set')


args = parser.parse_args()
print("Arguments passed via the terminal:\n", args)

"""### Load data"""

# load from hub

dataset_finetune = load_dataset("penma/deberta2", token=config.HF_ACCESS_TOKEN)["train"]    

dataset_train = load_dataset("MoritzLaurer/dataset_train_nli", token=config.HF_ACCESS_TOKEN)["train"]
dataset_test_concat_nli = load_dataset("MoritzLaurer/dataset_test_concat_nli", token=config.HF_ACCESS_TOKEN)["train"]
dataset_test_disaggregated = load_dataset("MoritzLaurer/dataset_test_disaggregated_nli", token=config.HF_ACCESS_TOKEN)


# manually written task names for validating that code doesn't miss anything
task_names_manual = [
    'wellformedquery', 'financialphrasebank', 'rottentomatoes', 'amazonpolarity',
    'imdb', 'appreviews', 'yelpreviews', 'wikitoxic_toxicaggregated',
    'wikitoxic_obscene', 'wikitoxic_threat', 'wikitoxic_insult',
    'wikitoxic_identityhate', 'hateoffensive', 'hatexplain',
    'trueteacher', 'spam', 'massive', 'banking77', 'emotiondair',
    'emocontext', 'empathetic', 'agnews', 'yahootopics',
    'biasframes_offensive', 'biasframes_sex', 'biasframes_intent',
    # added for v1.1
    "manifesto", "capsotu", "finetune",
]

# select specific training subset
if args.dataset_name_heldout == "finetune":
    dataset_train_filt = dataset_finetune
elif args.dataset_name_heldout == "praveen":
    dataset_name_only_praveen = ["yelpreviews"]
    dataset_names_lst = set(dataset_train['task_name'])
    dataset_name_heldout = [dataset_name for dataset_name in dataset_names_lst if dataset_name not in dataset_name_only_praveen]
elif args.dataset_name_heldout == "all_except_nli":
    dataset_name_only_nli = ["mnli", "anli", "fevernli", "wanli", "lingnli"]
    dataset_names_lst = set(dataset_train['task_name'])
    dataset_name_heldout = [dataset_name for dataset_name in dataset_names_lst if dataset_name not in dataset_name_only_nli]
else:
    dataset_name_heldout = [args.dataset_name_heldout]

print("\n\n")
print(f"Training Dataset: {dataset_train} with counts:\n{dataset_train.to_pandas().task_name.value_counts()}")
print("\n\n")


# dataset_train_filt = dataset_train.filter(lambda example: example['task_name'] not in dataset_name_heldout)

print("\n\n")
#print(f"Filtered Training Dataset: {dataset_train_filt} with counts:\n{dataset_train_filt.to_pandas().task_name.value_counts()}")
print(f"Filtered Training Dataset: {dataset_train_filt} with counts:\n{dataset_train_filt.to_pandas().value_counts()}")
print("\n\n")

# downsampling for faster testing
if args.downsample:
    dataset_train_filt = dataset_train_filt.select(range(10))#1000
    dataset_test_concat_nli = dataset_test_concat_nli.select(range(1000))
    for dataset in dataset_test_disaggregated:
        dataset_test_disaggregated[dataset] = dataset_test_disaggregated[dataset].select(range(20))

"""### Tokenize, train eval"""

### Load model and tokenizer

if args.do_train:
    print("Training...")
    # model_name = "microsoft/xtremedistil-l6-h256-uncased"  #"microsoft/deberta-v3-xsmall"  #"microsoft/deberta-v3-large"  #"microsoft/deberta-v3-base" # microsoft/xtremedistil-l6-h256-uncased
    model_name = "MoritzLaurer/deberta-v3-base-zeroshot-v1.1-all-33"  #"microsoft/deberta-v3-xsmall"  #"microsoft/deberta-v3-large"  #"microsoft/deberta-v3-base" # microsoft/xtremedistil-l6-h256-uncased
else:
    print("Evaluating...")
    # can only comprehensively test binary NLI models, because NLI test datasets are binarized
    model_name = "MoritzLaurer/deberta-v3-base-mnli-fever-anli-ling-wanli-binary"  #"facebook/bart-large-mnli"  #"sileod/deberta-v3-base-tasksource-nli"  #"MoritzLaurer/DeBERTa-v3-base-mnli-fever-docnli-ling-2c"

max_length = 512

## load model and tokenizer
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Device: {device}")
print(f"Model: {model_name}")

# label2id mapping
if args.do_train:
    label2id = {"entailment": 0, "not_entailment": 1}  #{"entailment": 0, "neutral": 1, "contradiction": 2}
    id2label = {0: "entailment", 1: "not_entailment"}  #{0: "entailment", 1: "neutral", 2: "contradiction"}

    tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=False, model_max_length=max_length)  # model_max_length=512
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name, label2id=label2id, id2label=id2label
    ).to(device)

    label_text_unique = list(label2id.keys())
    print(label_text_unique)

else:
    tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=False, model_max_length=max_length)  # model_max_length=512
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name
    ).to(device)

    label_text_unique = list(model.config.id2label.values())
    print(label_text_unique)

"""### Configure logging with W&B"""

## logging with wandb

# logging
wandb.login(key=config.WANDB_ACCESS_TOKEN)

project_name = f"nli-zeroshot-{DATE}"

now = datetime.now().strftime("%Y-%m-%d-%H-%M")
run_name = f"{model_name.split('/')[-1]}-zeroshot-heldout-{args.dataset_name_heldout}-{now}"

#wandb.init(project=project_name, name=run_name)
# if updating config here, HF trainer does not seem to log info to config anymore
#wandb.config.update({"dataset_name_heldout": dataset_name_heldout}, allow_val_change=True)

# https://huggingface.co/docs/transformers/v4.34.0/en/main_classes/callback#transformers.integrations.WandbCallback
wandb_log_model = "false"
wandb_watch = "false"
print(f"WANDB Arguments: {project_name} {wandb_log_model} {wandb_watch}")
os.environ["WANDB_PROJECT"] = project_name  # log to your project
os.environ["WANDB_LOG_MODEL"] = wandb_log_model  # Can be "end", "checkpoint" or "false". If set to "end", the model will be uploaded at the end of training. If set to "checkpoint", the checkpoint will be uploaded every args.save_steps . If set to "false", the model will not be uploaded. Use along with load_best_model_at_end() to upload best model.
os.environ["WANDB_WATCH"] = wandb_watch   # Can be "gradients", "all", "parameters", or "false". Set to "all" to log gradients and parameters.

# custom init https://docs.wandb.ai/guides/integrations/huggingface#customize-wandbinit
# config and name initialized via HF trainer
#wandb.init(project="my_project", config=experiment['config'], name=experiment['name']):

"""#### Tokenize"""

# Dynamic padding, HF course: https://huggingface.co/course/chapter3/2?fw=pt

# without padding="max_length" & max_length=512, it should do dynamic padding.
def tokenize_func(examples):
    return tokenizer(examples["text"], examples["hypothesis"], truncation=True)  # max_length=512,  padding=True

# training on:
encoded_dataset_train = dataset_train_filt.map(tokenize_func, batched=True)
print("Encoded Dataset Train:")
print(len(encoded_dataset_train))
print(encoded_dataset_train)
# testing during training loop on aggregated testset:
encoded_dataset_test = dataset_test_concat_nli.map(tokenize_func, batched=True)
print("Encoded Dataset Test:")
print(len(encoded_dataset_test))
print(encoded_dataset_test)

# testing on individual datasets:
encoded_dataset_test_disaggregated = dataset_test_disaggregated.map(tokenize_func, batched=True)

# remove columns the library does not expect
encoded_dataset_train = encoded_dataset_train.remove_columns(["hypothesis", "text"])
encoded_dataset_test = encoded_dataset_test.remove_columns(["hypothesis", "text"])

"""#### Training"""

# release memory: https://huggingface.co/blog/optimize-llm

def flush():
  gc.collect()
  torch.cuda.empty_cache()
  torch.cuda.reset_peak_memory_stats()

# function for computing metrics for normally formatted classification tasks
# here, this is used for the standard NLI datasets like MNLI, ANLI etc
def compute_metrics_standard(eval_pred, label_text_alphabetical=None):
    labels = eval_pred.label_ids
    pred_logits = eval_pred.predictions
    preds_max = np.argmax(pred_logits, axis=1)  # argmax on each row (axis=1) in the tensor

    # metrics
    precision_macro, recall_macro, f1_macro, _ = precision_recall_fscore_support(labels, preds_max, average='macro')  # https://scikit-learn.org/stable/modules/generated/sklearn.metrics.precision_recall_fscore_support.html
    precision_micro, recall_micro, f1_micro, _ = precision_recall_fscore_support(labels, preds_max, average='micro')  # https://scikit-learn.org/stable/modules/generated/sklearn.metrics.precision_recall_fscore_support.html
    acc_balanced = balanced_accuracy_score(labels, preds_max)
    acc_not_balanced = accuracy_score(labels, preds_max)

    metrics = {'f1_macro': f1_macro,
            'f1_micro': f1_micro,
            'accuracy_balanced': acc_balanced,
            'accuracy': acc_not_balanced,
            'precision_macro': precision_macro,
            'recall_macro': recall_macro,
            'precision_micro': precision_micro,
            'recall_micro': recall_micro,
            #'label_gold_raw': labels,
            #'label_predicted_raw': preds_max
            }
    print("Aggregate metrics: ", {key: metrics[key] for key in metrics if key not in ["label_gold_raw", "label_predicted_raw"]} )  # print metrics but without label lists
    print("Detailed metrics: ", classification_report(
        labels, preds_max, labels=np.sort(pd.factorize(label_text_alphabetical, sort=True)[0]),
        target_names=label_text_alphabetical, sample_weight=None,
        digits=2, output_dict=True, zero_division='warn'),
    "\n")

    return metrics


# function to compute metrics for classification tasks that have been reformatted into the NLI format
# here, this is used for the non-NLI classification tasks (which were converted to NLI format)
def compute_metrics_nli_binary(eval_pred, label_text_alphabetical=None):
    predictions, labels = eval_pred

    # hacky special handling for BART encoder-decoder model
    #if "bart" in model_name:
    #    predictions = predictions[0]

    # split in chunks with predictions for each hypothesis for one unique premise
    def chunks(lst, n):  # Yield successive n-sized chunks from lst. https://stackoverflow.com/questions/312443/how-do-you-split-a-list-into-evenly-sized-chunks
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    # for each chunk/premise, select the most likely hypothesis, either via raw logits, or softmax
    prediction_chunks_lst = list(chunks(predictions, len(set(label_text_alphabetical)) ))  # len(LABEL_TEXT_ALPHABETICAL)
    hypo_position_highest_prob = []
    for i, chunk in enumerate(prediction_chunks_lst):
        # only accesses the first column of the array, i.e. the entailment prediction logit of all hypos and takes the highest one
        if "bart" not in model_name:
            hypo_position_highest_prob.append(np.argmax(chunk[:, 0]))
        else:  # bart has label sequence ['contradiction', 'neutral', 'entailment']
            hypo_position_highest_prob.append(np.argmax(chunk[:, 2]))

    label_chunks_lst = list(chunks(labels, len(set(label_text_alphabetical)) ))
    label_position_gold = []
    for chunk in label_chunks_lst:
        label_position_gold.append(np.argmin(chunk))  # argmin to detect the position of the 0 among the 1s

    # for inspection
    print("Highest probability prediction per premise: ", hypo_position_highest_prob[:10])
    print("Correct label per premise: ", label_position_gold[:10])

    ## metrics
    precision_macro, recall_macro, f1_macro, _ = precision_recall_fscore_support(label_position_gold, hypo_position_highest_prob, average='macro')  # https://scikit-learn.org/stable/modules/generated/sklearn.metrics.precision_recall_fscore_support.html
    precision_micro, recall_micro, f1_micro, _ = precision_recall_fscore_support(label_position_gold, hypo_position_highest_prob, average='micro')  # https://scikit-learn.org/stable/modules/generated/sklearn.metrics.precision_recall_fscore_support.html
    acc_balanced = balanced_accuracy_score(label_position_gold, hypo_position_highest_prob)
    acc_not_balanced = accuracy_score(label_position_gold, hypo_position_highest_prob)
    metrics = {'f1_macro': f1_macro,
               'f1_micro': f1_micro,
               'accuracy_balanced': acc_balanced,
               'accuracy': acc_not_balanced,
               'precision_macro': precision_macro,
               'recall_macro': recall_macro,
               'precision_micro': precision_micro,
               'recall_micro': recall_micro,
               #'label_gold_raw': label_position_gold,
               #'label_predicted_raw': hypo_position_highest_prob
               }
    print("Aggregate metrics: ", {
        key: metrics[key] for key in metrics
        if key not in ["label_gold_raw", "label_predicted_raw"]
    })
    print("Detailed metrics: ", classification_report(
        label_position_gold,
        hypo_position_highest_prob,
        labels=np.sort(pd.factorize(label_text_alphabetical, sort=True)[0]),
        target_names=label_text_alphabetical,
        sample_weight=None, digits=2, output_dict=True,
        zero_division='warn'),
    "\n")

    return metrics

training_directory = f'./results/{model_name.split("/")[-1]}-zeroshot-{args.dataset_name_heldout}-{now}'

fp16_bool = True if torch.cuda.is_available() else False
if "mDeBERTa" in model_name: fp16_bool = False  # mDeBERTa does not support FP16 yet

# https://huggingface.co/transformers/main_classes/trainer.html#transformers.TrainingArguments
eval_batch = 64 if "large" in model_name else 64*2
per_device_train_batch_size = 8 if "large" in model_name else 32
gradient_accumulation_steps = 4 if "large" in model_name else 1

#if USING_COLAB:
    #per_device_train_batch_size = int(per_device_train_batch_size / 4)
    #gradient_accumulation_steps = int(gradient_accumulation_steps * 4)
    #eval_batch = int(eval_batch / 32) if "large" in model_name else int(eval_batch / 8)

hub_model_id = f'penma/{model_name.split("/")[-1]}-zeroshot-v1.1-{args.dataset_name_heldout}'

train_args = TrainingArguments(
    output_dir=training_directory,
    logging_dir=f'{training_directory}/logs',
    #deepspeed="ds_config_zero3.json",  # if using deepspeed
    lr_scheduler_type= "linear",
    group_by_length=False,  # can increase speed with dynamic padding, by grouping similar length texts https://huggingface.co/transformers/main_classes/trainer.html
    learning_rate=9e-6 if "large" in model_name else 2e-5,
    per_device_train_batch_size=per_device_train_batch_size,
    per_device_eval_batch_size=eval_batch,
    gradient_accumulation_steps=gradient_accumulation_steps,  # (!adapt/halve batch size accordingly). accumulates gradients over X steps, only then backward/update. decreases memory usage, but also slightly speed
    #eval_accumulation_steps=2,
    num_train_epochs=3,
    #max_steps=400,
    #warmup_steps=0,  # 1000,
    warmup_ratio=0.06,  #0.1, 0.06
    weight_decay=0.01,  #0.1,
    fp16=fp16_bool,   # ! only makes sense at batch-size > 8. loads two copies of model weights, which creates overhead. https://huggingface.co/transformers/performance.html?#fp16
    fp16_full_eval=fp16_bool,
    evaluation_strategy="epoch",
    seed=SEED_GLOBAL,
    #load_best_model_at_end=True,
    #metric_for_best_model="accuracy",
    #eval_steps=300,  # evaluate after n steps if evaluation_strategy!='steps'. defaults to logging_steps
    save_strategy="no",  # options: "no"/"steps"/"epoch"
    #save_steps=1_000_000,  # Number of updates steps before two checkpoint saves.
    save_total_limit=1,  # If a value is passed, will limit the total amount of checkpoints. Deletes the older checkpoints in output_dir
    #logging_strategy="epoch",
    report_to="all",  # "all"
    run_name=run_name,
    push_to_hub=True,  # does not seem to work if save_strategy="no"
    hub_model_id=hub_model_id,
    hub_token=config.HF_ACCESS_TOKEN,
    hub_strategy="end",
    hub_private_repo=True,
)

trainer = Trainer(
    model=model,
    #model_init=model_init,
    tokenizer=tokenizer,
    args=train_args,
    train_dataset=encoded_dataset_train,  #.shard(index=1, num_shards=200),  # https://huggingface.co/docs/datasets/processing.html#sharding-the-dataset-shard
    eval_dataset=encoded_dataset_test,  #.shard(index=1, num_shards=20),
    compute_metrics=lambda x: compute_metrics_standard(x, label_text_alphabetical=label_text_unique)  #compute_metrics,
    #data_collator=data_collator,  # for weighted sampling per dataset; for dynamic padding probably not necessary because done by default  https://huggingface.co/course/chapter3/3?fw=pt
)

if device == "cuda":
    # free memory
    flush()
    release_memory(model)
    #del (model, trainer)

# train
if args.do_train:

    trainer.train()

"""#### Evaluation"""

# could load specific model for evaluation here
#model = AutoModelForSequenceClassification.from_pretrained('./results/nli-few-shot/all-nli-3c/DeBERTa-v3-mnli-fever-anli-v1',   # nli_effect/distilroberta-paraphrase-mnli-fever-anli-v1
#                                                           label2id=label2id, id2label=id2label).to(device)

# free memory
if device == "cuda":
    flush()
    release_memory(model)

datasets_not_to_evaluate = ["dummy_dataset"]   # "anthropic", "banking77", "massive", "empathetic"

result_dic = {}
for key_task_name, value_dataset in tqdm.tqdm(encoded_dataset_test_disaggregated.items(), desc="Iterations over testsets"):
    print(f"\n*** Evaluating task: {key_task_name}. Length of dataset: {len(value_dataset)}")
    # skip selected datasets
    if any(dataset_name in key_task_name for dataset_name in datasets_not_to_evaluate):
        continue
    # eval not_nli datasets
    elif key_task_name not in ["mnli_m", "mnli_mm", "fevernli", "anli_r1", "anli_r2", "anli_r3", "wanli", "lingnli"]:  #dataset_test_disaggregated.keys():
        label_text_alphabetical_task = np.sort(np.unique(value_dataset["label_text"])).tolist()
        trainer.compute_metrics = lambda x: compute_metrics_nli_binary(x, label_text_alphabetical=label_text_alphabetical_task)
        result = trainer.evaluate(eval_dataset=encoded_dataset_test_disaggregated[key_task_name])
    # eval nli datasets. only works for binary nli models because datasets are binary
    elif len(label_text_unique) == 2:
        trainer.compute_metrics = lambda x: compute_metrics_standard(x, label_text_alphabetical=label_text_unique)
        result = trainer.evaluate(eval_dataset=encoded_dataset_test_disaggregated[key_task_name])
    else:
        raise ValueError(f"Issue with task: {key_task_name}")

    result_dic.update({key_task_name: result})
    print(f"Result for task {key_task_name}: ", result, "\n")

    # log metrics
    wandb.run.summary.update({f"{key_task_name}/{key_metric_name}": value_metric for key_metric_name, value_metric in result.items()})

    if device == "cuda":
        flush()
        release_memory(model)


print("\n\nOverall results: ", result_dic)

# log additional info to config
# needs to be done after trainer initialized the config (?)
wandb.config.update({"dataset_name_heldout": args.dataset_name_heldout}, allow_val_change=True)

print("wandb.run.id: ", wandb.run.id)
print("wandb.run.name: ", wandb.run.name)

wandb.finish()

"""#### Save"""

# save & upload trained models
if args.upload_to_hub and args.do_train:

    trainer.push_to_hub(commit_message="End of training")

    # tokenizer needs to be uploaded separately to create tokenizer.json
    # otherwise only tokenizer_config.json is created and pip install sentencepiece is required
    tokenizer.push_to_hub(repo_id=hub_model_id, use_temp_dir=True, private=True, use_auth_token=config.HF_ACCESS_TOKEN)


    # to save best model to disk
    """
    model_path = f"{training_directory}/best-{model_name.split('/')[-1]}-{DATE}"

    trainer.save_model(output_dir=model_path)

    print(os.getcwd())
    model = AutoModelForSequenceClassification.from_pretrained(model_path, torch_dtype=torch.float16)
    tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=True, model_max_length=512)

    ## Push to hub without trainer
    #!sudo apt-get install git-lfs
    #!huggingface-cli login
    # unnecessary if token provided below

    # https://huggingface.co/docs/transformers/main_classes/model#transformers.PreTrainedModel.push_to_hub
    repo_id = f'MoritzLaurer/{model_name.split("/")[-1]}-zeroshot-v1.1-{args.dataset_name_heldout}'
    model.push_to_hub(repo_id=repo_id, use_temp_dir=True, private=True, use_auth_token=config.HF_ACCESS_TOKEN)
    tokenizer.push_to_hub(repo_id=repo_id, use_temp_dir=True, private=True, use_auth_token=config.HF_ACCESS_TOKEN)
    """

## testing automatic creation of .md file
# https://mdutils.readthedocs.io/en/latest/mdutils.html#subpackages
mdFile = MdUtils(file_name=f'README-{model_name.split("/")[-1]}-{DATE}', title='Model Card')

row_dataset_names = list(result_dic.keys())
row_metrics = [str(round(value["eval_accuracy"], 3)) for key, value in result_dic.items()]
row_samp_per_sec = [str(round(value["eval_samples_per_second"], 0)) for key, value in result_dic.items()]

table_lst = ["Datasets"] + row_dataset_names + ["Accuracy"] + row_metrics + [f"Inference text/sec (A100, batch={eval_batch})"] + row_samp_per_sec

# create markdown table with results
#mdFile.new_line()
results_table_me = mdFile.new_table(columns=len(list(result_dic.keys()))+1, rows=3, text=table_lst, text_align='center')
print(results_table_me)

# write results_table_me to training directors
path_main = os.getcwd()
os.chdir(training_directory)
mdFile.create_md_file()
os.chdir(path_main)