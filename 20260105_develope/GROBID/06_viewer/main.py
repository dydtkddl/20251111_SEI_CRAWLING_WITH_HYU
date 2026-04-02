"""
Filtered Experimental Data Viewer
정제된 Experimental Section 전용 뷰어
"""
from fastapi import FastAPI, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from collections import Counter
import glob

app = FastAPI(title="Supplementary Data Viewer", version="2.0")

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Path to specific supplementary data file
DATA_FILE = BASE_DIR.parent / "06_run_supplementary_out" / "supplementary_grobid_results.jsonl"

# Global data cache
_data_cache: List[Dict[str, Any]] = []
_stats_cache: Dict[str, Any] = {}
_current_file: Optional[Path] = None


def find_latest_jsonl() -> Optional[Path]:
    """Return the specific supplementary data file if it exists"""
    if DATA_FILE.exists():
        return DATA_FILE
    return None


def load_data(force_reload: bool = False) -> List[Dict[str, Any]]:
    """Load supplementary data from JSONL"""
    global _data_cache, _current_file
    
    if _data_cache and not force_reload:
        return _data_cache
    
    jsonl_path = find_latest_jsonl()
    
    if not jsonl_path:
        print(f"Data file not found: {DATA_FILE}")
        return []
    
    _current_file = jsonl_path
    data = []
    
    try:
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                    # Use filename as ID, fallback to idx
                    source = doc.get('source_file', '')
                    paper_id = Path(source).stem if source else f"supp_{idx}"
                    
                    # Clean up title if empty
                    if not doc.get('title'):
                        doc['title'] = paper_id
                        
                    doc['paper_id'] = paper_id
                    doc['idx'] = idx
                    data.append(doc)
                except json.JSONDecodeError as e:
                    print(f"Error parsing line {idx}: {e}")
                    continue
        
        _data_cache = data
        print(f"Loaded {len(data)} supplementary papers from {jsonl_path.name}")
    except Exception as e:
        print(f"Error loading JSONL: {e}")
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
    """Calculate comprehensive statistics"""
    global _stats_cache
    
    if _stats_cache:
        return _stats_cache
    
    if not data:
        return {}
    
    total_papers = len(data)
    papers_with_abstract = 0
    
    total_sections = 0
    total_paragraphs = 0
    total_sentences = 0
    section_headings = Counter()
    section_levels = Counter()
    
    # 섹션 타입 분류
    preparation_sections = 0
    synthesis_sections = 0
    measurement_sections = 0
    material_sections = 0
    
    for doc in data:
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
        
        # Analyze section types
        def analyze_sections(secs, level=1):
            nonlocal preparation_sections, synthesis_sections, measurement_sections, material_sections
            
            for section in secs:
                heading = section.get('heading', '').lower()
                section_headings[heading] += 1
                section_levels[section.get('level', level)] += 1
                
                # Categorize sections
                if 'preparation' in heading or 'fabrication' in heading:
                    preparation_sections += 1
                if 'synthesis' in heading:
                    synthesis_sections += 1
                if 'measurement' in heading or 'test' in heading or 'electrochemical' in heading:
                    measurement_sections += 1
                if 'material' in heading:
                    material_sections += 1
                
                if section.get('children'):
                    analyze_sections(section['children'], level + 1)
        
        analyze_sections(sections)
    
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
        "avg_sentences_per_section": round(total_sentences / total_sections, 1) if total_sections > 0 else 0,
        "top_section_headings": dict(top_sections),
        "unique_section_headings": len(section_headings),
        "section_levels": dict(section_levels),
        "preparation_sections": preparation_sections,
        "synthesis_sections": synthesis_sections,
        "measurement_sections": measurement_sections,
        "material_sections": material_sections,
        "data_source": _current_file.name if _current_file else "Unknown"
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
        
        # Search in paper_id
        paper_id = doc.get('paper_id', '').lower()
        if query_lower in paper_id:
            results.append(doc)
            continue
    
    return results


def flatten_sections(sections: List[Dict], level: int = 0) -> List[Dict]:
    """Flatten hierarchical sections"""
    flat = []
    
    for section in sections:
        flat_section = {
            'level': section.get('level', level + 1),
            'heading': section.get('heading', ''),
            'kind': section.get('kind', 'section'),
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
    """Main viewer"""
    data = load_data()
    stats = calculate_statistics(data)
    
    # Prepare paper list
    paper_list = []
    for doc in data:
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
        section_counts = count_sections_recursive(doc.get('sections', []))
        paper_list.append({
            "idx": doc.get('idx', 0),
            "paper_id": doc.get('paper_id', ''),
            "title": doc.get('title', 'Untitled'),
            "section_count": section_counts['sections'],
            "paragraph_count": section_counts['paragraphs'],
            "sentence_count": section_counts['sentences'],
            "has_abstract": len(doc.get('abstract_paragraphs', [])) > 0
        })
    
    return JSONResponse({"papers": paper_list, "count": len(paper_list)})


@app.get("/api/paper/{paper_idx}")
async def get_paper(paper_idx: int, view_mode: str = Query("paragraph")):
    """Get single paper content"""
    data = load_data()
    
    if paper_idx < 0 or paper_idx >= len(data):
        return JSONResponse({"error": "Paper not found"}, status_code=404)
    
    doc = data[paper_idx]
    
    # Flatten sections
    flat_sections = flatten_sections(doc.get('sections', []))
    
    # Build content
    content = {
        "paper_id": doc.get('paper_id', ''),
        "title": doc.get('title', 'Untitled'),
        "source_file": doc.get('source_file', ''),
        "original_file_type": doc.get('original_file_type', ''),
        "was_converted": doc.get('was_converted_from_word', False),
        "abstract": doc.get('abstract_paragraphs', []),
        "view_mode": view_mode,
        "sections": []
    }
    
    for sec_idx, section in enumerate(flat_sections):
        heading = section.get('heading', 'Untitled Section')
        kind = section.get('kind', 'section')
        paragraphs = section.get('paragraphs', [])
        sentences = section.get('sentences', [])
        level = section.get('level', 1)
        
        section_data = {
            "idx": sec_idx,
            "level": level,
            "heading": heading,
            "kind": kind,
            "paragraph_count": len(paragraphs),
            "sentence_count": sum(len(s) for s in sentences),
            "has_children": section.get('has_children', False),
            "content": []
        }
        
        if view_mode == "sentence":
            # Sentence view
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


@app.get("/api/reload")
async def reload_data():
    """Reload data from disk"""
    global _data_cache, _stats_cache
    _data_cache = []
    _stats_cache = {}
    data = load_data(force_reload=True)
    return JSONResponse({"status": "success", "papers_loaded": len(data)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8004, reload=True)