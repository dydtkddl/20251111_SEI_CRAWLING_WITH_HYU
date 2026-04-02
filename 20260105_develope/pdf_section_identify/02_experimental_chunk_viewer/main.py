from fastapi import FastAPI, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
import json
import re
from pathlib import Path
from typing import Dict, List, Any
from collections import Counter

app = FastAPI(title="Experimental Chunks Viewer", version="3.0")

# Setup templates
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Path to the JSON file
JSON_PATH = BASE_DIR.parent / "02_experimental_chunks_FINAL.json"

def parse_filter_input(raw_input: str) -> set:
    """Parse filter input from user"""
    if not raw_input:
        return set()
    
    clean_text = re.sub(r"[{}[\],'\"]", " ", raw_input)
    tokens = [t.strip() for t in re.split(r"[\s,]+", clean_text) if t.strip()]
    return set(tokens)

def load_data() -> Dict[str, Any]:
    """Load experimental chunks data"""
    if not JSON_PATH.exists():
        return {}
    
    try:
        with open(JSON_PATH, mode='r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading JSON: {e}")
        return {}

def calculate_statistics(full_data: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate comprehensive statistics from the data"""
    
    total_docs = len(full_data)
    docs_with_chunks = 0
    docs_without_chunks = 0
    total_chunks = 0
    
    category_counts = Counter()
    score_distribution = {"0-2": 0, "2-4": 0, "4-6": 0, "6-8": 0, "8-10": 0, "10+": 0}
    heading_counts = []
    
    proc_zn_sentences_total = 0
    kept_sentences_total = 0
    fallback_used_count = 0
    method_ranges_found_count = 0
    
    llm_calls_total = 0
    llm_refine_calls_total = 0
    
    for doc_info in full_data.values():
        chunks = doc_info.get("chunks", [])
        num_chunks = len(chunks)
        
        if num_chunks > 0:
            docs_with_chunks += 1
        else:
            docs_without_chunks += 1
        
        total_chunks += num_chunks
        
        # Stats
        stats = doc_info.get("stats", {})
        if stats.get("fallback_used"):
            fallback_used_count += 1
        if stats.get("method_ranges_found"):
            method_ranges_found_count += 1
            
        heading_counts.append(stats.get("candidate_headings", 0))
        proc_zn_sentences_total += stats.get("proc_zn_sentences", 0)
        kept_sentences_total += stats.get("kept_sentences", 0)
        
        llm_calls_total += stats.get("llm_calls_used", 0)
        llm_refine_calls_total += stats.get("llm_refine_calls_used", 0)
        
        # Process chunks
        for chunk in chunks:
            cat = chunk.get("category", "unknown")
            category_counts[cat] += 1
            
            score = chunk.get("score", 0)
            if score < 2:
                score_distribution["0-2"] += 1
            elif score < 4:
                score_distribution["2-4"] += 1
            elif score < 6:
                score_distribution["4-6"] += 1
            elif score < 8:
                score_distribution["6-8"] += 1
            elif score < 10:
                score_distribution["8-10"] += 1
            else:
                score_distribution["10+"] += 1
    
    avg_chunks = total_chunks / total_docs if total_docs > 0 else 0
    avg_headings = sum(heading_counts) / total_docs if total_docs > 0 else 0
    
    kept_paragraphs_total = sum(d.get("stats", {}).get("kept_paragraphs", 0) for d in full_data.values())
    dropped_paragraphs_total = sum(d.get("stats", {}).get("dropped_paragraphs", 0) for d in full_data.values())
    
    return {
        "total_docs": total_docs,
        "docs_with_chunks": docs_with_chunks,
        "docs_without_chunks": docs_without_chunks,
        "total_chunks": total_chunks,
        "avg_chunks_per_doc": round(avg_chunks, 2),
        "avg_headings_per_doc": round(avg_headings, 2),
        "category_counts": dict(category_counts),
        "score_distribution": score_distribution,
        "fallback_used_count": fallback_used_count,
        "fallback_percent": round(100 * fallback_used_count / total_docs, 1) if total_docs > 0 else 0,
        "method_ranges_found_count": method_ranges_found_count,
        "proc_zn_sentences_total": proc_zn_sentences_total,
        "kept_sentences_total": kept_sentences_total,
        "kept_paragraphs_total": kept_paragraphs_total,
        "dropped_paragraphs_total": dropped_paragraphs_total,
        "total_paragraphs_processed": kept_paragraphs_total + dropped_paragraphs_total,
        "keep_rate": round(100 * kept_paragraphs_total / (kept_paragraphs_total + dropped_paragraphs_total), 1) if (kept_paragraphs_total + dropped_paragraphs_total) > 0 else 0,
        "llm_calls_total": llm_calls_total,
        "llm_refine_calls_total": llm_refine_calls_total,
        "llm_enabled_percent": round(100 * fallback_used_count / total_docs, 1) if total_docs > 0 else 0 # Alias for UI
    }

@app.get("/")
async def read_root(
    request: Request, 
    filter_ids: str = Query(None),
    min_score: float = Query(None),
    category: str = Query(None),
    show_empty: bool = Query(True)
):
    """Main viewer endpoint with filtering capabilities"""
    
    full_data = load_data()
    
    if not full_data:
        return templates.TemplateResponse("index.html", {
            "request": request, 
            "error": f"JSON file not found or empty at {JSON_PATH}", 
            "data": [],
            "filter_ids": filter_ids or "",
            "min_score": min_score,
            "category": category or "",
            "show_empty": show_empty,
            "global_stats": {},
            "filtered_stats": {}
        })

    target_ids = parse_filter_input(filter_ids)
    
    # Calculate global statistics
    global_stats = calculate_statistics(full_data)
    
    # Process and filter data
    processed_data = []
    filtered_chunks_count = 0
    filtered_docs_with_chunks = 0
    filtered_docs_without_chunks = 0
    
    for doc_id, doc_info in full_data.items():
        # Apply document ID filter
        if target_ids:
            is_target = any(tid.lower() in doc_id.lower() for tid in target_ids)
            if not is_target:
                continue
        
        chunks = doc_info.get("chunks", [])
        
        # Apply chunk-level filters
        filtered_chunks = []
        for chunk in chunks:
            # Score filter
            if min_score is not None:
                if chunk.get("score", 0) < min_score:
                    continue
            
            # Category filter
            if category and category != "all":
                if chunk.get("category", "") != category:
                    continue
            
            filtered_chunks.append(chunk)
        
        # Show empty documents option
        if not show_empty and not filtered_chunks:
            continue
        
        if filtered_chunks:
            filtered_docs_with_chunks += 1
        else:
            filtered_docs_without_chunks += 1
        
        filtered_chunks_count += len(filtered_chunks)
        
        processed_data.append({
            "doc_id": doc_id,
            "source_path": doc_info.get("md_path", doc_info.get("source_path", "")),
            "selected_headings": doc_info.get("selected_headings", []),
            "chunks": filtered_chunks,
            "stats": doc_info.get("stats", {})
        })
    
    # Filtered statistics
    filtered_stats = {
        "total_docs": len(processed_data),
        "docs_with_chunks": filtered_docs_with_chunks,
        "docs_without_chunks": filtered_docs_without_chunks,
        "total_chunks": filtered_chunks_count
    }
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "data": processed_data,
        "filter_ids": filter_ids or "",
        "min_score": min_score,
        "category": category or "",
        "show_empty": show_empty,
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
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)