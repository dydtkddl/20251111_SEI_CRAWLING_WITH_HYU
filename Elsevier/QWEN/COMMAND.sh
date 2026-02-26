CUDA_VISIBLE_DEVICES=1 \
python zmb_exsitu_stage2_pipeline.py \
  --csv ../cleaned_outputs/articles_cleaned_v8.csv \
  --title_col dc_title \
  --abstract_col abstract \
  --sf_col source_file \
  --journal_col publicationName \
  --pubtype_col pubType \
  --model qwen3:30b-a3b-instruct-2507-q4_K_M

