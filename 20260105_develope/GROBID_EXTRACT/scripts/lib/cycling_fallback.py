# scripts/lib/cycling_fallback.py
"""
Cycling Extraction Fallback Module

Per 15_설계.md Section 7 (Day 2):
- If EXTRACT_CYCLING returns 0 measurements but cycling keywords are present
- Retry with Gemini Flash instead of local model (qwen)
- Add coverage flag to QC if still 0
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging
import re

logger = logging.getLogger(__name__)

# Cycling-related keywords to detect if paper likely has cycling data
CYCLING_KEYWORDS = [
    # English
    "cycle", "cycles", "cycling", "cycled",
    "mAh g", "mAh/g", "mAh cm", "mAh/cm",
    "A g-1", "A/g", "mA g-1", "mA/g",
    "mA cm-2", "mA/cm2", "mA cm", 
    "capacity retention", "coulombic efficiency",
    "stripping", "plating", "deposition",
    "symmetric cell", "full cell",
    "10000", "5000", "1000",  # Common cycle counts
    "hours", "h at",
    "stability", "lifetime",
]


def has_cycling_content(ctx: Dict[str, Any]) -> bool:
    """
    Check if paper context contains cycling-related keywords.
    
    Args:
        ctx: Paper context dict with chunks_main, chunks_supp, inventory
        
    Returns:
        True if cycling keywords found
    """
    # Collect all text
    text_parts = []
    
    # From chunks
    for ch in ctx.get("chunks_main", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    for ch in ctx.get("chunks_supp", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    
    # From figure captions
    inv = ctx.get("inventory", {})
    for fig in inv.get("figures", []):
        if isinstance(fig, dict):
            text_parts.append(fig.get("caption", ""))
    
    # Join and lowercase
    full_text = " ".join(text_parts).lower()
    
    # Count keyword matches
    matches = []
    for kw in CYCLING_KEYWORDS:
        if kw.lower() in full_text:
            matches.append(kw)
    
    # Require at least 3 different keywords for high confidence
    return len(matches) >= 3


def get_cycling_keywords_found(ctx: Dict[str, Any]) -> List[str]:
    """Get list of cycling keywords found in context."""
    text_parts = []
    for ch in ctx.get("chunks_main", []) + ctx.get("chunks_supp", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    
    inv = ctx.get("inventory", {})
    for fig in inv.get("figures", []):
        if isinstance(fig, dict):
            text_parts.append(fig.get("caption", ""))
    
    full_text = " ".join(text_parts).lower()
    
    return [kw for kw in CYCLING_KEYWORDS if kw.lower() in full_text]


def build_coverage_flag(
    paper_id: str,
    case_id: str,
    expected_metric: str,
    keywords_found: List[str]
) -> Dict[str, Any]:
    """
    Build a coverage flag entry for QC report.
    
    Per 15_설계.md Section 7.1:
    If cycling=0 even after fallback, flag as MISSING_EXPECTED_METRIC
    """
    return {
        "paper_id": paper_id,
        "case_id": case_id,
        "flag": "MISSING_EXPECTED_METRIC",
        "expected": expected_metric,
        "keywords_found": keywords_found[:10],  # Limit to 10
        "message": f"Paper contains {len(keywords_found)} cycling keywords but 0 measurements extracted"
    }


# Symmetric vs full cell keyword groups (per 15_설계.md Section 7.2)
SYMMETRIC_KEYWORDS = ["symmetric", "stripping", "plating", "mA cm-2", "mAh cm-2", "Zn||Zn"]
FULL_CELL_KEYWORDS = ["full cell", "mAh g-1", "A g-1", "cathode", "capacity retention"]


def classify_cycling_context(ctx: Dict[str, Any]) -> str:
    """
    Classify whether paper focuses on symmetric or full cell cycling.
    
    Returns:
        "SYMMETRIC", "FULL_CELL", or "BOTH"
    """
    text_parts = []
    for ch in ctx.get("chunks_main", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    
    full_text = " ".join(text_parts).lower()
    
    has_symmetric = any(kw.lower() in full_text for kw in SYMMETRIC_KEYWORDS)
    has_full_cell = any(kw.lower() in full_text for kw in FULL_CELL_KEYWORDS)
    
    if has_symmetric and has_full_cell:
        return "BOTH"
    elif has_symmetric:
        return "SYMMETRIC"
    elif has_full_cell:
        return "FULL_CELL"
    else:
        return "UNCLEAR"
