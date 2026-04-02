"""
TEI Paper Sentence/Paragraph Viewer
A commercial-grade viewer for GROBID parsed scientific papers
"""
from fastapi import FastAPI, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from collections import Counter
import re

app = FastAPI(title="TEI Paper Viewer", version="2.0")

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Path to the JSONL file
JSONL_PATH = BASE_DIR.parent / "01_run_out" / "tei_paragraph_sentence.jsonl"

# Global data cache
_data_cache: List[Dict[str, Any]] = []
_stats_cache: Dict[str, Any] = {}


def load_data() -> List[Dict[str, Any]]:
    """Load and cache TEI parsed data from JSONL"""
    global _data_cache
    
    if _data_cache:
        return _data_cache
    
    if not JSONL_PATH.exists():
        print(f"JSONL file not found: {JSONL_PATH}")
        return []
    
    data = []
    try:
        with open(JSONL_PATH, 'r', encoding='utf-8') as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                    # Extract paper ID from filename
                    source = doc.get('source_file', '')
                    paper_id = Path(source).stem if source else f"paper_{idx}"
                    doc['paper_id'] = paper_id
                    doc['idx'] = idx
                    data.append(doc)
                except json.JSONDecodeError as e:
                    print(f"Error parsing line {idx}: {e}")
                    continue
        
        _data_cache = data
        print(f"Loaded {len(data)} papers from {JSONL_PATH}")
    except Exception as e:
        print(f"Error loading JSONL: {e}")
        return []
    
    return data


def calculate_statistics(data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate comprehensive statistics from the paper data"""
    global _stats_cache
    
    if _stats_cache:
        return _stats_cache
    
    if not data:
        return {}
    
    total_papers = len(data)
    total_sections = 0
    total_paragraphs = 0
    total_sentences = 0
    section_headings = Counter()
    papers_with_abstract = 0
    
    word_counts = []
    sentence_per_paper = []
    
    for doc in data:
        # Count abstracts
        abstract = doc.get('abstract_paragraphs', [])
        if abstract and len(abstract) > 0:
            papers_with_abstract += 1
        
        # Count sections
        sections = doc.get('sections', [])
        total_sections += len(sections)
        
        doc_sentences = 0
        doc_words = 0
        
        for section in sections:
            heading = section.get('heading', 'Untitled')
            section_headings[heading] += 1
            
            paragraphs = section.get('paragraphs', [])
            total_paragraphs += len(paragraphs)
            
            # Count words from paragraphs
            for para in paragraphs:
                doc_words += len(para.split())
            
            sentences = section.get('sentences', [])
            for sent_list in sentences:
                total_sentences += len(sent_list)
                doc_sentences += len(sent_list)
        
        word_counts.append(doc_words)
        sentence_per_paper.append(doc_sentences)
    
    # Get top 20 most common section headings
    top_sections = section_headings.most_common(20)
    
    stats = {
        "total_papers": total_papers,
        "papers_with_abstract": papers_with_abstract,
        "abstract_percent": round(100 * papers_with_abstract / total_papers, 1) if total_papers > 0 else 0,
        "total_sections": total_sections,
        "avg_sections_per_paper": round(total_sections / total_papers, 1) if total_papers > 0 else 0,
        "total_paragraphs": total_paragraphs,
        "avg_paragraphs_per_paper": round(total_paragraphs / total_papers, 1) if total_papers > 0 else 0,
        "total_sentences": total_sentences,
        "avg_sentences_per_paper": round(total_sentences / total_papers, 1) if total_papers > 0 else 0,
        "avg_words_per_paper": round(sum(word_counts) / total_papers, 0) if total_papers > 0 else 0,
        "top_section_headings": dict(top_sections),
        "unique_section_headings": len(section_headings)
    }
    
    _stats_cache = stats
    return stats


def search_papers(data: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Search papers by title or content"""
    if not query:
        return data
    
    query_lower = query.lower()
    results = []
    
    for doc in data:
        title = doc.get('title', '').lower()
        if query_lower in title:
            results.append(doc)
            continue
        
        # Search in abstract
        abstract = doc.get('abstract_paragraphs', [])
        if any(query_lower in p.lower() for p in abstract):
            results.append(doc)
            continue
        
        # Search in sections
        sections = doc.get('sections', [])
        found = False
        for section in sections:
            heading = section.get('heading', '').lower()
            if query_lower in heading:
                found = True
                break
            paragraphs = section.get('paragraphs', [])
            if any(query_lower in p.lower() for p in paragraphs):
                found = True
                break
        
        if found:
            results.append(doc)
    
    return results


@app.get("/")
async def read_root(request: Request):
    """Main viewer with sidebar"""
    data = load_data()
    stats = calculate_statistics(data)
    
    # Prepare paper list for sidebar
    paper_list = []
    for doc in data:
        paper_list.append({
            "idx": doc.get('idx', 0),
            "paper_id": doc.get('paper_id', ''),
            "title": doc.get('title', 'Untitled')[:80] + ('...' if len(doc.get('title', '')) > 80 else ''),
            "full_title": doc.get('title', 'Untitled'),
            "section_count": len(doc.get('sections', [])),
            "has_abstract": len(doc.get('abstract_paragraphs', [])) > 0
        })
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "paper_list": paper_list,
        "stats": stats,
        "total_papers": len(data)
    })


@app.get("/api/papers")
async def get_papers(search: str = Query(None)):
    """API endpoint to get paper list with search"""
    data = load_data()
    
    if search:
        data = search_papers(data, search)
    
    paper_list = []
    for doc in data:
        paper_list.append({
            "idx": doc.get('idx', 0),
            "paper_id": doc.get('paper_id', ''),
            "title": doc.get('title', 'Untitled'),
            "section_count": len(doc.get('sections', [])),
            "has_abstract": len(doc.get('abstract_paragraphs', [])) > 0
        })
    
    return JSONResponse({"papers": paper_list, "count": len(paper_list)})


@app.get("/api/paper/{paper_idx}")
async def get_paper(paper_idx: int, view_mode: str = Query("paragraph")):
    """Get single paper content with view mode"""
    data = load_data()
    
    if paper_idx < 0 or paper_idx >= len(data):
        return JSONResponse({"error": "Paper not found"}, status_code=404)
    
    doc = data[paper_idx]
    
    # Build content based on view mode
    content = {
        "paper_id": doc.get('paper_id', ''),
        "title": doc.get('title', 'Untitled'),
        "source_file": doc.get('source_file', ''),
        "abstract": doc.get('abstract_paragraphs', []),
        "view_mode": view_mode,
        "sections": []
    }
    
    sections = doc.get('sections', [])
    
    for sec_idx, section in enumerate(sections):
        heading = section.get('heading', 'Untitled Section')
        paragraphs = section.get('paragraphs', [])
        sentences = section.get('sentences', [])
        
        section_data = {
            "idx": sec_idx,
            "heading": heading,
            "paragraph_count": len(paragraphs),
            "sentence_count": sum(len(s) for s in sentences),
            "content": []
        }
        
        if view_mode == "sentence":
            # Sentence view: flatten all sentences
            for para_idx, sent_list in enumerate(sentences):
                for sent_idx, sentence in enumerate(sent_list):
                    section_data["content"].append({
                        "type": "sentence",
                        "para_idx": para_idx,
                        "sent_idx": sent_idx,
                        "text": sentence
                    })
        else:
            # Paragraph view
            for para_idx, paragraph in enumerate(paragraphs):
                sent_count = len(sentences[para_idx]) if para_idx < len(sentences) else 0
                section_data["content"].append({
                    "type": "paragraph",
                    "para_idx": para_idx,
                    "text": paragraph,
                    "sentence_count": sent_count
                })
        
        content["sections"].append(section_data)
    
    return JSONResponse(content)


@app.get("/api/stats")
async def get_stats():
    """API endpoint for statistics"""
    data = load_data()
    return JSONResponse(calculate_statistics(data))


@app.get("/api/section-headings")
async def get_section_headings():
    """Get all unique section headings with counts"""
    data = load_data()
    headings = Counter()
    
    for doc in data:
        for section in doc.get('sections', []):
            heading = section.get('heading', 'Untitled')
            headings[heading] += 1
    
    return JSONResponse({
        "headings": dict(headings.most_common(100)),
        "total_unique": len(headings)
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8200, reload=True)