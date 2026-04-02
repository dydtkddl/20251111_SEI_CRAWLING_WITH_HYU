
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

# Path to the JSON file (parent directory)
JSON_PATH = BASE_DIR.parent / "01_merged_content.json"

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
    
    if not JSON_PATH.exists():
        return templates.TemplateResponse("index.html", {
            "request": request, 
            "error": f"JSON file not found at {JSON_PATH}", 
            "data": [],
            "filter_ids": filter_ids or ""
        })

    try:
        with open(JSON_PATH, mode='r', encoding='utf-8') as f:
            full_data = json.load(f)
            
            for file_key, sections in full_data.items():
                # Check Filter
                if target_ids:
                    # If filter provided, check if file key contains ANY of the IDs
                    is_target = any(pid.lower() in file_key.lower() for pid in target_ids)
                    if not is_target:
                        continue
                
                # Transform sections into a list compatible with the template
                section_list = []
                for sec_key, sec_val in sections.items():
                    section_list.append({
                        "heading": sec_key,
                        "content": sec_val.get("content", ""),
                        "level": sec_val.get("_matched_level", 1),
                        "source": sec_val.get("source", "Main Text"),
                        "matched_header": sec_val.get("_matched_header", sec_key)
                    })
                
                data.append({
                    "file_key": file_key,
                    "sections": section_list
                })
                
    except Exception as e:
        return templates.TemplateResponse("index.html", {
            "request": request, 
            "error": f"Error reading JSON: {str(e)}", 
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