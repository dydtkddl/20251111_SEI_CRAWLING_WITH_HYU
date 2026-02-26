python 04_llm_classify_2stage.py ^
 --input_json "01_run_out_v2/grobid_results_all.json" ^
 --top_k 6 ^
 --min_score 0.3 ^
 --max_chars 1200 ^
 --temperature 0.0 ^
 --top_p 0.9 ^
 --timeout 90 ^
 --ollama_url http://localhost:11434 ^
 --llm_model qwen2.5:14b-instruct