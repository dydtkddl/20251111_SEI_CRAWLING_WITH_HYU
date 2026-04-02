"""
Pipeline Output Viewer v3.0
Full Pipeline Output 결과를 시각화하는 상업용 웹 뷰어
- 정렬/필터 기능 (S1 YES/NO, Ex-situ, Sections, Error 등)
- 텍스트 모달 내 검색/복사/라인번호 기능
- 키보드 단축키 (J/K 이동, / 검색, Esc 닫기)
- 모든 파이프라인 단계 추적 (prompts, raw outputs, evidence snippets)
- Gemini API 검수 기능
"""
import logging
import re
import os
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Query, UploadFile, File, Body, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from collections import Counter
import uvicorn
from tqdm import tqdm
from pydantic import BaseModel
import xml.etree.ElementTree as ET
import uuid

# Load environment variables
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("SEI_Viewer")

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
# DATA_DIR = BASE_DIR.parent / "09_20260115_full_pipeline_output"
DATA_DIR = BASE_DIR.parent / "09_20260115_full_pipeline_output_20260118"
# DATA_DIR = BASE_DIR.parent / "09_20260115_full_pipeline_output_v8"

# View Set storage path
SUMMARY_DIR = DATA_DIR / "summary"
VIEWSETS_FILE = SUMMARY_DIR / "viewsets.json"
METADATA_CACHE_FILE = SUMMARY_DIR / "metadata_cache.json"

# PDF files path (relative to project: ../../pdfs)
PDF_DIR = (BASE_DIR / "../../pdfs").resolve()

# Metadata XML path
META_XML_DIR = Path("D:/20251111_SEI_CRAWLING_WITH_HYU/Elsevier/xmls_meta_abs")

# FastAPI 앱 초기화
app = FastAPI(
    title="Pipeline Viewer v3.0",
    description="GROBID Full Pipeline Output Viewer with Enhanced Sorting, Filtering & Search",
    version="3.0",
    root_path="/SEI"
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Global data cache
_papers_cache: Dict[str, Dict[str, Any]] = {}
_stats_cache: Dict[str, Any] = {}

# PDF index cache: {paper_id: [{name, kind, size, mtime}, ...]}
_pdf_index_cache: Dict[str, List[Dict[str, Any]]] = {}
_pdf_index_built: bool = False

# Reviews directory and cache
REVIEWS_DIR = SUMMARY_DIR / "reviews"
_reviews_cache: Dict[str, Dict[str, Any]] = {}  # {paper_id: review_data}
_reviews_loaded: bool = False


# ============ View Set Models & Functions ============
class ViewSetCreate(BaseModel):
    name: str
    ids: List[str]
    note: Optional[str] = None


class ViewSetUpdate(BaseModel):
    ids: Optional[List[str]] = None
    note: Optional[str] = None


# Valid paper_id pattern (prevent path traversal)
PAPER_ID_PATTERN = re.compile(r'^[A-Za-z0-9._-]+$')


def load_viewsets() -> Dict[str, Any]:
    """Load viewsets from JSON file"""
    if not VIEWSETS_FILE.exists():
        return {"version": 1, "viewsets": {}}
    try:
        with open(VIEWSETS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load viewsets: {e}")
        return {"version": 1, "viewsets": {}}


def save_viewsets(data: Dict[str, Any]) -> bool:
    """Save viewsets to JSON file (atomic write)"""
    try:
        SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
        temp_file = VIEWSETS_FILE.with_suffix('.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_file.replace(VIEWSETS_FILE)
        return True
    except Exception as e:
        logger.error(f"Failed to save viewsets: {e}")
        return False


def validate_paper_ids(ids: List[str]) -> tuple[List[str], List[str], List[str]]:
    """Validate paper IDs: returns (valid_ids, invalid_ids, missing_ids)"""
    valid = []
    invalid = []
    missing = []
    
    for pid in ids:
        pid = pid.strip()
        if not pid:
            continue
        if not PAPER_ID_PATTERN.match(pid):
            invalid.append(pid)
        elif not (DATA_DIR / pid).exists():
            missing.append(pid)
        else:
            valid.append(pid)
    
    return valid, invalid, missing


def read_file_safe(filepath: Path, max_chars: int = None) -> Optional[str]:
    """Safely read a text file with optional truncation"""
    if not filepath.exists():
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            if max_chars and len(content) > max_chars:
                return content[:max_chars] + f"\n\n... [truncated, {len(content) - max_chars} more chars]"
            return content
    except Exception as e:
        logger.error(f"Error reading {filepath}: {e}")
        return None


# ============ Reviews Functions ============

def load_reviews() -> Dict[str, Dict[str, Any]]:
    """Load all reviews from REVIEWS_DIR"""
    global _reviews_cache, _reviews_loaded
    
    if _reviews_loaded:
        return _reviews_cache
    
    _reviews_cache = {}
    
    if not REVIEWS_DIR.exists():
        logger.info(f"Reviews directory not found, creating: {REVIEWS_DIR}")
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        _reviews_loaded = True
        return _reviews_cache
    
    logger.info(f"Loading reviews from: {REVIEWS_DIR}")
    
    for review_file in REVIEWS_DIR.glob("*.json"):
        try:
            with open(review_file, 'r', encoding='utf-8') as f:
                review_data = json.load(f)
                paper_id = review_data.get("paper_id")
                if paper_id:
                    _reviews_cache[paper_id] = review_data
        except Exception as e:
            logger.error(f"Error loading review {review_file}: {e}")
    
    _reviews_loaded = True
    logger.info(f"Loaded {len(_reviews_cache)} reviews")
    return _reviews_cache


def save_review(paper_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Save or update a review with atomic write"""
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Calculate agreement fields
    s1_agree = None
    s2_agree_exsitu = None
    
    if payload.get("stage1"):
        llm_pred = payload["stage1"].get("llm_pred")
        gold = payload["stage1"].get("gold")
        if llm_pred and gold:
            s1_agree = (llm_pred == gold)
            payload["stage1"]["agree"] = s1_agree
    
    if payload.get("stage2"):
        llm_pred = payload["stage2"].get("llm_pred_exsitu")
        gold = payload["stage2"].get("gold_exsitu")
        if llm_pred and gold:
            s2_agree_exsitu = (llm_pred == gold)
            payload["stage2"]["agree_exsitu"] = s2_agree_exsitu
    
    # Set updated_at timestamp
    from datetime import datetime, timezone
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if "created_at" not in payload:
        payload["created_at"] = payload["updated_at"]
    
    payload["paper_id"] = paper_id
    
    # Atomic write: write to temp file then rename
    review_file = REVIEWS_DIR / f"{paper_id}.json"
    temp_file = REVIEWS_DIR / f"{paper_id}.json.tmp"
    
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        temp_file.replace(review_file)
        
        # Update cache
        _reviews_cache[paper_id] = payload
        
        logger.info(f"Saved review for {paper_id}")
        return payload
    except Exception as e:
        logger.error(f"Error saving review for {paper_id}: {e}")
        if temp_file.exists():
            temp_file.unlink()
        raise


def delete_review(paper_id: str) -> bool:
    """Delete review file and remove from cache"""
    global _reviews_cache
    
    review_file = REVIEWS_DIR / f"{paper_id}.json"
    
    deleted = False
    if review_file.exists():
        try:
            review_file.unlink()
            deleted = True
        except Exception as e:
            logger.error(f"Error deleting review file {review_file}: {e}")
            raise
    
    if paper_id in _reviews_cache:
        del _reviews_cache[paper_id]
        deleted = True
        
    logger.info(f"Deleted review for {paper_id}")
    return deleted


def get_review_summary(paper_id: str) -> Dict[str, Any]:
    """Get review summary for paper list API"""
    load_reviews()
    
    if paper_id not in _reviews_cache:
        return {
            "reviewed": False,
            "review_updated_at": None,
            "s1_gold": None,
            "s1_agree": None,
            "s2_gold_exsitu": None,
            "s2_agree_exsitu": None,
            "review_comment_preview": None
        }
    
    review = _reviews_cache[paper_id]
    comment = review.get("comment", "")
    comment_preview = comment[:50] + "..." if len(comment) > 50 else comment
    
    return {
        "reviewed": True,
        "review_updated_at": review.get("updated_at"),
        "s1_gold": review.get("stage1", {}).get("gold"),
        "s1_agree": review.get("stage1", {}).get("agree"),
        "s2_gold_exsitu": review.get("stage2", {}).get("gold_exsitu"),
        "s2_agree_exsitu": review.get("stage2", {}).get("agree_exsitu"),
        "review_comment_preview": comment_preview if comment else None
    }


def load_all_papers() -> Dict[str, Dict[str, Any]]:
    """Load all paper data from the pipeline output directory"""
    global _papers_cache
    
    if _papers_cache:
        logger.info("Using cached paper data.")
        return _papers_cache
    
    papers = {}
    
    logger.info(f"Scanning directory: {DATA_DIR}")
    
    try:
        all_items = list(DATA_DIR.iterdir())
        target_folders = [
            f for f in all_items 
            if f.is_dir() and f.name not in ['logs', 'summary']
        ]
    except FileNotFoundError:
        logger.error(f"Directory not found: {DATA_DIR}")
        return {}

    logger.info(f"Found {len(target_folders)} paper folders. Starting load...")
    
    metadata_map = load_metadata_cache()
    
    for paper_folder in tqdm(target_folders, desc="Loading Papers"):
        paper_id = paper_folder.name
        paper_data = {
            "paper_id": paper_id,
            "metadata": metadata_map.get(paper_id, {}),
            "doc_title": None,
            "doc_abstract": None,
            "stage1": None,
            "stage1_prompt": None,
            "stage1_output_raw": None,
            "stage2": None,
            "stage2_prompt": None,
            "stage2_output_raw": None,
            "stage2_evidence_snippets": None,
            "sections": [],
            "section_prompts": [],
            "has_error": False,
            "folder_structure": {}
        }
        
        # Load document metadata
        # Prioritize meta_ files if they exist
        meta_title = read_file_safe(paper_folder / "meta_title.txt")
        paper_data["doc_title"] = meta_title if meta_title else read_file_safe(paper_folder / "doc_title.txt")
        
        meta_abstract = read_file_safe(paper_folder / "meta_abstract.txt")
        paper_data["doc_abstract"] = meta_abstract if meta_abstract else read_file_safe(paper_folder / "doc_abstract.txt")
        
        # Load stage1 files
        stage1_path = paper_folder / "stage1_result.json"
        if stage1_path.exists():
            try:
                with open(stage1_path, 'r', encoding='utf-8') as f:
                    paper_data["stage1"] = json.load(f)
            except Exception as e:
                paper_data["has_error"] = True
                paper_data["error"] = f"Error loading stage1: {e}"
                logger.error(f"[{paper_id}] Stage 1 load error: {e}")
        
        paper_data["stage1_prompt"] = read_file_safe(paper_folder / "stage1_prompt.txt")
        paper_data["stage1_output_raw"] = read_file_safe(paper_folder / "stage1_output_raw.txt")
        
        # Load stage2 files
        stage2_path = paper_folder / "stage2_result.json"
        if stage2_path.exists():
            try:
                with open(stage2_path, 'r', encoding='utf-8') as f:
                    paper_data["stage2"] = json.load(f)
            except Exception as e:
                paper_data["has_error"] = True
                paper_data["error"] = f"Error loading stage2: {e}"
                logger.error(f"[{paper_id}] Stage 2 load error: {e}")
        
        paper_data["stage2_prompt"] = read_file_safe(paper_folder / "stage2_prompt.txt")
        paper_data["stage2_output_raw"] = read_file_safe(paper_folder / "stage2_output_raw.txt")
        paper_data["stage2_evidence_snippets"] = read_file_safe(paper_folder / "stage2_evidence_snippets.txt")
        
        # Load sections_classification.jsonl
        sections_path = paper_folder / "sections_classification.jsonl"
        if sections_path.exists():
            try:
                with open(sections_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                section = json.loads(line)
                                paper_data["sections"].append(section)
                            except json.JSONDecodeError:
                                continue
            except Exception as e:
                paper_data["has_error"] = True
                logger.error(f"[{paper_id}] Sections load error: {e}")
        
        # Load section prompts
        section_prompts_dir = paper_folder / "section_prompts"
        if section_prompts_dir.exists():
            prompt_files = {}
            for f in section_prompts_dir.iterdir():
                if f.is_file():
                    # Parse filename: main_0001_prompt.txt, main_0001_output_raw.txt, etc.
                    parts = f.stem.split('_')
                    if len(parts) >= 3:
                        section_key = f"{parts[0]}_{parts[1]}"  # e.g., "main_0001"
                        file_type = '_'.join(parts[2:])  # e.g., "prompt", "output_raw", "content", "heading"
                        
                        if section_key not in prompt_files:
                            prompt_files[section_key] = {"key": section_key}
                        
                        prompt_files[section_key][file_type] = f.name
            
            paper_data["section_prompts"] = sorted(prompt_files.values(), key=lambda x: x["key"])
        
        # Scan folder structure
        folder_structure = {}
        for subdir in ["cleaned", "extracted", "removed", "tei"]:
            subdir_path = paper_folder / subdir
            if subdir_path.exists():
                folder_structure[subdir] = [f.name for f in subdir_path.iterdir()]
        paper_data["folder_structure"] = folder_structure
        
        papers[paper_id] = paper_data
    
    _papers_cache = papers
    logger.info(f"Successfully loaded {len(papers)} papers.")
    return papers



# ============ Metadata Functions ============

_metadata_cache = {}
_metadata_loaded = False

def extract_metadata_from_xml(xml_path: Path) -> Dict[str, Any]:
    """Extract metadata from Elsevier XML"""
    if not xml_path.exists():
        return {}
    
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        # Define namespaces
        ns = {
            'ce': 'http://www.elsevier.com/xml/common/dtd',
            'dc': 'http://purl.org/dc/elements/1.1/',
            'dcterms': 'http://purl.org/dc/terms/',
            'prism': 'http://prismstandard.org/namespaces/basic/2.0/'
        }
        
        # Helper to find text safely
        def find_text(xpath):
            elem = root.find(xpath, ns)
            return elem.text if elem is not None else None
            
        def find_all_text(xpath):
            return [elem.text for elem in root.findall(xpath, ns) if elem.text]

        # Extract fields
        # prism:coverDate -> 2022-09-30
        cover_date = find_text('.//prism:coverDate')
        year = cover_date[:4] if cover_date and len(cover_date) >= 4 else None
        
        journal = find_text('.//prism:publicationName')
        title = find_text('.//dc:title')
        doi = find_text('.//prism:doi')
        subjects = find_all_text('.//dcterms:subject')
        
        return {
            "year": year,
            "journal": journal,
            "title": title,
            "doi": doi,
            "subjects": subjects
        }
    except Exception as e:
        return {}

def load_metadata_cache(force_reload: bool = False) -> Dict[str, Dict]:
    """Load metadata for all papers"""
    global _metadata_cache, _metadata_loaded
    
    if _metadata_loaded and not force_reload:
        return _metadata_cache

    if METADATA_CACHE_FILE.exists() and not force_reload:
        try:
            with open(METADATA_CACHE_FILE, 'r', encoding='utf-8') as f:
                _metadata_cache = json.load(f)
            _metadata_loaded = True
            logger.info(f"Loaded metadata cache: {len(_metadata_cache)} entries")
            return _metadata_cache
        except Exception as e:
            logger.error(f"Error loading metadata cache: {e}")
    
    # Build cache
    logger.info("Building metadata cache from XML files...")
    new_cache = {}
    
    # We only care about papers that exist in DATA_DIR
    try:
        paper_ids = [p.name for p in DATA_DIR.iterdir() if p.is_dir() and p.name not in ['logs', 'summary']]
    except FileNotFoundError:
        return {}
    
    # Check if META_XML_DIR exists
    if not META_XML_DIR.exists():
        logger.warning(f"Metadata directory not found: {META_XML_DIR}")
        return {}

    for pid in tqdm(paper_ids, desc="Parsing Metadata XMLs"):
        xml_path = META_XML_DIR / f"{pid}__META_ABS.xml"
        meta = extract_metadata_from_xml(xml_path)
        if meta:
            new_cache[pid] = meta
            
    # Save cache
    try:
        if not SUMMARY_DIR.exists():
            SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
            
        with open(METADATA_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(new_cache, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved metadata cache to {METADATA_CACHE_FILE}")
    except Exception as e:
        logger.error(f"Error saving metadata cache: {e}")
        
    _metadata_cache = new_cache
    _metadata_loaded = True
    return _metadata_cache


def calculate_statistics(papers: Dict[str, Dict[str, Any]], use_cache: bool = True) -> Dict[str, Any]:
    """Calculate statistics (optionally using cache for global stats)"""
    global _stats_cache
    
    if use_cache and _stats_cache:
        return _stats_cache
    
    logger.info("Calculating statistics...")
    
    total_papers = len(papers)
    stage1_yes = 0
    stage2_exsitu_yes = 0
    stage2_labscale_yes = 0
    total_sections = 0
    sections_yes = 0
    sections_no = 0
    section_headings = Counter()
    modification_focus_counter = Counter()
    
    # Metadata counters
    years_counter = Counter()
    journals_counter = Counter()
    subjects_counter = Counter()
    
    # Review statistics
    load_reviews()
    reviewed_count = 0
    s1_agree_count = 0
    s1_disagree_count = 0
    s1_total_reviews = 0
    s2_agree_count = 0
    s2_disagree_count = 0
    s2_total_reviews = 0
    
    for paper_id, paper in papers.items():
        if paper.get("stage1"):
            if paper["stage1"].get("is_aqueous_zmb") == "YES":
                stage1_yes += 1
        
        if paper.get("stage2"):
            if paper["stage2"].get("has_exsitu_protective_layer") == "YES":
                stage2_exsitu_yes += 1
            if paper["stage2"].get("has_lab_scale_experiments") == "YES":
                stage2_labscale_yes += 1
            if paper["stage2"].get("modification_focus"):
                modification_focus_counter[paper["stage2"]["modification_focus"]] += 1
        
        for section in paper.get("sections", []):
            total_sections += 1
            decision = section.get("decision", "")
            if decision == "YES":
                sections_yes += 1
            elif decision == "NO":
                sections_no += 1
            
            heading = section.get("heading", "")
            if heading:
                section_headings[heading] += 1
                
        # Metadata Stats
        meta = paper.get("metadata", {})
        if meta:
            if meta.get("year"):
                years_counter[meta["year"]] += 1
            if meta.get("journal"):
                journals_counter[meta["journal"]] += 1
            if meta.get("subjects"):
                for subj in meta["subjects"]:
                    subjects_counter[subj] += 1
        
        # Review stats
        if paper_id in _reviews_cache:
            reviewed_count += 1
            review = _reviews_cache[paper_id]
            
            if review.get("stage1"):
                s1_agree = review["stage1"].get("agree")
                if s1_agree is not None:
                    s1_total_reviews += 1
                    if s1_agree:
                        s1_agree_count += 1
                    else:
                        s1_disagree_count += 1
            
            if review.get("stage2"):
                s2_agree = review["stage2"].get("agree_exsitu")
                if s2_agree is not None:
                    s2_total_reviews += 1
                    if s2_agree:
                        s2_agree_count += 1
                    else:
                        s2_disagree_count += 1
    
    stats = {
        "total_papers": total_papers,
        "stage1_yes_count": stage1_yes,
        "stage1_yes_percent": (stage1_yes / total_papers * 100) if total_papers > 0 else 0,
        "stage2_exsitu_yes": stage2_exsitu_yes,
        "stage2_exsitu_yes_percent": (stage2_exsitu_yes / total_papers * 100) if total_papers > 0 else 0,
        "stage2_labscale_yes": stage2_labscale_yes,
        "stage2_labscale_yes_percent": (stage2_labscale_yes / total_papers * 100) if total_papers > 0 else 0,
        "total_sections": total_sections,
        "sections_yes": sections_yes,
        "sections_no": sections_no,
        "sections_yes_percent": (sections_yes / total_sections * 100) if total_sections > 0 else 0,
        "top_section_headings": dict(section_headings.most_common(15)),
        "modification_focus_distribution": dict(modification_focus_counter),
        # Review statistics (enterprise)
        "reviewed_count": reviewed_count,
        "reviewed_percent": (reviewed_count / total_papers * 100) if total_papers > 0 else 0,
        "s1_agreement_rate": (s1_agree_count / s1_total_reviews * 100) if s1_total_reviews > 0 else None,
        "s1_agree_count": s1_agree_count,
        "s1_disagree_count": s1_disagree_count,
        "s2_agreement_rate": (s2_agree_count / s2_total_reviews * 100) if s2_total_reviews > 0 else None,
        "s2_agree_count": s2_agree_count,
        "s2_disagree_count": s2_disagree_count,
        # Metadata statistics
        "years_distribution": dict(sorted(years_counter.items())),
        "journals_distribution": dict(journals_counter.most_common(20)),
        "subjects_distribution": dict(subjects_counter.most_common(20)),
        # Word Cloud Data
        "word_cloud_data": _generate_word_cloud_data(papers)
    }
    
    if use_cache:
        _stats_cache = stats
        
    logger.info("Statistics calculation complete.")
    return stats


def _generate_word_cloud_data(papers: Dict[str, Dict[str, Any]], max_words: int = 100) -> List[Dict[str, Any]]:
    """Generate word frequency data for word cloud from titles and subjects"""
    text_corpus = []
    
    # Collect text from titles and subjects
    for paper in papers.values():
        # Title
        if paper.get("doc_title"):
            text_corpus.append(paper["doc_title"])
        
        # Subjects
        meta = paper.get("metadata", {})
        if meta and meta.get("subjects"):
            text_corpus.extend(meta["subjects"])
            
    if not text_corpus:
        return []
        
    # Simple stop words list
    STOP_WORDS = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
        'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did',
        'can', 'could', 'will', 'would', 'shall', 'should', 'may', 'might', 'must',
        'this', 'that', 'these', 'those', 'it', 'its', 'they', 'them', 'their', 'we', 'our', 'us',
        'from', 'up', 'down', 'out', 'into', 'over', 'under', 'again', 'further', 'then', 'once',
        'here', 'there', 'when', 'where', 'why', 'how', 'all', 'any', 'both', 'each', 'few', 'more',
        'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than',
        'too', 'very', 's', 't', 'can', 'will', 'just', 'don', 'should', 'now', 'use', 'using', 'used',
        'study', 'analysis', 'based', 'via', 'via', 'during', 'through', 'between', 'among'
    }
    
    # Tokenize and count
    words = []
    for text in text_corpus:
        # Normalize: lowercase, replace non-alphanumeric with space
        clean_text = re.sub(r'[^a-zA-Z0-9\s]', ' ', text.lower())
        tokens = clean_text.split()
        
        for token in tokens:
            if len(token) > 2 and token not in STOP_WORDS and not token.isdigit():
                words.append(token)
                
    counter = Counter(words)
    
    # Format for frontend: [{"text": "word", "value": 10}, ...]
    return [{"text": word, "value": count} for word, count in counter.most_common(max_words)]


@app.get("/")
async def read_root(request: Request):
    """Main viewer page"""
    logger.info(f"Accessing root page. Client: {request.client.host}")
    papers = load_all_papers()
    stats = calculate_statistics(papers)
    
    paper_list = []
    for paper_id, paper in papers.items():
        stage1_decision = paper.get("stage1", {}).get("is_aqueous_zmb", "N/A") if paper.get("stage1") else "N/A"
        stage2_exsitu = paper.get("stage2", {}).get("has_exsitu_protective_layer", "N/A") if paper.get("stage2") else "N/A"
        stage2_labscale = paper.get("stage2", {}).get("has_lab_scale_experiments", "N/A") if paper.get("stage2") else "N/A"
        modification_focus = paper.get("stage2", {}).get("modification_focus", "N/A") if paper.get("stage2") else "N/A"
        
        sections = paper.get("sections", [])
        yes_sections = sum(1 for s in sections if s.get("decision") == "YES")
        total_sections = len(sections)
        
        paper_list.append({
            "paper_id": paper_id,
            "doc_title": paper.get("doc_title", "").strip()[:100] if paper.get("doc_title") else "",
            "stage1_decision": stage1_decision,
            "stage2_exsitu": stage2_exsitu,
            "stage2_labscale": stage2_labscale,
            "modification_focus": modification_focus,
            "total_sections": total_sections,
            "yes_sections": yes_sections,
            "has_error": paper.get("has_error", False)
        })
    
    paper_list.sort(key=lambda x: x["paper_id"])
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "paper_list": paper_list,
        "stats": stats,
        "total_papers": len(papers)
    })


def filter_papers(
    papers: Dict[str, Any],
    search: str = None,
    viewset: str = None,
    idlist: str = None
) -> Dict[str, Any]:
    """Filter papers based on search query and ID list/viewset"""
    filtered_papers = {}
    
    # Determine ID filter
    id_filter = None
    if viewset:
        viewsets_data = load_viewsets()
        if viewset in viewsets_data.get("viewsets", {}):
            id_filter = set(viewsets_data["viewsets"][viewset].get("ids", []))
    elif idlist:
        id_filter = set(pid.strip() for pid in idlist.split(",") if pid.strip())
    
    for paper_id, paper in papers.items():
        # Apply ID filter (viewset or idlist)
        if id_filter is not None and paper_id not in id_filter:
            continue
        
        # Search in paper_id and doc_title
        if search:
            search_lower = search.lower()
            title = paper.get("doc_title", "") or ""
            if search_lower not in paper_id.lower() and search_lower not in title.lower():
                continue
        
        filtered_papers[paper_id] = paper
        
    return filtered_papers


@app.get("/api/papers")
async def get_papers(
    search: str = Query(None),
    viewset: str = Query(None),
    idlist: str = Query(None)
):
    """API endpoint to get paper list with search and viewset/idlist filtering"""
    all_papers = load_all_papers()
    filtered_papers = filter_papers(all_papers, search, viewset, idlist)
    
    paper_list = []
    for paper_id, paper in filtered_papers.items():
        stage1 = paper.get("stage1", {}) or {}
        stage2 = paper.get("stage2", {}) or {}
        
        sections = paper.get("sections", [])
        yes_sections = sum(1 for s in sections if s.get("decision") == "YES")
        
        # Get review summary
        review_summary = get_review_summary(paper_id)
        
        paper_list.append({
            "paper_id": paper_id,
            "doc_title": (paper.get("doc_title", "") or "").strip()[:100],
            "stage1_decision": stage1.get("is_aqueous_zmb", "N/A"),
            "stage1_confidence": stage1.get("confidence", 0),
            "stage2_exsitu": stage2.get("has_exsitu_protective_layer", "N/A"),
            "stage2_labscale": stage2.get("has_lab_scale_experiments", "N/A"),
            "modification_focus": stage2.get("modification_focus", "N/A"),
            "total_sections": len(sections),
            "yes_sections": yes_sections,
            "has_error": paper.get("has_error", False),
            # Review summary fields (enterprise feature)
            "reviewed": review_summary["reviewed"],
            "review_updated_at": review_summary["review_updated_at"],
            "s1_gold": review_summary["s1_gold"],
            "s1_agree": review_summary["s1_agree"],
            "s2_gold_exsitu": review_summary["s2_gold_exsitu"],
            "s2_agree_exsitu": review_summary["s2_agree_exsitu"],
            "review_comment_preview": review_summary["review_comment_preview"]
        })
    
    paper_list.sort(key=lambda x: x["paper_id"])
    return JSONResponse({"papers": paper_list, "count": len(paper_list)})
    #     yes_sections = sum(1 for s in sections if s.get("decision") == "YES")
        
    #     paper_list.append({
    #         "paper_id": paper_id,
    #         "doc_title": (paper.get("doc_title", "") or "").strip()[:100],
    #         "stage1_decision": stage1.get("is_aqueous_zmb", "N/A"),
    #         "stage1_confidence": stage1.get("confidence", 0),
    #         "stage2_exsitu": stage2.get("has_exsitu_protective_layer", "N/A"),
    #         "stage2_labscale": stage2.get("has_lab_scale_experiments", "N/A"),
    #         "modification_focus": stage2.get("modification_focus", "N/A"),
    #         "total_sections": len(sections),
    #         "yes_sections": yes_sections,
    #         "has_error": paper.get("has_error", False)
    #     })
    
    # paper_list.sort(key=lambda x: x["paper_id"])
    # return JSONResponse({"papers": paper_list, "count": len(paper_list)})


@app.get("/api/paper/{paper_id}")
async def get_paper(paper_id: str):
    """Get detailed paper data including all prompts and outputs"""
    papers = load_all_papers()
    
    if paper_id not in papers:
        logger.warning(f"Paper not found: {paper_id}")
        return JSONResponse({"error": "Paper not found"}, status_code=404)
    
    paper = papers[paper_id]
    
    # Get PDF info for this paper
    pdfs = get_pdfs_for_paper(paper_id)
    pdf_main = select_pdf(paper_id) if pdfs else None
    
    # Get review if exists
    load_reviews()
    review = _reviews_cache.get(paper_id)
    
    return JSONResponse({
        "paper_id": paper_id,
        "doc_title": paper.get("doc_title"),
        "doc_abstract": paper.get("doc_abstract"),
        "stage1": paper.get("stage1"),
        "stage1_prompt": paper.get("stage1_prompt"),
        "stage1_output_raw": paper.get("stage1_output_raw"),
        "stage2": paper.get("stage2"),
        "stage2_prompt": paper.get("stage2_prompt"),
        "stage2_output_raw": paper.get("stage2_output_raw"),
        "stage2_evidence_snippets": paper.get("stage2_evidence_snippets"),
        "sections": paper.get("sections", []),
        "section_prompts": paper.get("section_prompts", []),
        "folder_structure": paper.get("folder_structure", {}),
        "has_error": paper.get("has_error", False),
        "error": paper.get("error", None),
        "pdfs": pdfs,
        "pdf_main": pdf_main,
        "review": review
    })


@app.get("/api/paper/{paper_id}/section_prompt/{section_key}/{file_type}")
async def get_section_prompt_file(paper_id: str, section_key: str, file_type: str):
    """Get a specific section prompt file content"""
    paper_folder = DATA_DIR / paper_id / "section_prompts"
    
    # Map file_type to actual file extension
    filename_map = {
        "prompt": f"{section_key}_prompt.txt",
        "output_raw": f"{section_key}_output_raw.txt",
        "content": f"{section_key}_content.txt",
        "heading": f"{section_key}_heading.txt"
    }
    
    if file_type not in filename_map:
        return JSONResponse({"error": "Invalid file type"}, status_code=400)
    
    file_path = paper_folder / filename_map[file_type]
    content = read_file_safe(file_path, max_chars=50000)
    
    if content is None:
        return JSONResponse({"error": "File not found"}, status_code=404)
    
    return JSONResponse({"content": content, "filename": filename_map[file_type]})


@app.get("/api/paper/{paper_id}/file/{folder}/{filename}")
async def get_paper_file(paper_id: str, folder: str, filename: str):
    """Get content of a specific file in paper's subfolder"""
    allowed_folders = ["cleaned", "extracted", "removed", "tei"]
    
    if folder not in allowed_folders:
        return JSONResponse({"error": "Invalid folder"}, status_code=400)
    
    file_path = DATA_DIR / paper_id / folder / filename
    content = read_file_safe(file_path, max_chars=100000)
    
    if content is None:
        return JSONResponse({"error": "File not found"}, status_code=404)
    
    return JSONResponse({"content": content, "filename": filename, "folder": folder})


@app.get("/api/stats")
async def get_stats(
    search: str = Query(None),
    viewset: str = Query(None),
    idlist: str = Query(None)
):
    """API endpoint for statistics (supports filtering)"""
    all_papers = load_all_papers()
    
    # Only calculate filtered stats if filters are present
    if search or viewset or idlist:
        filtered_papers = filter_papers(all_papers, search, viewset, idlist)
        # Use a temporary cache-bypass calculation for filtered stats
        # (calculate_statistics caches the global result, so we implement a direct calculation here 
        #  or allow calculate_statistics to accept explicit papers dict without caching if logical)
        # For simplicity, we calculate directly here by reusing calculate_statistics 
        # BUT we need to ensure calculate_statistics doesn't modify/return the global cache if subset is passed.
        # Let's check calculate_statistics implementation.
        # It uses global _stats_cache. We should modify calculate_statistics to support no-cache mode.
        
        # Modified approach: We will use calculate_statistics but bypass cache check if it's a subset
        # Ideally we refactor calculate_statistics, but for now let's just do:
        return JSONResponse(calculate_statistics(filtered_papers, use_cache=False))
        
    return JSONResponse(calculate_statistics(all_papers, use_cache=True))
# ---------------------------------------------------------------------
# Enterprise Stats Dashboard API Endpoints
# ---------------------------------------------------------------------

# Global in‑memory snapshot storage (demo only)
SNAPSHOTS: Dict[str, Dict[str, Any]] = {}

@app.get("/api/stats/overview")
async def get_overview_stats(
    search: str = Query(None),
    viewset: str = Query(None),
    idlist: str = Query(None)
):
    """Return high‑level KPI overview stats (subset of calculate_statistics)."""
    all_papers = load_all_papers()
    if search or viewset or idlist:
        filtered = filter_papers(all_papers, search, viewset, idlist)
        stats = calculate_statistics(filtered, use_cache=False)
    else:
        stats = calculate_statistics(all_papers, use_cache=True)
    overview_keys = [
        "total_papers",
        "stage1_yes_count",
        "stage1_yes_percent",
        "stage2_exsitu_yes",
        "stage2_exsitu_yes_percent",
        "stage2_labscale_yes",
        "stage2_labscale_yes_percent",
        "reviewed_count",
        "reviewed_percent",
    ]
    return JSONResponse({k: stats.get(k) for k in overview_keys})

@app.get("/api/stats/distribution")
async def get_distribution(
    field: str = Query(..., description="Field to distribute: year, journal, subject"),
    search: str = Query(None),
    viewset: str = Query(None),
    idlist: str = Query(None)
):
    """Return histogram data for a metadata field."""
    all_papers = load_all_papers()
    papers = filter_papers(all_papers, search, viewset, idlist) if (search or viewset or idlist) else all_papers
    dist: Dict[str, int] = {}
    for pid, data in papers.items():
        meta = data.get("metadata", {})
        if field == "year":
            val = meta.get("year")
        elif field == "journal":
            val = meta.get("journal")
        elif field == "subject":
            subjects = meta.get("subjects", [])
            if isinstance(subjects, list):
                for sub in subjects:
                    dist[sub] = dist.get(sub, 0) + 1
                continue
            else:
                val = subjects
        else:
            return JSONResponse({"error": "Unsupported field"}, status_code=400)
        if val is None:
            continue
        if isinstance(val, list):
            for v in val:
                dist[str(v)] = dist.get(str(v), 0) + 1
        else:
            dist[str(val)] = dist.get(str(val), 0) + 1
    sorted_dist = dict(sorted(dist.items(), key=lambda i: i[1], reverse=True))
    return JSONResponse({"field": field, "distribution": sorted_dist})

@app.get("/api/stats/confusion")
async def get_confusion(
    target: str = Query(..., description="Target metric: s1, s2_exsitu, s2_labscale"),
    search: str = Query(None),
    viewset: str = Query(None),
    idlist: str = Query(None)
):
    """Return confusion matrix and paper IDs for a target metric."""
    all_papers = load_all_papers()
    papers = filter_papers(all_papers, search, viewset, idlist) if (search or viewset or idlist) else all_papers
    matrix = {"TP": 0, "FP": 0, "TN": 0, "FN": 0}
    ids_by_cell: Dict[str, List[str]] = {k: [] for k in matrix}
    for pid, data in papers.items():
        review = data.get("review", {})
        gold = review.get("gold")
        pred = review.get("pred")
        if not gold or not pred:
            continue
        if target == "s1":
            gold_val, pred_val = gold.get("s1"), pred.get("s1")
        elif target == "s2_exsitu":
            gold_val, pred_val = gold.get("s2_exsitu"), pred.get("s2_exsitu")
        elif target == "s2_labscale":
            gold_val, pred_val = gold.get("s2_labscale"), pred.get("s2_labscale")
        else:
            return JSONResponse({"error": "Unsupported target"}, status_code=400)
        if gold_val == pred_val:
            if gold_val == "YES":
                matrix["TP"] += 1
                ids_by_cell["TP"].append(pid)
            else:
                matrix["TN"] += 1
                ids_by_cell["TN"].append(pid)
        else:
            if pred_val == "YES":
                matrix["FP"] += 1
                ids_by_cell["FP"].append(pid)
            else:
                matrix["FN"] += 1
                ids_by_cell["FN"].append(pid)
    return JSONResponse({"target": target, "matrix": matrix, "ids": ids_by_cell})

@app.get("/api/stats/calibration")
async def get_calibration(
    target: str = Query(..., description="Target metric: s1, s2_exsitu, s2_labscale"),
    bins: int = Query(10),
    search: str = Query(None),
    viewset: str = Query(None),
    idlist: str = Query(None)
):
    """Return reliability diagram data for a target metric."""
    all_papers = load_all_papers()
    papers = filter_papers(all_papers, search, viewset, idlist) if (search or viewset or idlist) else all_papers
    bucket_data: Dict[int, Dict[str, Any]] = {i: {"count": 0, "correct": 0} for i in range(bins)}
    for pid, data in papers.items():
        review = data.get("review", {})
        gold = review.get("gold")
        pred = review.get("pred")
        conf = review.get("confidence", {})
        if not gold or not pred:
            continue
        if target == "s1":
            gold_val, pred_val, conf_val = gold.get("s1"), pred.get("s1"), conf.get("s1")
        elif target == "s2_exsitu":
            gold_val, pred_val, conf_val = gold.get("s2_exsitu"), pred.get("s2_exsitu"), conf.get("s2_exsitu")
        elif target == "s2_labscale":
            gold_val, pred_val, conf_val = gold.get("s2_labscale"), pred.get("s2_labscale"), conf.get("s2_labscale")
        else:
            continue
        if conf_val is None:
            continue
        bucket = int(min(max(conf_val * bins, 0), bins - 1))
        bucket_data[bucket]["count"] += 1
        if gold_val == pred_val:
            bucket_data[bucket]["correct"] += 1
    result = []
    for i in range(bins):
        d = bucket_data[i]
        acc = (d["correct"] / d["count"]) if d["count"] > 0 else None
        result.append({"bucket": i, "count": d["count"], "accuracy": acc})
    return JSONResponse({"target": target, "bins": bins, "data": result})

@app.get("/api/stats/missingness")
async def get_missingness(
    search: str = Query(None),
    viewset: str = Query(None),
    idlist: str = Query(None)
):
    """Return field‑wise missingness percentages across papers."""
    all_papers = load_all_papers()
    papers = filter_papers(all_papers, search, viewset, idlist) if (search or viewset or idlist) else all_papers
    field_counts: Dict[str, int] = {}
    missing_counts: Dict[str, int] = {}
    total = len(papers)
    for pid, data in papers.items():
        meta = data.get("metadata", {})
        for field, value in meta.items():
            field_counts[field] = field_counts.get(field, 0) + 1
            if value in (None, "", [], {}):
                missing_counts[field] = missing_counts.get(field, 0) + 1
    missingness = {f: (missing_counts.get(f, 0) / total) * 100 for f in field_counts}
    return JSONResponse({"total_papers": total, "missingness_percent": missingness})

@app.get("/api/stats/sections")
async def get_section_stats(
    search: str = Query(None),
    viewset: str = Query(None),
    idlist: str = Query(None)
):
    """Return statistics about section YES/NO ratios."""
    all_papers = load_all_papers()
    papers = filter_papers(all_papers, search, viewset, idlist) if (search or viewset or idlist) else all_papers
    section_yes: Dict[str, int] = {}
    section_total: Dict[str, int] = {}
    for pid, data in papers.items():
        sections = data.get("sections", {})
        for sec_key, sec_info in sections.items():
            yes = sec_info.get("yes")
            section_total[sec_key] = section_total.get(sec_key, 0) + 1
            if yes:
                section_yes[sec_key] = section_yes.get(sec_key, 0) + 1
    ratios = {k: (section_yes.get(k, 0) / section_total[k]) * 100 for k in section_total}
    return JSONResponse({"section_yes_ratio_percent": ratios})

@app.post("/api/snapshots")
async def create_snapshot(filter_state: Dict[str, Any] = Body(...)):
    """Store a snapshot of current filter state and return a shareable ID."""
    snap_id = str(uuid.uuid4())
    SNAPSHOTS[snap_id] = filter_state
    return JSONResponse({"snapshot_id": snap_id})

@app.get("/api/snapshots/{snap_id}")
async def get_snapshot(snap_id: str):
    """Retrieve a previously stored snapshot."""
    state = SNAPSHOTS.get(snap_id)
    if state is None:
        return JSONResponse({"error": "Snapshot not found"}, status_code=404)
    return JSONResponse(state)


@app.get("/api/reload")
async def reload_data():
    """Force reload all data from disk"""
    logger.info("Manual reload requested.")
    global _papers_cache, _stats_cache, _pdf_index_cache, _pdf_index_built, _reviews_cache, _reviews_loaded
    _papers_cache = {}
    _stats_cache = {}
    _pdf_index_cache = {}
    _pdf_index_built = False
    _reviews_cache = {}
    _reviews_loaded = False
    papers = load_all_papers()
    stats = calculate_statistics(papers)
    build_pdf_index()  # Rebuild PDF index
    load_reviews()  # Rebuild reviews cache
    return JSONResponse({"message": "Data reloaded", "papers": len(papers), "stats": stats})


# ============ PDF Functions ============

def build_pdf_index() -> Dict[str, List[Dict[str, Any]]]:
    """Build PDF index by scanning PDF_DIR"""
    global _pdf_index_cache, _pdf_index_built
    
    if _pdf_index_built:
        return _pdf_index_cache
    
    logger.info(f"Building PDF index from: {PDF_DIR}")
    
    if not PDF_DIR.exists():
        logger.warning(f"PDF directory not found: {PDF_DIR}")
        _pdf_index_built = True
        return _pdf_index_cache
    
    # Regex to extract paper_id and kind from filename
    # Pattern: 1-s2.0-S0010938X25003439-main.pdf -> paper_id=S0010938X25003439, kind=main
    pdf_pattern = re.compile(r'-s2\.0-(?P<paper_id>[A-Za-z0-9]+)-(?P<tail>.+)\.pdf$', re.IGNORECASE)
    
    pdf_files = list(PDF_DIR.glob("*.pdf"))
    logger.info(f"Found {len(pdf_files)} PDF files")
    
    for pdf_file in pdf_files:
        match = pdf_pattern.search(pdf_file.name)
        if match:
            paper_id = match.group("paper_id")
            tail = match.group("tail")
            kind = "main" if tail.lower() == "main" else tail.lower()
        else:
            # Fallback: try to extract any ID-like pattern
            # Just use filename without extension
            paper_id = pdf_file.stem
            kind = "unknown"
        
        stat = pdf_file.stat()
        pdf_info = {
            "name": pdf_file.name,
            "kind": kind,
            "size": stat.st_size,
            "mtime": stat.st_mtime
        }
        
        if paper_id not in _pdf_index_cache:
            _pdf_index_cache[paper_id] = []
        _pdf_index_cache[paper_id].append(pdf_info)
    
    # Sort each paper's PDFs: main first, then supp, then alphabetically
    def sort_key(pdf):
        kind = pdf.get("kind", "")
        if kind == "main":
            return (0, kind)
        elif kind == "supp":
            return (1, kind)
        else:
            return (2, kind)
    
    for paper_id in _pdf_index_cache:
        _pdf_index_cache[paper_id].sort(key=sort_key)
    
    _pdf_index_built = True
    logger.info(f"PDF index built: {len(_pdf_index_cache)} papers with PDFs")
    return _pdf_index_cache


def get_pdfs_for_paper(paper_id: str) -> List[Dict]:
    """Get all PDFs for a paper"""
    build_pdf_index()
    return _pdf_index_cache.get(paper_id, [])


def select_pdf(paper_id: str, kind: Optional[str] = None, name: Optional[str] = None) -> Optional[Dict]:
    """Select a specific PDF for a paper"""
    pdfs = get_pdfs_for_paper(paper_id)
    if not pdfs:
        return None
    
    # If name is specified, find exact match (whitelist check)
    if name:
        for pdf in pdfs:
            if pdf["name"] == name:
                return pdf
        return None
    
    # If kind is specified, find first match
    if kind:
        for pdf in pdfs:
            if pdf["kind"] == kind:
                return pdf
        return None
    
    # Default: return first (main preferred due to sorting)
    return pdfs[0] if pdfs else None


# ============ Review API Endpoints ============

@app.get("/api/paper/{paper_id}/review")
async def get_review(paper_id: str):
    """Get review for a paper"""
    if not PAPER_ID_PATTERN.match(paper_id):
        return JSONResponse({"error": "Invalid paper ID"}, status_code=400)
    
    load_reviews()
    
    if paper_id not in _reviews_cache:
        return JSONResponse({"exists": False}, status_code=404)
    
    return JSONResponse(_reviews_cache[paper_id])


@app.put("/api/paper/{paper_id}/review")
async def put_review(paper_id: str, payload: Dict[str, Any] = Body(...)):
    """Save or update a review"""
    if not PAPER_ID_PATTERN.match(paper_id):
        return JSONResponse({"error": "Invalid paper ID"}, status_code=400)
    
    try:
        # Save review
        saved_review = save_review(paper_id, payload)
        
        # Return updated review summary for immediate UI update
        review_summary = get_review_summary(paper_id)
        
        return JSONResponse({
            "success": True,
            "review": saved_review,
            "summary": review_summary
        })
    except Exception as e:
        logger.error(f"Error saving review for {paper_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/paper/{paper_id}/review")
async def delete_paper_review(paper_id: str):
    """Delete a review for a paper"""
    if not PAPER_ID_PATTERN.match(paper_id):
        return JSONResponse({"error": "Invalid paper ID"}, status_code=400)
    
    try:
        delete_review(paper_id)
        
        # Return empty summary for immediate UI update
        review_summary = get_review_summary(paper_id)
        
        return JSONResponse({
            "success": True,
            "message": "Review deleted",
            "summary": review_summary
        })
    except Exception as e:
        logger.error(f"Error deleting review for {paper_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ============ PDF API Endpoints ============

@app.get("/api/paper/{paper_id}/pdf/list")
async def get_paper_pdf_list(paper_id: str):
    """List all PDFs for a paper"""
    if not PAPER_ID_PATTERN.match(paper_id):
        return JSONResponse({"error": "Invalid paper ID"}, status_code=400)
    
    pdfs = get_pdfs_for_paper(paper_id)
    return JSONResponse({
        "exists": len(pdfs) > 0,
        "pdfs": pdfs
    })


@app.get("/api/paper/{paper_id}/pdf/meta")
async def get_paper_pdf_meta(paper_id: str, kind: str = Query(None)):
    """Get PDF metadata (used for showing PDF status in UI)"""
    if not PAPER_ID_PATTERN.match(paper_id):
        return JSONResponse({"error": "Invalid paper ID"}, status_code=400)
    
    pdfs = get_pdfs_for_paper(paper_id)
    selected = select_pdf(paper_id, kind=kind)
    
    return JSONResponse({
        "exists": len(pdfs) > 0,
        "count": len(pdfs),
        "selected": selected
    })


@app.get("/api/paper/{paper_id}/pdf/view")
async def view_paper_pdf(paper_id: str, name: str = Query(None), kind: str = Query(None)):
    """View PDF inline in browser"""
    if not PAPER_ID_PATTERN.match(paper_id):
        return JSONResponse({"error": "Invalid paper ID"}, status_code=400)
    
    selected = select_pdf(paper_id, kind=kind, name=name)
    if not selected:
        return JSONResponse({"error": "PDF not found"}, status_code=404)
    
    pdf_path = PDF_DIR / selected["name"]
    if not pdf_path.exists():
        return JSONResponse({"error": "PDF file not found"}, status_code=404)
    
    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{selected["name"]}"'
        }
    )


@app.get("/api/paper/{paper_id}/pdf/download")
async def download_paper_pdf(paper_id: str, name: str = Query(None), kind: str = Query(None)):
    """Download PDF as attachment"""
    if not PAPER_ID_PATTERN.match(paper_id):
        return JSONResponse({"error": "Invalid paper ID"}, status_code=400)
    
    selected = select_pdf(paper_id, kind=kind, name=name)
    if not selected:
        return JSONResponse({"error": "PDF not found"}, status_code=404)
    
    pdf_path = PDF_DIR / selected["name"]
    if not pdf_path.exists():
        return JSONResponse({"error": "PDF file not found"}, status_code=404)
    
    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=selected["name"],
        headers={
            "Content-Disposition": f'attachment; filename="{selected["name"]}"'
        }
    )


# ============ View Set API Endpoints ============

@app.get("/api/viewsets")
async def list_viewsets():
    """List all saved viewsets"""
    data = load_viewsets()
    viewsets = data.get("viewsets", {})
    
    result = []
    for name, vs in viewsets.items():
        result.append({
            "name": name,
            "count": len(vs.get("ids", [])),
            "note": vs.get("note"),
            "updated_at": vs.get("updated_at")
        })
    
    result.sort(key=lambda x: x["name"])
    return JSONResponse({"viewsets": result, "count": len(result)})


@app.get("/api/viewsets/{name}")
async def get_viewset(name: str):
    """Get a specific viewset by name"""
    data = load_viewsets()
    viewsets = data.get("viewsets", {})
    
    if name not in viewsets:
        return JSONResponse({"error": "Viewset not found"}, status_code=404)
    
    return JSONResponse(viewsets[name])


@app.post("/api/viewsets")
async def create_viewset(viewset: ViewSetCreate):
    """Create or update a viewset"""
    data = load_viewsets()
    
    # Validate name
    if not viewset.name or not viewset.name.strip():
        return JSONResponse({"error": "Name is required"}, status_code=400)
    
    name = viewset.name.strip()
    
    # Validate and filter IDs
    valid_ids, invalid_ids, missing_ids = validate_paper_ids(viewset.ids)
    
    now = datetime.now(timezone.utc).isoformat()
    
    is_update = name in data.get("viewsets", {})
    
    data.setdefault("viewsets", {})[name] = {
        "name": name,
        "ids": valid_ids,
        "note": viewset.note,
        "created_at": data.get("viewsets", {}).get(name, {}).get("created_at", now),
        "updated_at": now
    }
    
    if save_viewsets(data):
        return JSONResponse({
            "ok": True,
            "name": name,
            "count": len(valid_ids),
            "invalid": invalid_ids,
            "missing": missing_ids,
            "updated": is_update
        })
    else:
        return JSONResponse({"error": "Failed to save viewset"}, status_code=500)


@app.put("/api/viewsets/{name}")
async def update_viewset(name: str, update: ViewSetUpdate):
    """Update an existing viewset"""
    data = load_viewsets()
    
    if name not in data.get("viewsets", {}):
        return JSONResponse({"error": "Viewset not found"}, status_code=404)
    
    vs = data["viewsets"][name]
    
    if update.ids is not None:
        valid_ids, _, _ = validate_paper_ids(update.ids)
        vs["ids"] = valid_ids
    
    if update.note is not None:
        vs["note"] = update.note
    
    vs["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    if save_viewsets(data):
        return JSONResponse({"ok": True, "count": len(vs.get("ids", []))})
    else:
        return JSONResponse({"error": "Failed to save viewset"}, status_code=500)


@app.delete("/api/viewsets/{name}")
async def delete_viewset(name: str):
    """Delete a viewset"""
    data = load_viewsets()
    
    if name not in data.get("viewsets", {}):
        return JSONResponse({"error": "Viewset not found"}, status_code=404)
    
    del data["viewsets"][name]
    
    if save_viewsets(data):
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"error": "Failed to save viewset"}, status_code=500)


@app.post("/api/parse_id_file")
async def parse_id_file(file: UploadFile = File(...)):
    """Parse an uploaded text file containing paper IDs (one per line)"""
    try:
        content = await file.read()
        text = content.decode('utf-8')
        
        # Split by newlines and clean up
        lines = [line.strip() for line in text.splitlines()]
        
        # Limit to 20000 lines
        if len(lines) > 20000:
            lines = lines[:20000]
        
        valid_ids, invalid_ids, missing_ids = validate_paper_ids(lines)
        
        return JSONResponse({
            "ids": valid_ids,
            "invalid": invalid_ids,
            "missing": missing_ids,
            "total_lines": len(lines)
        })
    except Exception as e:
        logger.error(f"Failed to parse ID file: {e}")
        return JSONResponse({"error": f"Failed to parse file: {str(e)}"}, status_code=400)


@app.get("/api/stats/drilldown")
async def get_drilldown_ids(
    field: str = Query(..., description="Field to filter by"),
    value: str = Query(..., description="Value to match"),
    search: str = Query(None),
    viewset: str = Query(None),
    idlist: str = Query(None)
):
    """Return IDs of papers matching specific field/value criteria (Drill-down)"""
    all_papers = load_all_papers()
    filtered = filter_papers(all_papers, search, viewset, idlist)
    
    matching_ids = []
    target_value = value.strip()
    
    for pid, paper in filtered.items():
        match = False
        meta = paper.get("metadata", {})
        s1 = paper.get("stage1", {}) or {}
        s2 = paper.get("stage2", {}) or {}
        
        if field == "year":
            val = meta.get("year")
            if val is not None and str(val) == target_value: match = True
        elif field == "journal":
            if meta.get("journal") == target_value: match = True
        elif field == "subject":
            if target_value in (meta.get("subjects") or []): match = True
        elif field == "stage1_decision":
             # Handle S1 Interest bar chart if exists
             decision = s1.get("is_aqueous_zmb", "N/A")
             if decision == target_value: match = True
        elif field == "modification_focus":
             if s2.get("modification_focus") == target_value: match = True
             
        if match:
            matching_ids.append(pid)
            
    return JSONResponse({"ids": matching_ids, "count": len(matching_ids)})

@app.post("/api/snapshots")
async def create_snapshot(payload: Dict[str, Any] = Body(...)):
    """Create a snapshot of the current view (save as a temporary viewset)"""
    snapshot_id = str(uuid.uuid4())
    viewsets = load_viewsets()
    
    # Payload expected: { ids: [...], name: "Snapshot...", note: "..." }
    viewsets["viewsets"][snapshot_id] = {
        "name": payload.get("name", f"Snapshot {snapshot_id[:8]}"),
        "ids": payload.get("ids", []),
        "note": payload.get("note", "Created via Stats Dashboard"),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    if save_viewsets(viewsets):
        return JSONResponse({"snapshot_id": snapshot_id})
    return JSONResponse({"error": "Failed to save snapshot"}, status_code=500)


# ============ Gemini API Verification ============
GEMINI_REVIEWS_DIR = SUMMARY_DIR / "gemini_reviews"

# Available Gemini models
GEMINI_MODELS = [
    {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash (Fast)"},
    {"id": "gemini-2.5-pro-preview-05-06", "name": "Gemini 2.5 Pro Preview"},
    {"id": "gemini-exp-1206", "name": "Gemini 3.0 Pro Preview (Experimental)"},
    {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro (Stable)"},
    {"id": "gemini-1.5-flash", "name": "Gemini 1.5 Flash"},
]


class GeminiVerifyRequest(BaseModel):
    paper_id: str
    stage: str  # "stage1" or "stage2"
    model: str = "gemini-2.0-flash"
    include_local_response: bool = False


@app.get("/api/gemini/models")
async def get_gemini_models():
    """Get available Gemini models"""
    return JSONResponse({"models": GEMINI_MODELS, "api_key_set": bool(GEMINI_API_KEY)})


@app.post("/api/gemini/verify")
async def gemini_verify(req: GeminiVerifyRequest):
    """Send prompt to Gemini for verification"""
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
    
    # Load paper data
    papers = load_all_papers()
    if req.paper_id not in papers:
        raise HTTPException(status_code=404, detail="Paper not found")
    
    paper = papers[req.paper_id]
    
    # Get prompt based on stage (file names: stage1_prompt.txt, stage2_prompt.txt)
    if req.stage == "stage1":
        prompt_file = "stage1_prompt.txt"
        local_response = paper.get("stage1", {})
    elif req.stage == "stage2":
        prompt_file = "stage2_prompt.txt"
        local_response = paper.get("stage2", {})
    else:
        raise HTTPException(status_code=400, detail="Invalid stage")
    
    # Read prompt from file
    paper_folder = DATA_DIR / req.paper_id
    prompt_path = paper_folder / prompt_file
    
    if not prompt_path.exists():
        raise HTTPException(status_code=404, detail=f"Prompt file not found: {prompt_file}")
    
    original_prompt = prompt_path.read_text(encoding="utf-8")
    
    # Two modes of operation:
    # 1. Without local response: Send original prompt directly to Gemini
    # 2. With local response: Ask Gemini to evaluate the local LLM's answer
    
    if req.include_local_response and local_response:
        # Mode 2: Evaluation mode
        gemini_prompt = f"""다음은 연구 논문 분류를 위해 로컬 LLM에게 전달한 프롬프트와 그 응답입니다.

# 원본 분류 프롬프트
---
{original_prompt}
---

# 로컬 LLM의 답변
```json
{json.dumps(local_response, ensure_ascii=False, indent=2)}
```

# 검수 요청
위 프롬프트의 지시사항에 따라 로컬 LLM이 제공한 답변을 평가해주세요:

1. **답변의 정확성**: 프롬프트 지시사항을 올바르게 따랐는가?
2. **판단의 타당성**: 분류 결과(YES/NO 등)가 합리적인가?
3. **누락 또는 오류**: 명백한 실수나 누락된 정보가 있는가?
4. **개선 제안**: 더 나은 답변이 있다면 무엇인가?

**한국어로 상세하게 평가해주세요.**
"""
    else:
        # Mode 1: Direct classification mode - send original prompt as-is
        gemini_prompt = original_prompt + "\n\n**한국어로 답변해주세요.**"
    
    # Call Gemini API
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{req.model}:generateContent"
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                api_url,
                params={"key": GEMINI_API_KEY},
                json={
                    "contents": [{"parts": [{"text": gemini_prompt}]}],
                    "generationConfig": {
                        "temperature": 0.1,
                        "maxOutputTokens": 4096
                    }
                }
            )
            
            if response.status_code != 200:
                logger.error(f"Gemini API error: {response.status_code} - {response.text}")
                raise HTTPException(status_code=response.status_code, detail=f"Gemini API error: {response.text[:500]}")
            
            result = response.json()
            
            # Extract text from response
            gemini_text = ""
            if "candidates" in result and result["candidates"]:
                parts = result["candidates"][0].get("content", {}).get("parts", [])
                gemini_text = "".join(p.get("text", "") for p in parts)
            
            # Save review
            review_data = {
                "paper_id": req.paper_id,
                "stage": req.stage,
                "model": req.model,
                "include_local_response": req.include_local_response,
                "prompt_length": len(gemini_prompt),
                "gemini_response": gemini_text,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "usage": result.get("usageMetadata", {})
            }
            
            # Save to file
            paper_reviews_dir = GEMINI_REVIEWS_DIR / req.paper_id
            paper_reviews_dir.mkdir(parents=True, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            review_file = paper_reviews_dir / f"{timestamp}_{req.stage}_{req.model.replace('-', '_')}.json"
            
            with open(review_file, 'w', encoding='utf-8') as f:
                json.dump(review_data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"Gemini verification saved: {review_file.name}")
            
            return JSONResponse({
                "success": True,
                "gemini_response": gemini_text,
                "model": req.model,
                "stage": req.stage,
                "saved_to": str(review_file.name),
                "created_at": review_data["created_at"]
            })
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Gemini API timeout")
    except Exception as e:
        logger.error(f"Gemini verification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/paper/{paper_id}/gemini-reviews")
async def get_gemini_reviews(paper_id: str):
    """Get all Gemini reviews for a paper"""
    if not PAPER_ID_PATTERN.match(paper_id):
        raise HTTPException(status_code=400, detail="Invalid paper ID")
    
    paper_reviews_dir = GEMINI_REVIEWS_DIR / paper_id
    
    if not paper_reviews_dir.exists():
        return JSONResponse({"reviews": []})
    
    reviews = []
    for f in sorted(paper_reviews_dir.glob("*.json"), reverse=True):
        try:
            with open(f, 'r', encoding='utf-8') as file:
                review = json.load(file)
                review["filename"] = f.name
                reviews.append(review)
        except Exception as e:
            logger.error(f"Error loading review {f}: {e}")
    
    return JSONResponse({"reviews": reviews})


if __name__ == "__main__":
    logger.info("Starting Uvicorn Server with Proxy Headers support...")
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8502,
        reload=True,
        lifespan="off",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
