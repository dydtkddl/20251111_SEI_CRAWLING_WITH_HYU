
from fastapi import FastAPI, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import csv
import json
import os
import re
from pathlib import Path

app = FastAPI()

# Setup templates
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Path to the CSV file (parent directory)
CSV_PATH = BASE_DIR.parent / "03_keyword_check_results.csv"

def parse_filter_input(raw_input: str) -> set:
    if not raw_input:
        return set()
    
    # Remove Python/JSON syntax noise: { } [ ] ' "
    clean_text = re.sub(r"[{}[\],'\"]", " ", raw_input)
    # Split by whitespace/comma
    tokens = [t.strip() for t in re.split(r"[\s,]+", clean_text) if t.strip()]
    return set(tokens)

@app.get("/")
async def read_root(request: Request, filter_ids: str = Query(None)):
    data = []
    
    target_ids = parse_filter_input(filter_ids)
    
    if not CSV_PATH.exists():
        return templates.TemplateResponse("index.html", {
            "request": request, 
            "error": f"CSV file not found at {CSV_PATH}", 
            "data": [],
            "filter_ids": filter_ids or ""
        })

    try:
        with open(CSV_PATH, mode='r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                file_path = row.get("File", "Unknown")
                
                # Check Filter
                if target_ids:
                    # If filter provided, check if file path contains ANY of the IDs
                    is_target = any(pid in file_path for pid in target_ids)
                    if not is_target:
                        continue
                
                # Only show Found=True
                if row.get("Found") != "True":
                    continue
                
                # Support both old "Evidence" and new "EvidenceJSON" columns
                evidence_json = row.get("EvidenceJSON_Cleaned") or row.get("Evidence", "[]")
                
                try:
                    evidence_list = json.loads(evidence_json)
                except json.JSONDecodeError:
                    evidence_list = []
                
                data.append({
                    "file_path": file_path,
                    "evidence": evidence_list
                })
    except Exception as e:
        return templates.TemplateResponse("index.html", {
            "request": request, 
            "error": f"Error reading CSV: {str(e)}", 
            "data": [],
            "filter_ids": filter_ids or ""
        })

    return templates.TemplateResponse("index.html", {
        "request": request, 
        "data": data,
        "filter_ids": filter_ids or ""
    })

if __name__ == "__main__":
    import uvicorn
    # Auto-reload for development
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)