from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Union

app = FastAPI(title="Scientific Paper Structure Explorer")

# CORS needed if running frontend separately, but we serve static
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
BASE_DIR = Path(r"d:\20251111_SEI_CRAWLING_WITH_HYU\20260105_전달내용_및_develope\pdfs_marker_output")

class MarkdownParser:
    @staticmethod
    def parse(content: str) -> List[Dict[str, Any]]:
        """
        Parse markdown content into a flat list of sections based on headers.
        Returns a linear list of nodes.
        """
        lines = content.split('\n')
        nodes = []
        
        # Regex to match headers
        header_pattern = re.compile(r'^(#{1,6})\s+(.*)')
        
        current_content_buffer = []
        current_node = None

        def flush_content():
            if current_content_buffer:
                text = "\n".join(current_content_buffer).strip()
                if text:
                    if current_node:
                        current_node["content"].append(text)
                    else:
                        # Preamble node (content before first header)
                        preamble_node = {
                            "level": 0,
                            "tag": "PRE",
                            "title": "Preamble / Metadata",
                            "content": [text],
                            "id": "preamble"
                        }
                        # Only add preamble if we haven't started adding nodes yet, 
                        # or handle differently. For now let's insert at finding.
                        # But loop logic requires current_node to be set for appending.
                        # We will append a preamble node to nodes list directly.
                        # However, current_node reference needs update? No.
                        # Just append to nodes list.
                        # But simpler: if no current_node, create one.
                        pass # Skipping logic complication, let's fix below.

                current_content_buffer.clear()

        # Handle Preamble explicitly
        # We need a current_node from start if we want to capture top text?
        # Or parse loop handles it.
        
        for line in lines:
            line_stripped = line.strip()
            match = header_pattern.match(line)
            
            if match:
                # Flush previous text content
                if current_content_buffer and current_node:
                     text = "\n".join(current_content_buffer).strip()
                     if text: current_node["content"].append(text)
                     current_content_buffer.clear()
                elif current_content_buffer and not current_node:
                     # This is preamble
                     text = "\n".join(current_content_buffer).strip()
                     if text:
                         pre_node = {
                            "level": 0,
                            "tag": "HEAD",
                            "title": "Document Header / Preamble",
                            "content": [text],
                            "id": "preamble"
                         }
                         nodes.append(pre_node)
                     current_content_buffer.clear()
                
                hashes, title = match.groups()
                level = len(hashes)
                
                # Create new node
                current_node = {
                    "level": level,
                    "tag": hashes,
                    "title": title.strip(),
                    "content": [],
                    "id": f"s-{len(nodes)}-{hash(title)}"
                }
                nodes.append(current_node)
            else:
                current_content_buffer.append(line)
        
        # Final flush
        if current_content_buffer and current_node:
             text = "\n".join(current_content_buffer).strip()
             if text: current_node["content"].append(text)
        
        return nodes

@app.get("/api/files")
def list_files():
    """List all MD files in the directory recursively as a flat list."""
    md_files = []
    try:
        # Recursively find all .md files
        for path in BASE_DIR.rglob("*.md"):
            # Create a simple object for each file
            # Use specific parent folder name as a label/tag if useful, 
            # but main display is the filename
            md_files.append({
                "name": path.name,
                "path": str(path),
                "parent": path.parent.name,
                "type": "file"
            })
    except PermissionError:
        pass
        
    # Sort by name
    md_files.sort(key=lambda x: x["parent"]) # Group by folder visually in list
    return JSONResponse(content=md_files)

@app.get("/api/document")
def get_document(path: str):
    """Parse and return a specific MD file."""
    file_path = Path(path)
    
    # Security check: ensure path is within BASE_DIR
    try:
        file_path.relative_to(BASE_DIR)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
        
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        structure = MarkdownParser.parse(content)
        return {"structure": structure, "raw": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Sort of SPA catch-all
@app.get("/")
def read_root():
    return FileResponse("static/index.html")

app.mount("/", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    # Allow running directly
    uvicorn.run(app, host="127.0.0.1", port=8000)
