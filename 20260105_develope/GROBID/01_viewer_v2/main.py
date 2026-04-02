"""
TEI Paper Viewer v2
GROBID Hierarchical Paper Viewer - grobid_results_all.json 기반
"""
from fastapi import FastAPI, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from collections import Counter

app = FastAPI(title="TEI Paper Viewer v2", version="2.0")

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Path to the JSON file
JSON_PATH = BASE_DIR.parent / "01_run_out_v2" / "grobid_results_all.json"

# Global data cache
_data_cache: List[Dict[str, Any]] = []
_stats_cache: Dict[str, Any] = {}


def load_data() -> List[Dict[str, Any]]:
    """Load and cache TEI parsed data from JSON"""
    global _data_cache
    
    if _data_cache:
        return _data_cache
    
    if not JSON_PATH.exists():
        print(f"JSON file not found: {JSON_PATH}")
        return []
    
    try:
        with open(JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Add index and paper_id to each document
        for idx, doc in enumerate(data):
            source = doc.get('source_file', '')
            paper_id = Path(source).stem if source else f"paper_{idx}"
            doc['paper_id'] = paper_id
            doc['idx'] = idx
            
            # Check for errors
            if 'error' in doc:
                doc['has_error'] = True
            else:
                doc['has_error'] = False
        
        _data_cache = data
        print(f"Loaded {len(data)} papers from {JSON_PATH}")
    except Exception as e:
        print(f"Error loading JSON: {e}")
        return []
    
    return data


def count_sections_recursive(sections: List[Dict], include_children: bool = True) -> Dict[str, int]:
    """Recursively count paragraphs and sentences in sections"""
    total_sections = 0
    total_paragraphs = 0
    total_sentences = 0
    
    for section in sections:
        total_sections += 1
        paragraphs = section.get('paragraphs', [])
        sentences = section.get('sentences', [])
        
        total_paragraphs += len(paragraphs)
        for sent_list in sentences:
            total_sentences += len(sent_list)
        
        # Recursively count children
        if include_children and section.get('children'):
            child_counts = count_sections_recursive(section['children'], True)
            total_sections += child_counts['sections']
            total_paragraphs += child_counts['paragraphs']
            total_sentences += child_counts['sentences']
    
    return {
        'sections': total_sections,
        'paragraphs': total_paragraphs,
        'sentences': total_sentences
    }


def calculate_statistics(data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate comprehensive statistics from the paper data"""
    global _stats_cache
    
    if _stats_cache:
        return _stats_cache
    
    if not data:
        return {}
    
    total_papers = len(data)
    papers_with_error = sum(1 for d in data if d.get('has_error'))
    papers_with_abstract = 0
    
    total_sections = 0
    total_paragraphs = 0
    total_sentences = 0
    section_headings = Counter()
    
    for doc in data:
        if doc.get('has_error'):
            continue
            
        # Count abstracts
        abstract = doc.get('abstract_paragraphs', [])
        if abstract and len(abstract) > 0:
            papers_with_abstract += 1
        
        # Count sections recursively
        sections = doc.get('sections', [])
        counts = count_sections_recursive(sections, include_children=True)
        total_sections += counts['sections']
        total_paragraphs += counts['paragraphs']
        total_sentences += counts['sentences']
        
        # Count section headings (only top-level for simplicity)
        for section in sections:
            heading = section.get('heading', 'Untitled')
            # Normalize heading (remove numbering for grouping)
            normalized = heading.strip()
            if normalized:
                section_headings[normalized] += 1
    
    valid_papers = total_papers - papers_with_error
    
    # Get top 20 most common section headings
    top_sections = section_headings.most_common(20)
    
    stats = {
        "total_papers": total_papers,
        "valid_papers": valid_papers,
        "papers_with_error": papers_with_error,
        "papers_with_abstract": papers_with_abstract,
        "abstract_percent": round(100 * papers_with_abstract / valid_papers, 1) if valid_papers > 0 else 0,
        "total_sections": total_sections,
        "avg_sections_per_paper": round(total_sections / valid_papers, 1) if valid_papers > 0 else 0,
        "total_paragraphs": total_paragraphs,
        "avg_paragraphs_per_paper": round(total_paragraphs / valid_papers, 1) if valid_papers > 0 else 0,
        "total_sentences": total_sentences,
        "avg_sentences_per_paper": round(total_sentences / valid_papers, 1) if valid_papers > 0 else 0,
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
        if doc.get('has_error'):
            continue
            
        title = doc.get('title', '').lower()
        if query_lower in title:
            results.append(doc)
            continue
        
        # Search in abstract
        abstract = doc.get('abstract_paragraphs', [])
        if any(query_lower in p.lower() for p in abstract):
            results.append(doc)
            continue
        
        # Search in paper_id
        paper_id = doc.get('paper_id', '').lower()
        if query_lower in paper_id:
            results.append(doc)
            continue
    
    return results


def flatten_sections(sections: List[Dict], level: int = 0) -> List[Dict]:
    """Flatten hierarchical sections into a flat list with level info"""
    flat = []
    
    for section in sections:
        flat_section = {
            'level': section.get('level', level + 1),
            'heading': section.get('heading', ''),
            'paragraphs': section.get('paragraphs', []),
            'sentences': section.get('sentences', []),
            'has_children': bool(section.get('children'))
        }
        flat.append(flat_section)
        
        # Add children recursively
        if section.get('children'):
            child_flat = flatten_sections(section['children'], level + 1)
            flat.extend(child_flat)
    
    return flat


@app.get("/")
async def read_root(request: Request):
    """Main viewer with sidebar"""
    data = load_data()
    stats = calculate_statistics(data)
    
    # Prepare paper list for sidebar
    paper_list = []
    for doc in data:
        if doc.get('has_error'):
            paper_list.append({
                "idx": doc.get('idx', 0),
                "paper_id": doc.get('paper_id', ''),
                "title": f"[ERROR] {doc.get('paper_id', 'Unknown')}",
                "full_title": f"Error: {doc.get('error', 'Unknown error')}",
                "section_count": 0,
                "has_abstract": False,
                "has_error": True
            })
        else:
            title = doc.get('title', 'Untitled')
            section_counts = count_sections_recursive(doc.get('sections', []))
            paper_list.append({
                "idx": doc.get('idx', 0),
                "paper_id": doc.get('paper_id', ''),
                "title": title[:80] + ('...' if len(title) > 80 else ''),
                "full_title": title,
                "section_count": section_counts['sections'],
                "paragraph_count": section_counts['paragraphs'],
                "sentence_count": section_counts['sentences'],
                "has_abstract": len(doc.get('abstract_paragraphs', [])) > 0,
                "has_error": False
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
        if doc.get('has_error'):
            paper_list.append({
                "idx": doc.get('idx', 0),
                "paper_id": doc.get('paper_id', ''),
                "title": f"[ERROR] {doc.get('paper_id', 'Unknown')}",
                "section_count": 0,
                "has_abstract": False,
                "has_error": True
            })
        else:
            section_counts = count_sections_recursive(doc.get('sections', []))
            paper_list.append({
                "idx": doc.get('idx', 0),
                "paper_id": doc.get('paper_id', ''),
                "title": doc.get('title', 'Untitled'),
                "section_count": section_counts['sections'],
                "paragraph_count": section_counts['paragraphs'],
                "sentence_count": section_counts['sentences'],
                "has_abstract": len(doc.get('abstract_paragraphs', [])) > 0,
                "has_error": False
            })
    
    return JSONResponse({"papers": paper_list, "count": len(paper_list)})


@app.get("/api/paper/{paper_idx}")
async def get_paper(paper_idx: int, view_mode: str = Query("paragraph")):
    """Get single paper content with view mode"""
    data = load_data()
    
    if paper_idx < 0 or paper_idx >= len(data):
        return JSONResponse({"error": "Paper not found"}, status_code=404)
    
    doc = data[paper_idx]
    
    if doc.get('has_error'):
        return JSONResponse({
            "error": doc.get('error', 'Unknown error'),
            "paper_id": doc.get('paper_id', ''),
            "source_file": doc.get('source_file', '')
        })
    
    # Flatten sections for easier display
    flat_sections = flatten_sections(doc.get('sections', []))
    
    # Build content based on view mode
    content = {
        "paper_id": doc.get('paper_id', ''),
        "title": doc.get('title', 'Untitled'),
        "source_file": doc.get('source_file', ''),
        "abstract": doc.get('abstract_paragraphs', []),
        "view_mode": view_mode,
        "sections": []
    }
    
    for sec_idx, section in enumerate(flat_sections):
        heading = section.get('heading', 'Untitled Section')
        paragraphs = section.get('paragraphs', [])
        sentences = section.get('sentences', [])
        level = section.get('level', 1)
        
        section_data = {
            "idx": sec_idx,
            "level": level,
            "heading": heading,
            "paragraph_count": len(paragraphs),
            "sentence_count": sum(len(s) for s in sentences),
            "has_children": section.get('has_children', False),
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
    
    def count_headings(sections):
        for section in sections:
            heading = section.get('heading', 'Untitled')
            if heading:
                headings[heading] += 1
            if section.get('children'):
                count_headings(section['children'])
    
    for doc in data:
        if not doc.get('has_error'):
            count_headings(doc.get('sections', []))
    
    return JSONResponse({
        "headings": dict(headings.most_common(100)),
        "total_unique": len(headings)
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8003, reload=True)