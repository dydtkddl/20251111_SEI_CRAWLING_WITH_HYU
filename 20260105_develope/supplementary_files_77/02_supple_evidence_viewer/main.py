from fastapi import FastAPI, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
import json
import re
from pathlib import Path
from typing import Dict, List, Any
from collections import Counter
import pandas as pd
import numpy as np

app = FastAPI(title="Supplementary Evidence Viewer", version="4.0")

# Setup templates
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Path to the CSV file
CSV_PATH = BASE_DIR.parent / "out_v20_supp_balanced.csv"
print(CSV_PATH)
def parse_filter_input(raw_input: str) -> set:
    """Parse filter input from user"""
    if not raw_input:
        return set()
    
    clean_text = re.sub(r"[{}[\],'\"]", " ", raw_input)
    tokens = [t.strip() for t in re.split(r"[\s,]+", clean_text) if t.strip()]
    return set(tokens)

def safe_json_load(val):
    if pd.isna(val) or not val:
        return []
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except:
        return []

def load_data() -> List[Dict[str, Any]]:
    """Load supplementary evidence data from CSV"""
    if not CSV_PATH.exists():
        return []
    
    try:
        df = pd.read_csv(CSV_PATH)
        # Parse JSON columns
        json_cols = [
            'EvidenceJSON', 'EvidenceJSON_Cleaned', 'TaggedSentencesJSON', 
            'CleanedChunksByTagJSON', 'WarningsJSON'
        ]
        for col in json_cols:
            if col in df.columns:
                df[col] = df[col].apply(safe_json_load)
        
        # Convert to list of dicts
        return df.to_dict(orient='records')
    except Exception as e:
        print(f"Error loading CSV: {e}")
        return []

def calculate_statistics(data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate comprehensive statistics from the data"""
    if not data:
        return {}
    
    total_docs = len(data)
    found_docs = sum(1 for d in data if d.get('Found') == True or d.get('Found') == 'True')
    fallback_used_count = sum(1 for d in data if d.get('FallbackUsed') == True or d.get('FallbackUsed') == 'True')
    
    tag_counts = Counter()
    total_tagged_sentences = 0
    llm_calls_total = 0
    
    for d in data:
        llm_calls_total += d.get('LLMCalls', 0)
        tagged = d.get('TaggedSentencesJSON', [])
        for s in tagged:
            tag_counts[s.get('tag', 'UNKNOWN')] += 1
            total_tagged_sentences += 1
            
    return {
        "total_docs": total_docs,
        "found_docs": found_docs,
        "found_percent": round(100 * found_docs / total_docs, 1) if total_docs > 0 else 0,
        "fallback_used_count": fallback_used_count,
        "fallback_percent": round(100 * fallback_used_count / total_docs, 1) if total_docs > 0 else 0,
        "total_tagged_sentences": total_tagged_sentences,
        "llm_calls_total": llm_calls_total,
        "tag_counts": dict(tag_counts),
        "avg_llm_per_doc": round(llm_calls_total / total_docs, 2) if total_docs > 0 else 0
    }

@app.get("/")
async def read_root(
    request: Request, 
    filter_ids: str = Query(None),
    tag: str = Query(None),
    only_found: bool = Query(False),
    only_fallback: bool = Query(False)
):
    """Main viewer endpoint with filtering capabilities"""
    
    full_data = load_data()
    
    if not full_data:
        return templates.TemplateResponse("index.html", {
            "request": request, 
            "error": f"CSV file not found or empty at {CSV_PATH}", 
            "data": [],
            "filter_ids": filter_ids or "",
            "tag": tag or "",
            "only_found": only_found,
            "only_fallback": only_fallback,
            "global_stats": {},
            "filtered_stats": {}
        })

    target_ids = parse_filter_input(filter_ids)
    
    # Calculate global statistics
    global_stats = calculate_statistics(full_data)
    
    # Process and filter data
    processed_data = []
    
    for row in full_data:
        # File ID filter
        file_name = row.get('File', '')
        if target_ids:
            if not any(tid.lower() in file_name.lower() for tid in target_ids):
                continue
        
        # Found filter
        if only_found and not (row.get('Found') == True or row.get('Found') == 'True'):
            continue
            
        # Fallback filter
        if only_fallback and not (row.get('FallbackUsed') == True or row.get('FallbackUsed') == 'True'):
            continue
        
        # Tag filter
        tagged_sents = row.get('TaggedSentencesJSON', [])
        if tag and tag != "all":
            if not any(s.get('tag') == tag for s in tagged_sents):
                continue
        
        processed_data.append(row)
    
    filtered_stats = {
        "total_docs": len(processed_data),
        "found_docs": sum(1 for d in processed_data if d.get('Found') == True or d.get('Found') == 'True')
    }
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "data": processed_data,
        "filter_ids": filter_ids or "",
        "tag": tag or "",
        "only_found": only_found,
        "only_fallback": only_fallback,
        "global_stats": global_stats,
        "filtered_stats": filtered_stats
    })

@app.get("/api/stats")
async def get_stats():
    """API endpoint for statistics"""
    full_data = load_data()
    return JSONResponse(calculate_statistics(full_data))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8002, reload=True)