# scripts/run_full_pipeline.py
"""
End-to-End Pipeline Runner

Reads PIIs from paper_list.txt, finds PDFs, runs GROBID parsing, 
and executes full extraction pipeline.

Usage:
    python scripts/run_full_pipeline.py --paper-list paper_list.txt
    python scripts/run_full_pipeline.py --paper-list paper_list.txt --skip-grobid
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# PDF search locations (relative to project root's parent)
PDF_SEARCH_PATHS = [
    "../pdfs",           # Main PDFs folder
    "../pdfs_77",        # Additional PDFs folder  
    "pdfs",              # In project root
]

# Supplementary file locations
SUPP_SEARCH_PATHS = [
    "../supplementary_files",
    "../supplementary_files_77",
]

# GROBID server URL
GROBID_URL = os.environ.get("GROBID_URL", "http://localhost:8080")


def find_pdf(pii: str, project_root: Path) -> Path | None:
    """
    Find PDF file for a given PII.
    
    Handles patterns like:
    - 1-s2.0-S000862232400438X-main.pdf (Elsevier format)
    - {PII}.pdf
    - {PII}_main.pdf
    - Folder/{PII}*.pdf
    """
    for search_path in PDF_SEARCH_PATHS:
        base = project_root / search_path
        if not base.exists():
            continue
        
        # Strategy 1: Exact filename patterns
        exact_patterns = [
            f"{pii}.pdf",
            f"{pii}_main.pdf",
            f"1-s2.0-{pii}-main.pdf",      # Elsevier pattern
            f"1-s2.0-{pii}-mainext.pdf",   # Extended version
        ]
        
        for pattern in exact_patterns:
            pdf_path = base / pattern
            if pdf_path.exists() and pdf_path.is_file():
                logger.info(f"  Found PDF (exact): {pdf_path}")
                return pdf_path
        
        # Strategy 2: Fuzzy match - find any file containing the PII
        for f in base.iterdir():
            if f.is_file() and f.suffix.lower() == '.pdf':
                if pii in f.name:
                    logger.info(f"  Found PDF (fuzzy): {f}")
                    return f
        
        # Strategy 3: Check if PII is a subfolder
        pii_folder = base / pii
        if pii_folder.exists() and pii_folder.is_dir():
            for f in pii_folder.glob("*.pdf"):
                logger.info(f"  Found PDF (subfolder): {f}")
                return f
    
    return None


def find_supplementary(pii: str, project_root: Path) -> list[Path]:
    """
    Find supplementary files for a given PII.
    
    Handles structures like:
    - supplementary_files/{PII}/{PII}_...-mmc1.docx
    - supplementary_files/{PII}/*.pdf
    """
    supp_files = []
    
    for search_path in SUPP_SEARCH_PATHS:
        base = project_root / search_path
        if not base.exists():
            continue
        
        # Check PII subfolder
        pii_folder = base / pii
        if pii_folder.exists() and pii_folder.is_dir():
            for f in pii_folder.iterdir():
                if f.is_file() and f.suffix.lower() in ['.pdf', '.docx', '.doc']:
                    supp_files.append(f)
    
    return supp_files


def convert_docx_to_pdf(docx_path: Path, output_dir: Path) -> Path | None:
    """
    Convert DOCX file to PDF using LibreOffice or python-docx2pdf.
    
    Returns path to converted PDF or None if conversion failed.
    """
    pdf_path = output_dir / f"{docx_path.stem}.pdf"
    
    if pdf_path.exists():
        logger.info(f"  DOCX→PDF: Already converted, skipping")
        return pdf_path
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Try python-docx2pdf first (Windows)
    try:
        from docx2pdf import convert
        convert(str(docx_path), str(pdf_path))
        logger.info(f"  DOCX→PDF: Converted using docx2pdf")
        return pdf_path
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"  DOCX→PDF: docx2pdf failed - {e}")
    
    # Try LibreOffice (cross-platform fallback)
    try:
        result = subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", 
             str(output_dir), str(docx_path)],
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode == 0 and pdf_path.exists():
            logger.info(f"  DOCX→PDF: Converted using LibreOffice")
            return pdf_path
    except Exception as e:
        logger.warning(f"  DOCX→PDF: LibreOffice failed - {e}")
    
    logger.error(f"  DOCX→PDF: All conversion methods failed for {docx_path}")
    return None


def run_grobid(pdf_path: Path, output_dir: Path) -> bool:
    """
    Run GROBID to parse PDF into TEI XML.
    
    Uses the grobid_client if available, otherwise calls API directly.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Check if already processed (check both old and new naming)
    xml_path = output_dir / "main.tei.xml"
    old_xml_path = output_dir / "tei.xml"
    if xml_path.exists() or old_xml_path.exists():
        logger.info(f"  GROBID: Already processed, skipping")
        return True
    
    try:
        import requests
        
        # Use GROBID processFulltextDocument API
        url = f"{GROBID_URL}/api/processFulltextDocument"
        
        with open(pdf_path, 'rb') as f:
            files = {'input': f}
            params = {
                'consolidateHeader': '1',
                'consolidateCitations': '0',
                'includeRawCitations': '1',
                'teiCoordinates': 'figure,ref,s,formula',
            }
            
            logger.info(f"  GROBID: Calling {url}...")
            resp = requests.post(url, files=files, data=params, timeout=300)
        
        if resp.status_code == 200:
            with open(xml_path, 'w', encoding='utf-8') as f:
                f.write(resp.text)
            logger.info(f"  GROBID: Saved TEI XML to {xml_path}")
            return True
        else:
            logger.error(f"  GROBID: Failed with status {resp.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"  GROBID: Error - {e}")
        return False


def run_chunking(paper_id: str, data_root: Path) -> bool:
    """Run the chunking script to split TEI into chunks."""
    try:
        logger.info(f"  Chunking: Running ingest...")
        # Use 00_ingest_build_chunks.py with streaming output
        cmd = [
            sys.executable, 
            "scripts/00_ingest_build_chunks.py", 
            "--data-root", str(data_root),
            "--paper-id", paper_id
        ]
        
        result = subprocess.run(cmd, check=False, timeout=120)
        
        if result.returncode == 0:
            logger.info(f"  Chunking: Success")
            return True
        else:
            logger.error(f"  Chunking: Failed (code {result.returncode})")
            return False
    except Exception as e:
        logger.error(f"  Chunking: Error - {e}")
        return False


def run_extraction_pipeline(paper_id: str, run_dir: Path, data_root: Path, no_cache: bool = False) -> bool:
    """Run the full extraction pipeline."""
    try:
        # Stream output to console so user can see progress (16_설계 requirement)
        cmd = [
            sys.executable, "scripts/run_pipeline_v2.py",
            "--run-dir", str(run_dir),
            "--data-root", str(data_root),
            "--paper-id", paper_id
        ]
        
        if no_cache:
            cmd.append("--no-cache")
        
        logger.info(f"  Extraction: Running pipeline...")
        # Use simple subprocess.run without capture_output to stream to stdout/stderr
        result = subprocess.run(
            cmd,
            check=False,  # Don't raise, just return code
            timeout=600   # 10 minutes per paper
        )
        
        if result.returncode == 0:
            logger.info(f"  Extraction: Success")
            return True
        else:
            logger.error(f"  Extraction: Failed with return code {result.returncode}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error(f"  Extraction: Timed out after 600s")
        return False
    except Exception as e:
        logger.error(f"  Extraction: Error - {e}")
        return False


def process_paper(
    pii: str,
    project_root: Path,
    data_root: Path,
    run_dir: Path,
    skip_grobid: bool = False,
    no_cache: bool = False
) -> dict:
    """
    Process a single paper through the full pipeline.
    
    Returns stats dict with status.
    """
    logger.info(f"=" * 60)
    logger.info(f"Processing: {pii}")
    logger.info(f"=" * 60)
    
    stats = {
        "pii": pii,
        "started_at": datetime.now().isoformat(),
        "pdf_found": False,
        "grobid_done": False,
        "chunking_done": False,
        "extraction_done": False,
        "status": "STARTED",
        "error": None
    }
    
    # Step 1: Find PDF
    pdf_path = find_pdf(pii, project_root)
    if not pdf_path:
        logger.error(f"  PDF not found for {pii}")
        stats["status"] = "PDF_NOT_FOUND"
        return stats
    
    logger.info(f"  PDF found: {pdf_path}")
    stats["pdf_found"] = True
    stats["pdf_path"] = str(pdf_path)
    
    # Step 2: Setup paper directory
    paper_dir = data_root / "papers" / pii
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "derived").mkdir(exist_ok=True)
    
    # Copy PDF to paper directory if not already there
    local_pdf = paper_dir / "main.pdf"
    if not local_pdf.exists():
        shutil.copy(pdf_path, local_pdf)
        logger.info(f"  Copied PDF to {local_pdf}")
    
    # Step 3: Run GROBID
    if not skip_grobid:
        if not run_grobid(local_pdf, paper_dir):
            stats["status"] = "GROBID_FAILED"
            return stats
    stats["grobid_done"] = True
    
    # Step 4: Run chunking
    if not run_chunking(pii, data_root):
        stats["status"] = "CHUNKING_FAILED"
        return stats
    stats["chunking_done"] = True
    
    # Step 5: Run extraction pipeline
    if not run_extraction_pipeline(pii, run_dir, data_root, no_cache=no_cache):
        stats["status"] = "EXTRACTION_FAILED"
        return stats
    stats["extraction_done"] = True
    
    stats["status"] = "SUCCESS"
    stats["finished_at"] = datetime.now().isoformat()
    logger.info(f"  Complete: {pii}")
    
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="End-to-End Pipeline: paper_list.txt → GROBID → Extraction"
    )
    parser.add_argument(
        "--paper-list", type=str, required=True,
        help="Path to paper_list.txt containing PIIs (one per line)"
    )
    parser.add_argument(
        "--data-root", type=str, default="data",
        help="Data root directory (default: data)"
    )
    parser.add_argument(
        "--run-dir", type=str, default=None,
        help="Run directory for results (default: runs/run_YYYYMMDD_HHMMSS)"
    )
    parser.add_argument(
        "--skip-grobid", action="store_true",
        help="Skip GROBID parsing (use existing TEI XML)"
    )
    parser.add_argument(
        "--max-papers", type=int, default=0,
        help="Maximum papers to process (0=all)"
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Force re-extraction (bypass LLM cache)"
    )
    
    args = parser.parse_args()
    
    # Setup paths
    project_root = Path(__file__).parent.parent
    data_root = Path(args.data_root)
    
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        run_dir = Path(f"runs/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    
    run_dir.mkdir(parents=True, exist_ok=True)
    
    # Read paper list
    paper_list_path = Path(args.paper_list)
    if not paper_list_path.exists():
        logger.error(f"Paper list not found: {paper_list_path}")
        return 1
    
    with open(paper_list_path, 'r', encoding='utf-8') as f:
        piis = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    if args.max_papers > 0:
        piis = piis[:args.max_papers]
    
    logger.info(f"Found {len(piis)} papers to process")
    logger.info(f"Run directory: {run_dir}")
    
    # Process each paper
    results = []
    for i, pii in enumerate(piis):
        logger.info(f"\n[{i+1}/{len(piis)}] Processing {pii}")
        stats = process_paper(
            pii=pii,
            project_root=project_root,
            data_root=data_root,
            run_dir=run_dir,
            skip_grobid=args.skip_grobid,
            no_cache=args.no_cache
        )
        results.append(stats)
    
    # Summary
    success = sum(1 for r in results if r["status"] == "SUCCESS")
    failed = len(results) - success
    
    logger.info(f"\n" + "=" * 60)
    logger.info(f"SUMMARY: {success}/{len(results)} succeeded, {failed} failed")
    logger.info(f"=" * 60)
    
    for r in results:
        status_icon = "✓" if r["status"] == "SUCCESS" else "✗"
        logger.info(f"  {status_icon} {r['pii']}: {r['status']}")
    
    # Save results summary
    import json
    summary_path = run_dir / "pipeline_summary.json"
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    logger.info(f"\nResults saved to: {summary_path}")
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
