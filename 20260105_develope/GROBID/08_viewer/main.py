
import uvicorn
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
import json
import os
from pathlib import Path
from typing import List, Dict, Any, Optional

# --- Configuration ---
BASE_DIR = Path(__file__).resolve().parent
MERGED_DATA_PATH = BASE_DIR.parent / "08_merged_data" / "merged_results.jsonl"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="Advanced Paper Viewer", version="3.0")

# Mount Static Files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --- Data Cache ---
_data_cache: List[Dict[str, Any]] = []

def load_data(force_reload: bool = False):
    global _data_cache
    if _data_cache and not force_reload:
        return _data_cache
    
    data = []
    if not MERGED_DATA_PATH.exists():
        print(f"Warning: Data file not found at {MERGED_DATA_PATH}")
        return []

    try:
        with open(MERGED_DATA_PATH, 'r', encoding='utf-8') as f:
            for idx, line in enumerate(f):
                if not line.strip(): continue
                try:
                    doc = json.loads(line)
                    # Ensure basic fields
                    if 'paper_id' not in doc:
                        source = doc.get('source_file', '')
                        # Extract simple ID if possible, else use index
                        doc['paper_id'] = f"Paper_{idx}" # Fallback
                        # Try to find ID in source
                        import re
                        match = re.search(r'1-s2\.0-([A-Z0-9]+)', source)
                        if match:
                            doc['paper_id'] = match.group(1)
                    
                    doc['idx'] = idx
                    data.append(doc)
                except json.JSONDecodeError:
                    pass
        _data_cache = data
        print(f"Loaded {len(data)} papers.")
    except Exception as e:
        print(f"Error loading data: {e}")
    
    return _data_cache

def get_paper_summary(doc: Dict) -> Dict:
    """Return a lightweight summary for the sidebar list."""
    supp_objs = doc.get('supplementary_sections', [])
    if not supp_objs:
        supp_objs = doc.get('supplementary_files', [])
        
    return {
        "idx": doc.get('idx'),
        "paper_id": doc.get('paper_id'),
        "title": doc.get('meta_title') or doc.get('title') or "Untitled",
        "has_abstract": bool(doc.get('meta_abstract') or doc.get('abstract_paragraphs')),
        "supp_count": len(supp_objs) if isinstance(supp_objs, list) else 0
    }

# --- Routes ---

@app.get("/")
async def root():
    return FileResponse(str(TEMPLATES_DIR / "index.html"))

@app.get("/api/papers")
async def api_papers():
    data = load_data()
    # Return summaries only
    summaries = [get_paper_summary(d) for d in data]
    return {"papers": summaries, "count": len(summaries)}

@app.get("/api/paper/{idx}")
async def api_paper_detail(idx: int):
    data = load_data()
    if 0 <= idx < len(data):
        doc = data[idx]
        return doc
    raise HTTPException(status_code=404, detail="Paper not found")

@app.get("/api/stats")
async def api_stats():
    data = load_data()
    total_papers = len(data)
    with_xml_abs = sum(1 for d in data if d.get('meta_abstract'))
    with_supp = sum(1 for d in data if (d.get('supplementary_sections') or d.get('supplementary_files')))
    
    return {
        "total_papers": total_papers,
        "with_xml_abstract": with_xml_abs,
        "with_supplementary": with_supp
    }

@app.get("/api/open_file")
async def open_file(path: str):
    """
    Attempts to open a local file.
    Only works if the server is running locally on the user's machine.
    """
    import subprocess
    import platform
    
    if not os.path.exists(path):
        return JSONResponse({"status": "error", "message": "File not found"}, status_code=404)

    try:
        if platform.system() == 'Windows':
            os.startfile(path)
        elif platform.system() == 'Darwin':
            subprocess.call(('open', path))
        else:
            subprocess.call(('xdg-open', path))
        return {"status": "success", "message": f"Opened {path}"}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

if __name__ == "__main__":
    # Pre-load data
    load_data()
    uvicorn.run("main:app", host="127.0.0.1", port=8011, reload=False)