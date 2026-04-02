#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script to extract Title, Abstract, and Experimental/Method sections from marker-pdf generated MD files
and save them as JSON files for later LLM classification.

This script processes all subfolder MD files in pdfs_marker_output directory.
"""

import os
import re
import json
import argparse
from pathlib import Path
from typing import Dict, Optional, List, Tuple
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

def clean_header(header_line: str) -> str:
    """
    Remove markdown hashes, bolding, and extra whitespace from a header line.
    Example: "## **2. Experimental**" -> "2. Experimental"
    """
    # Remove leading hashes (1-6) and whitespace
    text = re.sub(r'^#{1,6}\s*', '', header_line)
    # Remove bold/italic markers (* or _)
    text = re.sub(r'[\*_]{2,}', '', text)
    return text.strip()

def get_header_level(line: str) -> int:
    """Return the markdown header level (number of starting #). Returns 0 if not a header."""
    match = re.match(r'^(#{1,6})\s', line)
    return len(match.group(1)) if match else 0

def extract_title(md_content: str) -> str:
    """
    Extract title from markdown content.
    Strategy: Look for the first major header (H1 or H2) that isn't a metadata label.
    """
    lines = md_content.split('\n')
    
    # Common non-title headers to ignore
    ignore_keywords = {
        'introduction', 'abstract', 'method', 'experimental', 'results', 
        'conclusion', 'reference', 'acknowledgement', 'appendix', 
        'article info', 'keywords', 'contents', 'homepage', 'elsevier', 
        'sciencedirect', 'journal'
    }

    # 1. Scan first 50 lines for H1 (#) or H2 (##)
    for line in lines[:50]:
        line_stripped = line.strip()
        level = get_header_level(line_stripped)
        
        if level in [1, 2]:
            text = clean_header(line_stripped)
            text_lower = text.lower()
            
            # Check if likely a label to ignore
            if any(k in text_lower for k in ignore_keywords):
                continue
            
            # Additional heuristic: Titles are usually somewhat long but not a paragraph
            # and don't look like a URL or email
            if len(text) > 10 and 'http' not in text_lower:
                return text

    # 2. Fallback: Find the longest line in the first 20 lines that looks like a title
    # (Capitalized, no ending punctuation like period)
    candidates = []
    for line in lines[:20]:
        t = line.strip()
        # Remove bolding if present for length check
        t_clean = re.sub(r'[\*_]', '', t)
        if len(t_clean) > 20 and not t_clean.startswith('http') and not any(k in t_clean.lower() for k in ignore_keywords):
            candidates.append(t)
            
    if candidates:
        # Return longest candidate
        return max(candidates, key=len).strip(' *_')

    return ""


def extract_abstract(md_content: str) -> str:
    """
    Extract abstract from markdown content.
    Finds header matching 'Abstract' and reads until next header of same/higher level.
    """
    lines = md_content.split('\n')
    abstract_lines = []
    in_abstract = False
    abstract_level = 0
    
    for line in lines:
        line_stripped = line.strip()
        level = get_header_level(line_stripped)
        
        if level > 0:
            header_text = clean_header(line_stripped).lower()
            
            # Start of Abstract
            if 'abstract' in header_text and not in_abstract:
                in_abstract = True
                abstract_level = level
                continue
            
            # End of Abstract?
            if in_abstract:
                # Stop if we hit a header of same or higher level (smaller number)
                # OR if we hit specific sections like Introduction regardless of level
                if level <= abstract_level or 'introduction' in header_text:
                    break
        
        if in_abstract:
            # Skip empty lines or metadata lines like "Keywords:"
            if not line_stripped:
                continue
            if line_stripped.lower().startswith('keywords'):
                continue
                
            abstract_lines.append(line_stripped)
            
    return ' '.join(abstract_lines).strip()


def extract_experimental_method(md_content: str) -> str:
    """
    Extract Experimental or Method section.
    Finds headers like 'Experimental', 'Methods', etc.
    Extracts content until next major section.
    """
    lines = md_content.split('\n')
    exp_lines = []
    in_exp = False
    exp_header_level = 0
    
    target_keywords = ['experimental', 'material', 'method', 'preparation']
    stop_keywords = ['result', 'discussion', 'conclusion', 'reference', 'acknowledgement', 'appendix', 'credit', 'declaration']

    for line in lines:
        line_stripped = line.strip()
        level = get_header_level(line_stripped)
        
        if level > 0:
            header_text = clean_header(line_stripped).lower()
            
            if not in_exp:
                # Check for start of experimental section
                # Must contain target keyword
                if any(k in header_text for k in target_keywords):
                    # But NOT verify it's not "Results and Discussion" or similar if generic
                    # e.g. "Materials" is okay, "Supplementary Materials" handled below?
                    if 'supplementary' in header_text:
                        continue
                        
                    in_exp = True
                    exp_header_level = level
                    # exp_lines.append(f"[{line_stripped}]") # Debug: keep header? No.
                    continue
            else:
                # Already in experimental section, check if we should stop
                
                # Logic 1: Stop if we hit a header of the SAME or HIGHER level (e.g. ## -> ## or #)
                # IF and ONLY IF the new header is NOT a subsection (which usually has higher # count)
                # But sometimes MD structure is messy.
                # Let's rely mainly on KEYWORDS for stopping, or level jump.
                
                # Stop if header contains "Results", "Discussion", "Conclusion"
                if any(k in header_text for k in stop_keywords):
                    break
                    
                # Also stop if we hit a header of strictly higher level (e.g. ## Exp -> # Ref)
                # (Smaller number = higher level)
                if level < exp_header_level:
                    break
                
                # If same level (e.g. ## 2. Exp, ## 3. Results), we stop IF it looks like a new section
                # If we didn't catch it with stop_keywords, maybe check if it starts with a number?
                if level == exp_header_level:
                    # Check if it starts with a number that is different? 
                    # Complex logic. Generally 'Results' keyword is robust enough.
                    # But what if "## 3. Characterization"? That should be kept? Or new section?
                    # Usually Characterization is part of Method or separate.
                    # Let's assume SAME level creates a break unless it's clearly a continuation pattern.
                    # For safety, if we hit a same-level header that is NOT in stop_keywords,
                    # we might technically be in a new section (e.g. Introduction -> Related Work).
                    # But for Experimental, usually subsections are ####.
                    # If we see ## 3. Results, we break.
                    # If we see ## 3. Characterization, maybe we should break? 
                    # Let's be aggressive: Break on SAME level header too, unless it contains target_keywords again?
                    
                    if not any(k in header_text for k in target_keywords):
                         break
                
        if in_exp:
            exp_lines.append(line_stripped)

    # Clean up result
    full_text = ' '.join(exp_lines).strip()
    
    # Text length guard
    if len(full_text) > 15000:
        full_text = full_text[:15000] + "..."
        
    return full_text


def extract_sections_from_md(md_path: Path) -> Dict[str, str]:
    """
    Extract title, abstract, and experimental sections from a markdown file.
    """
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            md_content = f.read()
        
        title = extract_title(md_content)
        abstract = extract_abstract(md_content)
        experimental = extract_experimental_method(md_content)
        
        return {
            'source_file': md_path.stem,
            'md_path': str(md_path),
            'title': title,
            'abstract': abstract,
            'experimental': experimental
        }
    
    except Exception as e:
        logging.error(f"Error processing {md_path}: {e}")
        return {
            'source_file': md_path.stem,
            'title': '',
            'abstract': '',
            'experimental': '',
            'error': str(e)
        }


def process_pdfs_marker_output(base_dir: Path, output_json: Path):
    results = []
    
    # Iterate through subdirectories
    for subdir in base_dir.iterdir():
        if not subdir.is_dir():
            continue
        
        nested_subdir = subdir / subdir.name
        if not nested_subdir.exists():
            continue
        
        md_file = nested_subdir / f"{subdir.name}.md"
        if not md_file.exists():
            md_files = list(nested_subdir.glob('*.md'))
            if md_files:
                md_file = md_files[0]
            else:
                continue
        
        extracted = extract_sections_from_md(md_file)
        results.append(extracted)
    
    # Save results to JSON
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    logging.info(f"Saved {len(results)} extracted sections to: {output_json}")
    
    # Print statistics
    with_title = sum(1 for r in results if r.get('title'))
    with_abstract = sum(1 for r in results if r.get('abstract'))
    with_exp = sum(1 for r in results if len(r.get('experimental', '')) > 50) # Filter out trivial extracts
    
    logging.info(f"Statistics:")
    logging.info(f"  Total papers: {len(results)}")
    logging.info(f"  With title: {with_title} ({with_title/len(results)*100:.1f}%)")
    logging.info(f"  With abstract: {with_abstract} ({with_abstract/len(results)*100:.1f}%)")
    logging.info(f"  With experimental (>50 chars): {with_exp} ({with_exp/len(results)*100:.1f}%)")


def main():
    parser = argparse.ArgumentParser(description="Extract sections from marker-pdf MD files")
    parser.add_argument('--input_dir', type=Path, default=Path(r'd:\20251111_SEI_CRAWLING_WITH_HYU\20260105_전달내용_및_develope\pdfs_marker_output'), help='Input directory')
    parser.add_argument('--output', type=Path, default=Path(r'd:\20251111_SEI_CRAWLING_WITH_HYU\20260105_전달내용_및_develope\LLM_Classification\extracted_sections.json'), help='Output JSON path')
    
    args = parser.parse_args()
    
    if not args.input_dir.exists():
        logging.error(f"Input directory does not exist: {args.input_dir}")
        return
    
    args.output.parent.mkdir(parents=True, exist_ok=True)
    process_pdfs_marker_output(args.input_dir, args.output)

if __name__ == '__main__':
    main()
