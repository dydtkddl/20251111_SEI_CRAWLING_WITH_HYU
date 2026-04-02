"""
Stage 2 Verified Experimental Data Viewer
Commercial-grade viewer for LLM-filtered scientific papers.
Includes support for viewing Kept vs Removed sections with extraction reasoning.
"""
from fastapi import FastAPI, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from collections import Counter, defaultdict
import glob
import uvicorn

app = FastAPI(title="Stage 2 Viewer", version="2.0")

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Path to filtered data directory
FILTERED_DATA_DIR = BASE_DIR.parent / "05_llm_filtered_data"

# Global data cache
_papers_cache: List[Dict[str, Any]] = []
_stats_cache: Dict[str, Any] = {}
_loading_source: str = ""

def find_latest_file() -> Optional[Path]:
    """Find latest Unified JSONL file."""
    pattern = str(FILTERED_DATA_DIR / "llm_filtered_all_*.jsonl")
    files = glob.glob(pattern)
    if not files:
        return None
    return Path(max(files, key=lambda x: Path(x).stat().st_mtime))

def normalize_path(p: str) -> str:
    if not p: return ""
    return p.strip().replace("\\", "/")

def load_data(force_reload: bool = False) -> List[Dict[str, Any]]:
    global _papers_cache, _stats_cache, _loading_source
    
    if _papers_cache and not force_reload:
        return _papers_cache
    
    # file_path = find_latest_file()
    file_path = Path("../05_llm_filtered_data/llm_filtered_all_20260108_114742.jsonl")
    print(file_path)
    if not file_path:
        print("No Unified Data found.")
        return []
        
    _loading_source = file_path.name
    papers_list = []
    
    print(f"Loading data from {file_path}...")
    with open(file_path, 'r', encoding='utf-8') as f:
        for idx, line in enumerate(f):
            if not line.strip(): continue
            try:
                doc = json.loads(line)
                
                # Split sections into kept/removed on the fly
                sections = doc.get("sections", [])
                kept = []
                removed = []
                
                for sec in sections:
                    if sec.get("llm_decision") == "YES":
                        kept.append(sec)
                    else:
                        removed.append(sec)
                
                doc['idx'] = idx
                doc['paper_id'] = Path(doc.get('source_file')).stem
                doc['kept_sections'] = kept
                doc['removed_sections'] = removed
                
                papers_list.append(doc)
            except Exception as e:
                print(f"Error parse line {idx}: {e}")
                continue
    
    # Sort
    papers_list.sort(key=lambda x: x['paper_id'])
    
    # UI Index
    for i, p in enumerate(papers_list):
        p['ui_idx'] = i
        
    _papers_cache = papers_list
    print(f"Loaded {len(_papers_cache)} papers.")
    _stats_cache = {}
    return _papers_cache

def calculate_stats(papers: List[Dict]) -> Dict:
    global _stats_cache
    if _stats_cache: return _stats_cache
    
    total_papers = len(papers)
    total_kept_sections = 0
    total_removed_sections = 0
    
    rejection_reasons = Counter()
    
    for p in papers:
        total_kept_sections += len(p.get('kept_sections', []))
        removed = p.get('removed_sections', [])
        total_removed_sections += len(removed)
        
        for r in removed:
            reason = r.get('llm_reason', 'Unknown')
            # Simplify reason for chart
            if "Stage1" in reason: reason_cat = "Heading Filter (Stage 1)"
            elif "No candidate" in reason: reason_cat = "No Candidates (Low Score)"
            elif "rejected by LLM" in reason: reason_cat = "LLM Verification Failed"
            else: reason_cat = "Other"
            rejection_reasons[reason_cat] += 1
            
    _stats_cache = {
        "total_papers": total_papers,
        "total_kept_sections": total_kept_sections,
        "total_removed_sections": total_removed_sections,
        "rejection_reasons": dict(rejection_reasons),
        "avg_kept_per_paper": round(total_kept_sections/total_papers, 1) if total_papers else 0
    }
    return _stats_cache

@app.get("/")
async def read_root(request: Request):
    data = load_data()
    stats = calculate_stats(data)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "stats": stats,
        "source_name": _loading_source
    })

@app.get("/api/papers")
async def api_papers(q: str = None):
    data = load_data()
    results = []
    
    query = q.lower() if q else None
    
    for p in data:
        if query:
            if query not in p['title'].lower() and query not in p['paper_id'].lower():
                continue
                
        results.append({
            "ui_idx": p['ui_idx'],
            "paper_id": p['paper_id'],
            "title": p['title'],
            "kept_count": len(p['kept_sections']),
            "removed_count": len(p['removed_sections']),
            "has_abstract": bool(p.get("abstract_paragraphs"))
        })
        
    return JSONResponse(results)

@app.get("/api/paper/{ui_idx}")
async def api_paper_detail(ui_idx: int):
    data = load_data()
    if ui_idx < 0 or ui_idx >= len(data):
        return JSONResponse({"error": "Not found"}, status_code=404)
        
    paper = data[ui_idx]
    return JSONResponse(paper)

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8767, reload=True)