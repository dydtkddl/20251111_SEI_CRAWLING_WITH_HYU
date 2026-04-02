# scripts/lib/case_builder_hybrid.py
# -*- coding: utf-8 -*-
"""
Hybrid Case Builder Module

Uses rule-based sample candidate mining + LLM for confirmation/grouping.
This is far more stable than pure LLM-based case generation.

Enterprise Design Principle:
- Rule-based systems generate CANDIDATES (high recall)
- LLM CONFIRMS and LABELS candidates (high precision)
- Never rely on LLM alone for structural decisions
"""

from __future__ import annotations
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Callable


# === Sample Name Patterns for Battery Domain ===
SAMPLE_PATTERNS = [
    # Bare/pristine zinc patterns
    r'\b(?:bare|pristine|pure)\s+[Zz]n\b',
    r'\b[Zz]n\s+(?:foil|plate|anode)\b',
    
    # Coated zinc patterns (X/Zn, X@Zn, X-Zn)
    r'\b[A-Z][A-Za-z0-9\-]{0,10}/[Zz]n\b',
    r'\b[A-Z][A-Za-z0-9\-]{0,10}@[Zz]n\b',
    r'\b[A-Z][A-Za-z0-9\-]{0,10}-[Zz]n\b',
    
    # Cell configurations (X||Y, X|Y)
    r'\b[A-Z][A-Za-z0-9/\-]{0,20}\|\|[A-Za-z0-9/\-]{1,20}\b',
    r'\b[A-Z][A-Za-z0-9/\-]{0,20}\|[A-Za-z0-9/\-]{1,20}\b',
    
    # Common coating materials
    r'\b(?:graphene|GO|rGO|CNT|carbon|PANI|PPy|ZIF|MOF)/[Zz]n\b',
    r'\bG/[Zz]n\b',  # G/Zn specifically
    
    # With electrolyte mentions
    r'\b[Zz]n(?:SO4|Cl2|Ac|TfO|TFSI)\b',
]

# Patterns to exclude (false positives)
EXCLUDE_PATTERNS = [
    r'\b[Zz]n\s*\d+',  # Zn2+, Zn3P2 etc (ions/compounds)
    r'\b[Zz]n[A-Z][a-z]',  # ZnO, ZnS etc (compounds)
]


def _normalize_sample_name(name: str) -> str:
    """Normalize sample name for consistent matching."""
    s = re.sub(r'\s+', ' ', name).strip()
    s = s.replace("pristine", "bare").replace("Pristine", "bare")
    s = s.replace("pure", "bare").replace("Pure", "bare") 
    return s


def _is_excluded(name: str) -> bool:
    """Check if sample name matches exclusion patterns."""
    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, name, flags=re.IGNORECASE):
            return True
    return False


def mine_sample_candidates(ctx: Dict[str, Any], topn: int = 30) -> List[str]:
    """
    Extract sample name candidates from context using regex patterns.
    
    Args:
        ctx: Pipeline context with chunks, captions, etc.
        topn: Maximum number of candidates to return
    
    Returns:
        List of sample name candidates, ordered by frequency
    """
    texts = []
    
    # Collect text from all context sources
    for key in ("primary_evidence", "linked_evidence", "chunks_main", "chunks_supp"):
        for item in ctx.get(key, []) or []:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content") or ""
                texts.append(t)
    
    # Also check captions
    for key in ("figure_captions", "table_captions"):
        for item in ctx.get(key, []) or []:
            if isinstance(item, dict):
                t = item.get("caption") or item.get("text") or ""
                texts.append(t)
    
    # Extract matches
    hits = []
    combined_text = " ".join(texts)
    
    for pat in SAMPLE_PATTERNS:
        for match in re.finditer(pat, combined_text, flags=re.IGNORECASE):
            name = match.group(0).strip()
            if not _is_excluded(name):
                hits.append(name)
    
    # Normalize and count
    norm = [_normalize_sample_name(h) for h in hits]
    counter = Counter(norm)
    
    return [name for name, _ in counter.most_common(topn)]


def _default_case_split(candidates: List[str]) -> List[Dict[str, Any]]:
    """
    Rule-based fallback case splitting when LLM is unavailable.
    Basic strategy: separate bare Zn vs coated samples.
    """
    cases = []
    
    bare_variants = []
    coated_variants = []
    
    for c in candidates:
        c_lower = c.lower()
        if "bare" in c_lower or c_lower in ("zn", "zn foil", "zn plate", "zn anode"):
            bare_variants.append(c)
        else:
            coated_variants.append(c)
    
    if bare_variants or not candidates:
        cases.append({
            "case_id": "CASE-001",
            "case_name": "Bare Zn",
            "aliases": bare_variants or ["bare Zn", "pristine Zn", "Zn foil"],
            "description": "Uncoated zinc electrode (control/reference)"
        })
    
    if coated_variants:
        # Try to identify the main coating
        main_coating = coated_variants[0] if coated_variants else "Coated Zn"
        cases.append({
            "case_id": "CASE-002", 
            "case_name": main_coating,
            "aliases": coated_variants,
            "description": "Coated zinc electrode (experimental)"
        })
    
    return cases if cases else [{
        "case_id": "CASE-001",
        "case_name": "Default",
        "aliases": candidates,
        "description": "Single experimental case"
    }]


async def build_cases_hybrid_async(
    llm_call_fn: Callable,
    ctx: Dict[str, Any],
    paper_id: str = "",
    max_retry: int = 2
) -> List[Dict[str, Any]]:
    """
    Async hybrid case builder using rule-based mining + LLM confirmation.
    
    Features:
    - Rule-based candidate mining for high recall
    - LLM for grouping/labeling (not generation)
    - Repair loop if LLM returns 0 cases
    - Minimum 2-case guarantee (bare vs coated)
    
    Args:
        llm_call_fn: Async function(prompt, schema) -> dict
        ctx: Pipeline context
        paper_id: Paper identifier for logging
        max_retry: Maximum retry attempts if cases=0
    
    Returns:
        List of case dicts with case_id, case_name, aliases, description
    """
    candidates = mine_sample_candidates(ctx)
    
    if not candidates:
        return _default_case_split([])
    
    for attempt in range(max_retry + 1):
        try:
            is_repair = attempt > 0
            
            prompt = {
                "task": "case_build",
                "paper_id": paper_id,
                "sample_candidates": candidates,
                "instructions": [
                    "Group these sample candidates into distinct experimental CASEs.",
                    "Typically: bare/pristine Zn = CASE-001 (control), coated Zn = CASE-002 (experimental).",
                    "Return structured case objects with case_id, case_name, aliases, description.",
                    "CRITICAL: You MUST return at least 2 cases. If unsure, create separate cases rather than merging.",
                    "If the samples appear homogeneous, still separate by bare vs modified."
                ] + ([
                    "REPAIR: Previous attempt returned 0 cases. This is invalid.",
                    "You MUST return at least 2 cases from the candidate list."
                ] if is_repair else [])
            }
            
            schema = {
                "type": "object",
                "properties": {
                    "cases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "case_id": {"type": "string"},
                                "case_name": {"type": "string"},
                                "aliases": {"type": "array", "items": {"type": "string"}},
                                "description": {"type": "string"}
                            },
                            "required": ["case_id", "case_name"]
                        },
                        "minItems": 1
                    }
                },
                "required": ["cases"]
            }
            
            result = await llm_call_fn(prompt, schema)
            cases = result.get("cases") or []
            
            if cases:
                # Ensure minimum 2 cases if we have diverse candidates
                if len(cases) == 1 and len(candidates) >= 2:
                    # Check if we can split better
                    existing_names = {a.lower() for c in cases for a in c.get("aliases", [])}
                    remaining = [c for c in candidates if c.lower() not in existing_names]
                    if remaining:
                        cases.append({
                            "case_id": "CASE-002",
                            "case_name": remaining[0],
                            "aliases": remaining,
                            "description": "Additional experimental case"
                        })
                return cases
            
            # If no cases, retry
            print(f"[CaseBuilderHybrid] Attempt {attempt + 1}: 0 cases, retrying...")
                
        except Exception as e:
            print(f"[CaseBuilderHybrid] LLM failed: {e}")
    
    # All retries exhausted, use rule-based
    print(f"[CaseBuilderHybrid] Using rule-based fallback after {max_retry + 1} attempts")
    return _default_case_split(candidates)


def build_cases_hybrid_sync(ctx: Dict[str, Any], paper_id: str = "") -> List[Dict[str, Any]]:
    """
    Synchronous hybrid case builder (rule-based only, no LLM).
    Use this when LLM is unavailable or for testing.
    
    Args:
        ctx: Pipeline context
        paper_id: Paper identifier
    
    Returns:
        List of case dicts
    """
    candidates = mine_sample_candidates(ctx)
    return _default_case_split(candidates)


def assign_case_to_measurement(
    meas: Dict[str, Any],
    cases: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Assign a measurement to the most appropriate case based on evidence text.
    
    Args:
        meas: Measurement dict
        cases: List of case dicts with aliases
    
    Returns:
        Measurement dict with case_id assigned
    """
    meas = dict(meas)
    
    # Get text to match against
    ev = meas.get("evidence", {}) or {}
    match_text = (ev.get("quote") or "") + " " + (ev.get("section_path") or "")
    match_text = match_text.lower()
    
    # Score each case
    best_case = cases[0]["case_id"] if cases else "CASE-001"
    best_score = 0
    
    for case in cases:
        score = 0
        for alias in case.get("aliases", []):
            if alias.lower() in match_text:
                score += 1
        
        if score > best_score:
            best_score = score
            best_case = case["case_id"]
    
    meas["case_id"] = best_case
    return meas
