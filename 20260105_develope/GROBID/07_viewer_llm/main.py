
"""
LLM Filtered Data Viewer (Section Classification)
View classified supplementary sections (YES/NO/SKIP) with reasoning.
"""
from fastapi import FastAPI, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from collections import Counter, defaultdict
import os

app = FastAPI(title="Supplementary Classification Viewer", version="3.0")

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Create static dir if not exists
if not STATIC_DIR.exists():
    STATIC_DIR.mkdir()

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Path to the specific classification result file
# This is now the MERGED file containing Main Papers + Supp Sections
DATA_FILE = BASE_DIR.parent / "08_merged_data" / "merged_results.jsonl"

# Global cache
_papers_cache: List[Dict[str, Any]] = []
_stats_cache: Dict[str, Any] = {}
_current_file: Optional[Path] = None

def find_latest_jsonl() -> Optional[Path]:
    if DATA_FILE.exists():
        return DATA_FILE
    return None

def load_data(force_reload: bool = False) -> List[Dict[str, Any]]:
    """
    Load MERGED JSONL (Paper-based).
    Structure per line (from 08_merge.py):
    {
        "doc_id": "...", 
        "title": "...",
        "meta_title": "...",
        "sections": [... main pdf ...],
        "supplementary_sections": [ ... classified sections (YES/NO) ... ]
    }
    
    We map this to the Viewer's expected structure.
    """
    global _papers_cache, _stats_cache, _current_file
    
    if _papers_cache and not force_reload:
        return _papers_cache
    
    jsonl_path = find_latest_jsonl()
    if not jsonl_path:
        print(f"Data file not found: {DATA_FILE}")
        return []
    
    _current_file = jsonl_path
    
    papers = []
    
    try:
        print(f"Loading data from {jsonl_path}...")
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for line_idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    p_raw = json.loads(line)
                    
                    # Extract fields
                    # Prioritize meta_title from XML, fallback to title
                    title = p_raw.get("meta_title") or p_raw.get("title") or "Untitled"
                    doc_id = p_raw.get("paper_id") or p_raw.get("doc_id") or f"doc_{line_idx}"
                    source_file = p_raw.get("source_file", "")
                    
                    # Sections for this viewer are the SUPPLIMENTARY SECTIONS
                    # because the UI is built for YES/NO classification
                    sections_raw = p_raw.get("supplementary_sections", [])
                    
                    # Process sections
                    counts = Counter()
                    processed_sections = []
                    
                    for sec in sections_raw:
                        decision = sec.get("decision", "SKIP").upper()
                        counts[decision] += 1
                        
                        # Normalize keys if needed
                        # The viewer expects: heading, decision, confidence, reason, content_excerpt
                        processed_sections.append(sec)
                    
                    # Sort sections
                    # Try to sort numerically if path is like "1.2.3"
                    def sort_key(s):
                        try:
                            # If section_path is missing or not parseable, put it last or first
                            path_str = str(s.get("section_path", "0"))
                            parts = path_str.split('.')
                            # Convert to ints where possible
                            return tuple(int(p) if p.isdigit() else p for p in parts)
                        except:
                            return (0,)
                    
                    # Sort sections 
                    processed_sections.sort(key=sort_key)
                    
                    paper_obj = {
                        "idx": len(papers),
                        "doc_id": doc_id,
                        "title": title,
                        "source_file": source_file,
                        "sections": processed_sections, # Only showing Supp sections here
                        "yes_count": counts["YES"],
                        "no_count": counts["NO"],
                        "total_sections": len(processed_sections),
                        # Store raw if we ever need main pdf sections, but don't send to frontend by default to save bandwidth
                        # "raw_main_sections": p_raw.get("sections", []) 
                    }
                    papers.append(paper_obj)
                    
                except json.JSONDecodeError:
                    continue

        # Sort papers: prioritize those with YES sections, then by ID
        papers.sort(key=lambda x: (-x["yes_count"], x["doc_id"]))
        
        # Re-index after sort
        for i, p in enumerate(papers):
            p["idx"] = i
            
        _papers_cache = papers
        _stats_cache = {} 
        print(f"Loaded {len(papers)} papers from merged file.")
        
    except Exception as e:
        print(f"Error loading data: {e}")
        return []
        
    return _papers_cache

def calculate_stats(papers: List[Dict]):
    if _stats_cache:
        return _stats_cache
        
    total_papers = len(papers)
    total_sections = 0
    total_yes = 0
    total_no = 0
    
    for p in papers:
        total_sections += p["total_sections"]
        total_yes += p["yes_count"]
        total_no += p["no_count"]
        
    stats = {
        "total_papers": total_papers,
        "total_sections": total_sections,
        "total_yes": total_yes,
        "total_no": total_no,
        "yes_percent": round(total_yes / total_sections * 100, 1) if total_sections else 0
    }
    return stats

# --- Routes ---

@app.get("/")
async def read_root():
    return FileResponse(str(TEMPLATES_DIR / "index.html"))

@app.get("/api/papers")
async def api_papers(search: str = None):
    papers = load_data()
    results = []
    
    query = search.lower() if search else None
    
    for p in papers:
        # Filter if search
        if query:
            if (query not in p["title"].lower()) and (query not in p["doc_id"].lower()):
                continue
        
        # Light payload for sidebar
        results.append({
            "idx": p["idx"],
            "doc_id": p["doc_id"],
            "title": p["title"],
            "yes_count": p["yes_count"],
            "total_sections": p["total_sections"]
        })
        
    return JSONResponse(results)

@app.get("/api/paper/{idx}")
async def api_paper_detail(idx: int):
    papers = load_data()
    if idx < 0 or idx >= len(papers):
        return JSONResponse({"error": "Not found"}, status_code=404)
    
    return JSONResponse(papers[idx])

@app.get("/api/stats")
async def api_stats():
    papers = load_data()
    return JSONResponse(calculate_stats(papers))

if __name__ == "__main__":
    import uvicorn
    # Using 8012 port to distinguish from others
    uvicorn.run("main:app", host="127.0.0.1", port=8012, reload=True)