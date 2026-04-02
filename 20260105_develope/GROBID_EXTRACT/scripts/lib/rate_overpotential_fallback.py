# scripts/lib/rate_overpotential_fallback.py
"""
Rate Performance and Overpotential Extraction Fallback Module

Per 19_설계.md Phase 4:
- If EXTRACT_RATE returns 0 measurements but rate keywords are present
- If EXTRACT_OVERPOTENTIAL returns 0 measurements but overpotential keywords are present
- Retry with expanded evidence (more chunks) 
- Add coverage flag to QC if still 0
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List
import logging

logger = logging.getLogger(__name__)

# Rate-related keywords to detect if paper likely has rate performance data
RATE_KEYWORDS = [
    # Specific capacity
    "mAh g", "mAh/g", "mAh g-1", "mAh g -1",
    "specific capacity", "capacity of",
    # Energy/Power density  
    "energy density", "Wh/kg", "Wh kg", "W h kg", "W h kg-1", "W h kg -1",
    "power density", "W/kg", "W kg", "W kg-1", "W kg -1",
    # Rate terms
    "rate performance", "rate capability", "C-rate", "C rate",
    "Ragone", "Ragone plot",
    # Current density units
    "A g-1", "A/g", "A g -1",
    # Specific values (from target paper)
    "265.8", "145.5", "246.9", "8675.9",
    "deliver a capacity", "could deliver", "remained",
]

# Overpotential-related keywords
OVERPOTENTIAL_KEYWORDS = [
    # Core terms
    "overpotential", "nucleation overpotential", "deposition overpotential",
    "η", "polarization",
    # Voltage terms
    "voltage hysteresis", "voltage gap", "voltage difference", "voltage plateau",
    # Values in mV
    "mV", "213 mV", "43 mV", "84 mV",
    # Context terms
    "nucleation", "nucleation sites", "initial deposition",
    "plating", "stripping", "stripping/plating",
    # Current density context
    "mA cm-2", "mA/cm2", "mA cm", 
    "at 2", "at 10", "current density of",
    # Rate-dependent terms
    "1 C", "2 C", "5 C", "10 C", "20 C",
]


def has_rate_content(ctx: Dict[str, Any]) -> bool:
    """
    Check if paper context contains rate performance keywords.
    
    Args:
        ctx: Paper context dict with chunks_main, chunks_supp, inventory
        
    Returns:
        True if rate performance keywords found (>=3 matches)
    """
    text_parts = []
    
    for ch in ctx.get("chunks_main", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    for ch in ctx.get("chunks_supp", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    
    inv = ctx.get("inventory", {})
    for fig in inv.get("figures", []):
        if isinstance(fig, dict):
            text_parts.append(fig.get("caption", ""))
    
    full_text = " ".join(text_parts).lower()
    
    matches = [kw for kw in RATE_KEYWORDS if kw.lower() in full_text]
    return len(matches) >= 3


def get_rate_keywords_found(ctx: Dict[str, Any]) -> List[str]:
    """Get list of rate keywords found in context."""
    text_parts = []
    for ch in ctx.get("chunks_main", []) + ctx.get("chunks_supp", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    
    inv = ctx.get("inventory", {})
    for fig in inv.get("figures", []):
        if isinstance(fig, dict):
            text_parts.append(fig.get("caption", ""))
    
    full_text = " ".join(text_parts).lower()
    
    return [kw for kw in RATE_KEYWORDS if kw.lower() in full_text]


def has_overpotential_content(ctx: Dict[str, Any]) -> bool:
    """
    Check if paper context contains overpotential keywords.
    
    Args:
        ctx: Paper context dict with chunks_main, chunks_supp, inventory
        
    Returns:
        True if overpotential keywords found (>=3 matches)
    """
    text_parts = []
    
    for ch in ctx.get("chunks_main", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    for ch in ctx.get("chunks_supp", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    
    inv = ctx.get("inventory", {})
    for fig in inv.get("figures", []):
        if isinstance(fig, dict):
            text_parts.append(fig.get("caption", ""))
    
    full_text = " ".join(text_parts).lower()
    
    matches = [kw for kw in OVERPOTENTIAL_KEYWORDS if kw.lower() in full_text]
    return len(matches) >= 3


def get_overpotential_keywords_found(ctx: Dict[str, Any]) -> List[str]:
    """Get list of overpotential keywords found in context."""
    text_parts = []
    for ch in ctx.get("chunks_main", []) + ctx.get("chunks_supp", []):
        if isinstance(ch, dict):
            text_parts.append(ch.get("text", ""))
    
    inv = ctx.get("inventory", {})
    for fig in inv.get("figures", []):
        if isinstance(fig, dict):
            text_parts.append(fig.get("caption", ""))
    
    full_text = " ".join(text_parts).lower()
    
    return [kw for kw in OVERPOTENTIAL_KEYWORDS if kw.lower() in full_text]
