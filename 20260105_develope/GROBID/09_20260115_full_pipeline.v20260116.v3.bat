@echo off
python 09_20260115_full_pipeline.v20260116.v3.py ^
  --pii_list 09_20260115_full_pipeline.pii_list_.txt ^
  --xml_dir ../../Elsevier/xmls_meta_abs ^
  --pdf_dir ../pdfs ^
  --supp_dir ../supplementary_files ^
  --out_dir ./09_20260115_full_pipeline_output_20260118 ^
  --grobid_host localhost ^
  --grobid_port 8080 ^
  --ollama_mode http ^
  --ollama_url http://localhost:11434 ^
  --llm_model qwen2.5:14b-instruct ^
  --stage2_model qwen3:30b-thinking ^
  --stage2_schedule batch ^
  --stage2_batch_k 10 ^
  --stage2_keep_alive 30m ^
  --save_prompts ^
  --save_section_prompts ^
  --max_sections_to_process 5 ^
  --max_evidence_chars 8000 ^
  --use_cache ^
  --save_methodlike ^
  --early_stop_yes_n 4 ^
  --early_stop_min_conf 0.75 ^
  --early_stop_min_quality 0.55
pause
