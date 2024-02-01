#!/bin/bash

# Uncomment and adjust these if needed for your environment
# module load 2022
# module load Python/3.9.5-GCCcore-10.3.0
# pip install datasets~=2.14.0
# python ./zeroshot/download_datasets.py

echo "starting the script"
Iterate through all datasets for heldout testing
dataset_name_heldout=(
    'wellformedquery' 'financialphrasebank' 'rottentomatoes' 'amazonpolarity'
    'imdb' 'appreviews'   'wikitoxic_toxicaggregated'
    'wikitoxic_obscene' 'wikitoxic_threat' 'wikitoxic_insult'
    'wikitoxic_identityhate' 'hateoffensive' 'hatexplain'
    'trueteacher' 'spam' 'massive' 'banking77' 'emotiondair'
    'emocontext' 'empathetic' 'agnews' 'yahootopics'
    'biasframes_offensive' 'biasframes_sex' 'biasframes_intent'
    "manifesto" "capsotu"
)

# dataset_name_heldout=(
#     'yelpreviews'         #'imdb' 'appreviews' 'yelpreviews' 
# )

for dataset_name in "${dataset_name_heldout[@]}"
do
    echo "Submitting job for dataset: $dataset_name"
    ./zeroshot/job_run.bash "$dataset_name" True True > "./zeroshot/logs/logs_$dataset_name.txt" 2>&1
done

# Two runs with all data (not heldout) or with only nli data
datasets=("all_except_nli" "none")

for dataset_name in "${datasets[@]}"
do  
    echo "Submitting job for dataset: $dataset_name"    
    #./zeroshot/job_run.bash "$dataset_name" True True > "./zeroshot/logs/logs_$dataset_name.txt" 2>&1
done

# No need for chmod +x and script execution lines as they are for the current script
