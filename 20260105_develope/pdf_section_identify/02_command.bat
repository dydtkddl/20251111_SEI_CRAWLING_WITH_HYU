set SCRIPT=02_extract_numbered_methods_sections_UPGRADED.py
set HEADERS=01_headers_summary.json
set ROOTDIR=..\pdfs_marker_output
set OUTJSON=02_experimental_chunks_FINAL.json
set OUTJSONL=02_experimental_chunks_FINAL.jsonl

python -u %SCRIPT% ^
  --mode chunks ^
  --headers_json "%HEADERS%" ^
  --root_dir "%ROOTDIR%" ^
  --out_json "%OUTJSON%" ^
  --out_jsonl "%OUTJSONL%" ^
  --include_cell_assembly ^
  --include_electrolyte ^
  --include_weak_materials ^
  --remove_bracket_citations ^
  --remove_parenthetical_citations ^
  --drop_caption_blocks ^
  --keep_fig_table_ref_sentences ^
  --strip_in_sentence_figrefs ^
  --fallback_min_proc_zn_sentences 1 ^
  --auto_find_supp_candidates ^
  --supp_flag_threshold 0.65 ^
  --llm_backend none ^
  --log_level INFO ^
  > run_extract_v8.log 2>&1
