# scripts/00_ingest_build_chunks.py
"""
Build chunks from GROBID TEI XML files.

This script processes TEI XML files (MAIN and SUPP) and creates:
- 01_chunks_main.jsonl
- 01_chunks_supp.jsonl
- 00_inventory.json (figures, tables, sections)

Usage:
    python scripts/00_ingest_build_chunks.py --data-root data --paper-id S0378775323006754
    python scripts/00_ingest_build_chunks.py --data-root data --all
"""
from __future__ import annotations
import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.io_jsonl import write_json, write_jsonl

# TEI namespaces
NS = {"tei": "http://www.tei-c.org/ns/1.0"}

# Target chunk size (tokens ~ words * 1.3)
MIN_CHUNK_WORDS = 400
MAX_CHUNK_WORDS = 900


def _text(elem: Optional[ET.Element]) -> str:
    """Extract all text from an element recursively."""
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def _get_section_path(div: ET.Element, parent_path: str = "") -> str:
    """Extract section path from div element."""
    head = div.find("tei:head", NS)
    if head is not None:
        title = _text(head)
        if title:
            return f"{parent_path} > {title}" if parent_path else title
    return parent_path


def _extract_figures(root: ET.Element, doc: str) -> List[Dict[str, Any]]:
    """Extract figure information from TEI."""
    figures = []
    for fig in root.findall(".//tei:figure", NS):
        fig_id = fig.get("{http://www.w3.org/XML/1998/namespace}id") or fig.get("id") or ""
        head = fig.find("tei:head", NS)
        figdesc = fig.find("tei:figDesc", NS)
        label = fig.find("tei:label", NS)
        
        caption_parts = []
        if label is not None:
            caption_parts.append(_text(label))
        if head is not None:
            caption_parts.append(_text(head))
        if figdesc is not None:
            caption_parts.append(_text(figdesc))
        
        caption = " ".join(caption_parts)
        
        if caption or fig_id:
            figures.append({
                "doc": doc,
                "figure_id": fig_id or f"fig_{len(figures)+1}",
                "caption": caption,
                "page": None  # TEI doesn't always have page info
            })
    return figures


def _extract_tables(root: ET.Element, doc: str) -> List[Dict[str, Any]]:
    """Extract table information from TEI."""
    tables = []
    for tbl in root.findall(".//tei:figure[@type='table']", NS):
        tbl_id = tbl.get("{http://www.w3.org/XML/1998/namespace}id") or tbl.get("id") or ""
        head = tbl.find("tei:head", NS)
        figdesc = tbl.find("tei:figDesc", NS)
        label = tbl.find("tei:label", NS)
        
        caption_parts = []
        if label is not None:
            caption_parts.append(_text(label))
        if head is not None:
            caption_parts.append(_text(head))
        if figdesc is not None:
            caption_parts.append(_text(figdesc))
        
        caption = " ".join(caption_parts)
        
        # Try to parse table content
        table_elem = tbl.find("tei:table", NS)
        parsed = None
        if table_elem is not None:
            rows = []
            for row in table_elem.findall("tei:row", NS):
                cells = [_text(c) for c in row.findall("tei:cell", NS)]
                rows.append(cells)
            if rows:
                parsed = "\n".join(["\t".join(r) for r in rows])
        
        if caption or tbl_id:
            tables.append({
                "doc": doc,
                "table_id": tbl_id or f"table_{len(tables)+1}",
                "caption": caption,
                "parsed": parsed,
                "page": None
            })
    return tables


def _extract_sections(root: ET.Element) -> List[Dict[str, Any]]:
    """Extract section structure from TEI."""
    sections = []
    body = root.find(".//tei:body", NS)
    if body is None:
        return sections
    
    for div in body.findall(".//tei:div", NS):
        path = _get_section_path(div)
        if path:
            # Count paragraphs
            n_p = len(div.findall("tei:p", NS))
            sections.append({
                "section_path": path,
                "n_paragraphs": n_p
            })
    return sections


def _chunk_text(paragraphs: List[Tuple[str, str]], doc: str, paper_id: str) -> List[Dict[str, Any]]:
    """
    Chunk paragraphs into appropriately sized chunks.
    
    Args:
        paragraphs: List of (section_path, text) tuples
        doc: MAIN or SUPP
        paper_id: Paper identifier
    
    Returns:
        List of chunk dicts
    """
    chunks = []
    current_text = []
    current_section = ""
    current_words = 0
    chunk_idx = 1
    
    for section_path, text in paragraphs:
        words = len(text.split())
        
        # If adding this paragraph would exceed max, flush current chunk
        if current_words + words > MAX_CHUNK_WORDS and current_words >= MIN_CHUNK_WORDS:
            if current_text:
                chunks.append({
                    "paper_id": paper_id,
                    "doc": doc,
                    "chunk_id": f"C-{doc[0]}-{chunk_idx:05d}",
                    "section_path": current_section,
                    "page_range": None,
                    "text": " ".join(current_text)
                })
                chunk_idx += 1
                current_text = []
                current_words = 0
        
        current_text.append(text)
        current_words += words
        current_section = section_path if section_path else current_section
    
    # Flush remaining
    if current_text:
        chunks.append({
            "paper_id": paper_id,
            "doc": doc,
            "chunk_id": f"C-{doc[0]}-{chunk_idx:05d}",
            "section_path": current_section,
            "page_range": None,
            "text": " ".join(current_text)
        })
    
    return chunks


def _extract_paragraphs(root: ET.Element) -> List[Tuple[str, str]]:
    """Extract all paragraphs with their section paths."""
    paragraphs = []
    body = root.find(".//tei:body", NS)
    if body is None:
        # Try abstract
        abstract = root.find(".//tei:abstract", NS)
        if abstract is not None:
            for p in abstract.findall(".//tei:p", NS):
                text = _text(p)
                if text:
                    paragraphs.append(("Abstract", text))
        return paragraphs
    
    def _process_div(div: ET.Element, parent_path: str = ""):
        section_path = _get_section_path(div, parent_path)
        
        # Process paragraphs in this div
        for p in div.findall("tei:p", NS):
            text = _text(p)
            if text:
                paragraphs.append((section_path, text))
        
        # Process nested divs
        for subdiv in div.findall("tei:div", NS):
            _process_div(subdiv, section_path)
    
    for div in body.findall("tei:div", NS):
        _process_div(div)
    
    # Also get abstract
    abstract = root.find(".//tei:abstract", NS)
    if abstract is not None:
        abstract_paragraphs = []
        for p in abstract.findall(".//tei:p", NS):
            text = _text(p)
            if text:
                abstract_paragraphs.append(("Abstract", text))
        paragraphs = abstract_paragraphs + paragraphs
    
    return paragraphs


def process_tei(tei_path: Path, doc: str, paper_id: str) -> Tuple[List[Dict], Dict]:
    """
    Process a single TEI file.
    
    Returns:
        (chunks, inventory_part)
    """
    try:
        tree = ET.parse(tei_path)
        root = tree.getroot()
    except Exception as e:
        print(f"  Warning: Failed to parse {tei_path}: {e}")
        return [], {"figures": [], "tables": [], "sections": []}
    
    # Extract inventory
    figures = _extract_figures(root, doc)
    tables = _extract_tables(root, doc)
    sections = _extract_sections(root)
    
    # Extract and chunk paragraphs
    paragraphs = _extract_paragraphs(root)
    chunks = _chunk_text(paragraphs, doc, paper_id)
    
    inventory_part = {
        "figures": figures,
        "tables": tables,
        "sections": sections
    }
    
    return chunks, inventory_part


def process_paper(paper_dir: Path, paper_id: str) -> bool:
    """
    Process a single paper directory.
    
    Expected structure:
        paper_dir/
            main.tei.xml
            supp.tei.xml
            supp_docx_*.tei.xml (from converted docx)
    
    Creates:
        paper_dir/derived/
            00_inventory.json
            01_chunks_main.jsonl
            01_chunks_supp.jsonl
    """
    derived = paper_dir / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    
    # 1. Identify all TEI files
    main_tei = None
    supp_teis = []
    
    for f in paper_dir.glob("*.tei.xml"):
        name_lower = f.name.lower()
        if "main" in name_lower:
            main_tei = f
        elif "supp" in name_lower:
            supp_teis.append(f)
        else:
            # Fallback for unclassified TEI: assume main if no main yet, otherwise likely supp
            if not main_tei:
                main_tei = f
            else:
                supp_teis.append(f)
    
    if main_tei is None:
        print(f"  Warning: No main TEI file found in {paper_dir}")
        return False
    
    # 2. Process Main
    print(f"  Processing MAIN: {main_tei.name}")
    chunks_main, inv_main = process_tei(main_tei, "MAIN", paper_id)
    
    # 3. Process All Supplementary TEIs
    chunks_supp = []
    inv_supp = {"figures": [], "tables": [], "sections": []}
    
    # Sort for deterministic order (e.g. supp.tei.xml, supp_docx_0.tei.xml)
    for supp_path in sorted(supp_teis):
        print(f"  Processing SUPP: {supp_path.name}")
        # Use filename key as doc identifier (e.g. SUPP_DOCX_0) or just generic SUPP
        doc_key = "SUPP"
        if "docx" in supp_path.name:
            # unique key for converted files? or keep it simple as SUPP to be merged?
            # Keeping it as SUPP might be simpler for downstream, but maybe we want separation?
            # Existing pipeline probably expects "SUPP" or "MAIN" in 'doc' field.
            doc_key = "SUPP" 
        
        c_tei, i_tei = process_tei(supp_path, doc_key, paper_id)
        chunks_supp.extend(c_tei)
        inv_supp["figures"].extend(i_tei["figures"])
        inv_supp["tables"].extend(i_tei["tables"])
        inv_supp["sections"].extend(i_tei["sections"])
    
    # Merge inventory
    inventory = {
        "paper_id": paper_id,
        "figures": inv_main["figures"] + inv_supp["figures"],
        "tables": inv_main["tables"] + inv_supp["tables"],
        "sections": inv_main["sections"]
    }
    
    # Write outputs
    write_json(derived / "00_inventory.json", inventory)
    write_jsonl(derived / "01_chunks_main.jsonl", chunks_main)
    write_jsonl(derived / "01_chunks_supp.jsonl", chunks_supp)
    
    print(f"  Created: {len(chunks_main)} main chunks, {len(chunks_supp)} supp chunks (from {len(supp_teis)} supp files)")
    print(f"  Inventory: {len(inventory['figures'])} figures, {len(inventory['tables'])} tables")
    
    return True


def main():
    parser = argparse.ArgumentParser(description="Build chunks from GROBID TEI files")
    parser.add_argument("--data-root", type=str, default="data", help="Data root directory")
    parser.add_argument("--paper-id", type=str, help="Single paper ID to process")
    parser.add_argument("--all", action="store_true", help="Process all papers")
    args = parser.parse_args()
    
    data_root = Path(args.data_root)
    papers_dir = data_root / "papers"
    
    if args.paper_id:
        paper_dir = papers_dir / args.paper_id
        if not paper_dir.exists():
            print(f"Error: Paper directory not found: {paper_dir}")
            return
        print(f"Processing paper: {args.paper_id}")
        process_paper(paper_dir, args.paper_id)
    elif args.all:
        if not papers_dir.exists():
            print(f"Error: Papers directory not found: {papers_dir}")
            return
        for paper_dir in sorted(papers_dir.iterdir()):
            if paper_dir.is_dir():
                print(f"Processing: {paper_dir.name}")
                process_paper(paper_dir, paper_dir.name)
    else:
        print("Please specify --paper-id or --all")
        parser.print_help()


if __name__ == "__main__":
    main()
