# scripts/main_pipeline.py
"""
AZIB Ex-situ Protective Layer Extraction Pipeline - Main Entry Point

This is the MASTER ORCHESTRATOR that:
1. Reads a list of paper IDs (PIIs) from a text file
2. Locates PDFs and supplementary files
3. Runs GROBID to generate TEI XML
4. Runs the full extraction pipeline

Usage:
    python scripts/main_pipeline.py \
        --pii-list papers.txt \
        --pdf-dir pdfs \
        --supp-dir supplementary_files \
        --run-dir runs/run_001

papers.txt format (one PII per line):
    S0378775323006754
    S0378775323012345
    ...
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Setup path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.io_jsonl import read_json, write_json, read_jsonl, write_jsonl

# ============================================================================
# Logging Setup
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================
class Config:
    """Pipeline configuration."""
    # GROBID settings
    GROBID_URL = os.environ.get("GROBID_URL", "http://localhost:8080")
    GROBID_TIMEOUT = 300  # seconds per file
    
    # File patterns
    MAIN_PDF_PATTERNS = [
        "{pii}.pdf",
        "{pii}_main.pdf",
    ]
    SUPP_PDF_PATTERNS = [
        "{pii}_supp.pdf",
        "{pii}-supp.pdf",
        "{pii}_supplementary.pdf",
        "{pii}_SI.pdf",
        "{pii}_mmc1.pdf",
    ]
    
    # LLM Models
    MODEL_SMALL = os.environ.get("MODEL_SMALL", "gpt-4o-mini")
    MODEL_MID = os.environ.get("MODEL_MID", "gpt-4o")
    MODEL_LARGE = os.environ.get("MODEL_LARGE", "gpt-4o")


# ============================================================================
# PII List Loader
# ============================================================================
def load_pii_list(pii_file: Path) -> List[str]:
    """
    Load paper IDs from a text file.
    
    Format: One PII per line, lines starting with # are ignored
    Example:
        S0378775323006754
        S0378775323012345
        # This is a comment
    """
    piis = []
    with open(pii_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                piis.append(line)
    logger.info(f"Loaded {len(piis)} PIIs from {pii_file}")
    return piis


# ============================================================================
# File Locator
# ============================================================================
def find_pdf_file(pii: str, pdf_dir: Path, patterns: List[str]) -> Optional[Path]:
    """Find a PDF file matching the PII using various patterns."""
    for pattern in patterns:
        filename = pattern.format(pii=pii)
        path = pdf_dir / filename
        if path.exists():
            return path
    
    # Fallback: case-insensitive search
    pii_lower = pii.lower()
    for f in pdf_dir.iterdir():
        if f.is_file() and f.suffix.lower() == '.pdf':
            if pii_lower in f.name.lower():
                return f
    
    return None


def find_supplementary_files(pii: str, supp_dir: Path) -> List[Path]:
    """
    Find all supplementary files for a PII.
    
    Supports two structures:
    1. Flat: supp_dir/PII_file.pdf
    2. Folder: supp_dir/PII/file.pdf (or supp_dir/PII/*.docx)
    """
    files = []
    pii_lower = pii.lower()
    
    if not supp_dir.exists():
        return files
    
    # Strategy 1: Check for a directory named exactly as the PII (or containing it)
    # This is preferred for the enterprise dataset structure
    candidate_dirs = []
    for d in supp_dir.iterdir():
        if d.is_dir() and pii_lower in d.name.lower():
            candidate_dirs.append(d)
            
    for d in candidate_dirs:
        # Add all files in the PII specific directory
        # Recursive glob to catch files in nested subfolders if any
        for f in d.rglob("*"):
            if f.is_file() and not f.name.startswith("~") and f.name != "Thumbs.db":
                files.append(f)
                
    # Strategy 2: Flat file search (Legacy/Fallback)
    # Only add if not already found via directory to avoid duplicates if structures are mixed
    found_paths = {f.absolute() for f in files}
    
    for f in supp_dir.iterdir():
        if f.is_file() and pii_lower in f.name.lower():
            if f.absolute() not in found_paths:
                files.append(f)
    
    return files


def locate_paper_files(
    pii: str,
    pdf_dir: Path,
    supp_dir: Path
) -> Dict[str, Any]:
    """Locate all files for a paper."""
    main_pdf = find_pdf_file(pii, pdf_dir, Config.MAIN_PDF_PATTERNS)
    supp_files = find_supplementary_files(pii, supp_dir)
    
    # Separate supplementary PDFs from other files
    supp_pdfs = [f for f in supp_files if f.suffix.lower() == '.pdf']
    supp_other = [f for f in supp_files if f.suffix.lower() != '.pdf']
    
    # Convert Path objects to strings for JSON serialization
    return {
        "pii": pii,
        "main_pdf": str(main_pdf) if main_pdf else None,
        "supp_pdfs": [str(f) for f in supp_pdfs],
        "supp_other": [str(f) for f in supp_other],
        "found_main": main_pdf is not None,
        "found_supp": len(supp_pdfs) > 0,
        # Keep original Path objects for internal use
        "_main_pdf_path": main_pdf,
        "_supp_pdfs_paths": supp_pdfs,
    }


# ============================================================================
# GROBID Integration
# ============================================================================
def run_grobid_on_pdf(pdf_path: Path, output_dir: Path, doc_type: str = "main") -> Optional[Path]:
    """
    Run GROBID on a PDF to generate TEI XML.
    
    Args:
        pdf_path: Path to PDF file
        output_dir: Directory to save TEI XML
        doc_type: "main" or "supp"
    
    Returns:
        Path to generated TEI XML, or None if failed
    """
    try:
        import requests
    except ImportError:
        logger.error("requests library not available for GROBID. Install with: pip install requests")
        return None
    
    output_dir.mkdir(parents=True, exist_ok=True)
    tei_filename = f"{doc_type}.tei.xml"
    tei_path = output_dir / tei_filename
    
    # Check if already processed
    if tei_path.exists():
        logger.info(f"    TEI already exists: {tei_path.name}")
        return tei_path
    
    # Call GROBID API
    url = f"{Config.GROBID_URL}/api/processFulltextDocument"
    
    try:
        with open(pdf_path, 'rb') as f:
            files = {'input': (pdf_path.name, f, 'application/pdf')}
            data = {
                'consolidateHeader': '1',
                'consolidateCitations': '0',
                'includeRawAffiliations': '1',
                'includeRawCitations': '0',
                'teiCoordinates': 'ref,figure,formula',
            }
            
            logger.info(f"    Calling GROBID for {pdf_path.name}...")
            response = requests.post(
                url,
                files=files,
                data=data,
                timeout=Config.GROBID_TIMEOUT
            )
            
            if response.status_code == 200:
                with open(tei_path, 'w', encoding='utf-8') as out:
                    out.write(response.text)
                logger.info(f"    Generated: {tei_path.name}")
                return tei_path
            else:
                logger.error(f"    GROBID error {response.status_code}: {response.text[:200]}")
                return None
                
    except requests.exceptions.ConnectionError:
        logger.error(f"    GROBID connection failed. Is GROBID running at {Config.GROBID_URL}?")
        return None
    except requests.exceptions.Timeout:
        logger.error(f"    GROBID timeout for {pdf_path.name}")
        return None
    except Exception as e:
        logger.error(f"    GROBID error: {e}")
        return None


def convert_to_pdf(input_path: Path, output_dir: Path) -> Optional[Path]:
    """Convert a file to PDF using Pandoc."""
    try:
        output_path = output_dir / f"{input_path.stem}.pdf"
        if output_path.exists():
            return output_path
            
        # Simplified command: pandoc input.docx -o output.pdf
        cmd = ["pandoc", str(input_path), "-o", str(output_path)]
        
        logger.info(f"    Converting {input_path.name} to PDF...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return output_path
        else:
            logger.error(f"    Pandoc failed: {result.stderr[:200]}")
            return None
    except Exception as e:
        logger.error(f"    Conversion error: {e}")
        return None


# ============================================================================
# Paper Setup
# ============================================================================
def setup_paper_directory(
    pii: str,
    file_info: Dict[str, Any],
    data_root: Path
) -> Dict[str, Any]:
    """
    Set up the paper directory structure and run GROBID.
    
    Creates:
        data/papers/{pii}/
            meta.json
            main.pdf (copy or symlink)
            supp.pdf (copy or symlink)
            main.tei.xml (from GROBID)
            supp.tei.xml (from GROBID)
            derived/
    """
    paper_dir = data_root / "papers" / pii
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "derived").mkdir(exist_ok=True)
    
    result = {
        "pii": pii,
        "paper_dir": str(paper_dir),
        "main_tei": None,
        "supp_tei": None,
        "success": False,
        "errors": []
    }
    
    # Copy/link main PDF (use internal Path object)
    main_pdf_path = file_info.get("_main_pdf_path")
    if main_pdf_path:
        main_dest = paper_dir / "main.pdf"
        if not main_dest.exists():
            shutil.copy2(main_pdf_path, main_dest)
        
        # Run GROBID
        tei = run_grobid_on_pdf(main_dest, paper_dir, "main")
        if tei:
            result["main_tei"] = str(tei)
        else:
            result["errors"].append("GROBID failed for main PDF")
    else:
        result["errors"].append("Main PDF not found")
    
    # Copy/link supplementary PDF (use first one from internal Path list)
    supp_pdf_paths = file_info.get("_supp_pdfs_paths", [])
    supp_teis = []
    
    if supp_pdf_paths:
        supp_src = supp_pdf_paths[0]
        supp_dest = paper_dir / "supp.pdf"
        if not supp_dest.exists():
            shutil.copy2(supp_src, supp_dest)
        
        # Run GROBID
        tei = run_grobid_on_pdf(supp_dest, paper_dir, "supp")
        if tei:
            result["supp_tei"] = str(tei)
            supp_teis.append(str(tei))
        else:
            result["errors"].append("GROBID failed for supp PDF")

    # Process other supplementary files (docx -> pdf -> grobid)
    supp_other_files_saved = []
    if file_info.get("supp_other"):
        supp_dir_out = paper_dir / "supplementary"
        supp_dir_out.mkdir(exist_ok=True)
        
        for i, fpath_str in enumerate(file_info["supp_other"]):
            fpath = Path(fpath_str)
            if fpath.exists():
                # Copy original
                dest = supp_dir_out / fpath.name
                if not dest.exists():
                    shutil.copy2(fpath, dest)
                supp_other_files_saved.append(str(dest))
                
                # Convert to PDF if DOCX
                if fpath.suffix.lower() == '.docx':
                    pdf_path = convert_to_pdf(dest, supp_dir_out)
                    if pdf_path:
                        # Run GROBID
                        # Use unique name for TEI: supp_docx_{i}.tei.xml
                        tei_name = f"supp_docx_{i}"
                        tei = run_grobid_on_pdf(pdf_path, paper_dir, tei_name)
                        if tei:
                            supp_teis.append(str(tei))
    
    # Save metadata
    meta = {
        "pii": pii,
        "created_at": datetime.now().isoformat(),
        "main_pdf_source": str(file_info["main_pdf"]) if file_info["main_pdf"] else None,
        "supp_pdf_source": str(file_info["supp_pdfs"][0]) if file_info["supp_pdfs"] else None,
        "supp_other_files": supp_other_files_saved,
        "main_tei": result["main_tei"],
        "supp_tei": result["supp_tei"],
        "all_supp_teis": supp_teis
    }
    write_json(paper_dir / "meta.json", meta)
    
    result["success"] = result["main_tei"] is not None
    return result


# ============================================================================
# Chunking Stage
# ============================================================================
def run_chunking(pii: str, data_root: Path) -> bool:
    """Run the chunking script for a paper."""
    try:
        cmd = [
            sys.executable,
            str(Path(__file__).parent / "00_ingest_build_chunks.py"),
            "--data-root", str(data_root),
            "--paper-id", pii
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error(f"  Chunking failed: {result.stderr[:200]}")
            return False
        return True
    except Exception as e:
        logger.error(f"  Chunking error: {e}")
        return False


# ============================================================================
# Extraction Stage
# ============================================================================
def run_extraction(pii: str, data_root: Path, run_dir: Path) -> Dict[str, Any]:
    """Run the extraction pipeline for a paper."""
    try:
        # Import here to avoid circular imports
        from scripts.run_pipeline_v2 import process_paper_full
        return process_paper_full(data_root, pii, run_dir)
    except Exception as e:
        logger.error(f"  Extraction error: {e}")
        return {"pii": pii, "status": "FAILED", "errors": [str(e)]}


# ============================================================================
# Main Pipeline
# ============================================================================
def run_full_pipeline(
    pii_list: List[str],
    pdf_dir: Path,
    supp_dir: Path,
    data_root: Path,
    run_dir: Path,
    skip_grobid: bool = False,
    skip_chunking: bool = False,
    max_papers: int = 0
) -> Dict[str, Any]:
    """
    Run the complete pipeline for all papers.
    
    Stages:
    1. LOCATE: Find PDF and supplementary files
    2. GROBID: Generate TEI XML
    3. CHUNK: Build text chunks
    4. EXTRACT: Run LLM extraction
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    
    if max_papers > 0:
        pii_list = pii_list[:max_papers]
    
    total = len(pii_list)
    logger.info(f"Starting pipeline for {total} papers")
    logger.info(f"  PDF dir: {pdf_dir}")
    logger.info(f"  Supp dir: {supp_dir}")
    logger.info(f"  Data root: {data_root}")
    logger.info(f"  Run dir: {run_dir}")
    
    # Results tracking
    results = {
        "started_at": datetime.now().isoformat(),
        "total_papers": total,
        "stages": {
            "locate": {"success": 0, "failed": 0},
            "grobid": {"success": 0, "failed": 0, "skipped": 0},
            "chunk": {"success": 0, "failed": 0, "skipped": 0},
            "extract": {"success": 0, "failed": 0},
        },
        "paper_results": []
    }
    
    for i, pii in enumerate(pii_list, 1):
        logger.info(f"[{i}/{total}] Processing: {pii}")
        
        paper_result = {
            "pii": pii,
            "stages": {},
            "success": False
        }
        
        # Stage 1: Locate files
        file_info = locate_paper_files(pii, pdf_dir, supp_dir)
        paper_result["stages"]["locate"] = file_info
        
        if not file_info["found_main"]:
            logger.warning(f"  Main PDF not found, skipping")
            results["stages"]["locate"]["failed"] += 1
            results["paper_results"].append(paper_result)
            continue
        
        results["stages"]["locate"]["success"] += 1
        logger.info(f"  Found: main={file_info['found_main']}, supp={file_info['found_supp']}")
        
        # Stage 2: GROBID
        if skip_grobid:
            results["stages"]["grobid"]["skipped"] += 1
            paper_result["stages"]["grobid"] = {"skipped": True}
        else:
            grobid_result = setup_paper_directory(pii, file_info, data_root)
            paper_result["stages"]["grobid"] = grobid_result
            
            if grobid_result["success"]:
                results["stages"]["grobid"]["success"] += 1
            else:
                results["stages"]["grobid"]["failed"] += 1
                logger.warning(f"  GROBID failed: {grobid_result['errors']}")
                results["paper_results"].append(paper_result)
                continue
        
        # Stage 3: Chunking
        if skip_chunking:
            results["stages"]["chunk"]["skipped"] += 1
            paper_result["stages"]["chunk"] = {"skipped": True}
        else:
            chunk_ok = run_chunking(pii, data_root)
            paper_result["stages"]["chunk"] = {"success": chunk_ok}
            
            if chunk_ok:
                results["stages"]["chunk"]["success"] += 1
            else:
                results["stages"]["chunk"]["failed"] += 1
                results["paper_results"].append(paper_result)
                continue
        
        # Stage 4: Extraction
        extract_result = run_extraction(pii, data_root, run_dir)
        paper_result["stages"]["extract"] = extract_result
        
        if extract_result.get("status") == "DONE":
            results["stages"]["extract"]["success"] += 1
            paper_result["success"] = True
        else:
            results["stages"]["extract"]["failed"] += 1
        
        results["paper_results"].append(paper_result)
    
    # Save results
    results["finished_at"] = datetime.now().isoformat()
    write_json(run_dir / "pipeline_results.json", results)
    
    # Summary
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info(f"  Locate: {results['stages']['locate']['success']}/{total} success")
    logger.info(f"  GROBID: {results['stages']['grobid']['success']}/{total} success")
    logger.info(f"  Chunk: {results['stages']['chunk']['success']}/{total} success")
    logger.info(f"  Extract: {results['stages']['extract']['success']}/{total} success")
    logger.info("=" * 60)
    
    return results


# ============================================================================
# CLI Entry Point
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="AZIB Extraction Pipeline - Main Entry Point",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Process papers listed in papers.txt
    python scripts/main_pipeline.py --pii-list papers.txt
    
    # Skip GROBID if TEI already exists
    python scripts/main_pipeline.py --pii-list papers.txt --skip-grobid
    
    # Process max 10 papers
    python scripts/main_pipeline.py --pii-list papers.txt --max-papers 10
"""
    )
    
    parser.add_argument(
        "--pii-list", type=str, required=True,
        help="Text file with paper IDs (PIIs), one per line"
    )
    parser.add_argument(
        "--pdf-dir", type=str, default="pdfs",
        help="Directory containing main PDFs (default: pdfs)"
    )
    parser.add_argument(
        "--supp-dir", type=str, default="supplementary_files",
        help="Directory containing supplementary files (default: supplementary_files)"
    )
    parser.add_argument(
        "--data-root", type=str, default="data",
        help="Data root directory for processed papers (default: data)"
    )
    parser.add_argument(
        "--run-dir", type=str, default=None,
        help="Run output directory (default: runs/run_YYYYMMDD_HHMMSS)"
    )
    parser.add_argument(
        "--skip-grobid", action="store_true",
        help="Skip GROBID processing (use existing TEI files)"
    )
    parser.add_argument(
        "--skip-chunking", action="store_true",
        help="Skip chunking (use existing chunk files)"
    )
    parser.add_argument(
        "--max-papers", type=int, default=0,
        help="Maximum papers to process (0 = all)"
    )
    parser.add_argument(
        "--grobid-url", type=str, default=None,
        help="GROBID server URL (default: http://localhost:8070)"
    )
    
    args = parser.parse_args()
    
    # Override GROBID URL if specified
    if args.grobid_url:
        Config.GROBID_URL = args.grobid_url
    
    # Set default run dir
    if args.run_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.run_dir = f"runs/run_{timestamp}"
    
    # Convert to paths
    pii_list_path = Path(args.pii_list)
    pdf_dir = Path(args.pdf_dir)
    supp_dir = Path(args.supp_dir)
    data_root = Path(args.data_root)
    run_dir = Path(args.run_dir)
    
    # Validate
    if not pii_list_path.exists():
        logger.error(f"PII list file not found: {pii_list_path}")
        sys.exit(1)
    
    if not pdf_dir.exists():
        logger.error(f"PDF directory not found: {pdf_dir}")
        sys.exit(1)
    
    # Load PIIs
    pii_list = load_pii_list(pii_list_path)
    
    if not pii_list:
        logger.error("No PIIs found in list file")
        sys.exit(1)
    
    # Run pipeline
    run_full_pipeline(
        pii_list=pii_list,
        pdf_dir=pdf_dir,
        supp_dir=supp_dir,
        data_root=data_root,
        run_dir=run_dir,
        skip_grobid=args.skip_grobid,
        skip_chunking=args.skip_chunking,
        max_papers=args.max_papers
    )


if __name__ == "__main__":
    main()
