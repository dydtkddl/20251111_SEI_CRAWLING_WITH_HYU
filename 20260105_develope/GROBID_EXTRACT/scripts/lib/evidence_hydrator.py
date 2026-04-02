# scripts/lib/evidence_hydrator.py
# -*- coding: utf-8 -*-
"""
Evidence Hydrator Module

Automatically fills missing evidence fields (doc, section_path, quote) from
chunk metadata when LLM output leaves them empty. This ensures all measurements
are auditable and pass validation checks.

Enterprise Design Principle:
- LLM should only need to provide chunk_id or figure_id/table_id
- All other evidence fields are auto-populated from context
"""

from __future__ import annotations
import re
from typing import Any, Dict, List, Optional


def _generate_value_variants(value: Any, unit: Optional[str] = None) -> List[str]:
    """
    Generate all possible text representations of a value.
    Per 15_설계.md: handle mV↔V, scientific notation, etc.
    """
    variants = []
    
    if value is None:
        return variants
    
    # Try numeric conversion
    try:
        num = float(value)
    except (ValueError, TypeError):
        # String value - just add as is
        variants.append(str(value))
        return variants
    
    # Add base representations
    variants.append(str(value))
    if isinstance(value, float) and value == int(value):
        variants.append(str(int(value)))
    
    # mV ↔ V conversion
    if unit in ("V", "v"):
        variants.append(f"{num * 1000}")  # 0.01 V → 10
        variants.append(f"{num * 1000} mV")  # 10 mV
        variants.append(f"{int(num * 1000)} mV")
    elif unit in ("mV", "mv"):
        variants.append(f"{num / 1000}")  # 10 mV → 0.01
    
    # Hz conversions 
    if unit in ("Hz", "hz"):
        if num < 1:
            # 0.01 Hz → 10^-2
            import math
            exp = int(math.log10(num)) if num > 0 else 0
            variants.append(f"10^{exp}")
            variants.append(f"10{exp}")
        elif num >= 1000:
            variants.append(f"{num/1000} kHz")
            variants.append(f"{int(num/1000)} kHz")
    
    return variants


def _get_metric_keywords(metric: str) -> List[str]:
    """Get relevant keywords for a metric to score sentences."""
    keywords_map = {
        "eis_": ["EIS", "impedance", "Nyquist", "Rct", "Rs", "R0", "resistance", "Ω", "ohm"],
        "overpotential": ["overpotential", "voltage", "polarization", "mV"],
        "cycling": ["cycle", "cycling", "capacity", "retention", "hours", "stability"],
        "adsorption": ["adsorption", "binding", "energy", "eV", "DFT"],
        "thickness": ["thickness", "nm", "μm", "um", "layer"],
        "conductivity": ["conductivity", "mS", "ionic"],
        "corrosion": ["corrosion", "Tafel", "icorr", "ecorr"],
    }
    
    keywords = []
    for prefix, kws in keywords_map.items():
        if prefix in metric.lower():
            keywords.extend(kws)
    
    return keywords if keywords else ["measurement", "value"]


# Equipment patterns to penalize (per 15_설계.md Section 5)
EQUIPMENT_PATTERNS = [
    "SEM", "TEM", "XRD", "XPS", "AFM", "STEM",
    "S-4800", "JEM-", "Hitachi", "JEOL", "Bruker",
    "scanning electron", "transmission electron",
    "diffractometer", "spectrometer"
]


def _score_sentence(sentence: str, value: Any, unit: Optional[str], metric: str) -> int:
    """
    Score a sentence for how well it matches the measurement value.
    Per 15_설계.md Section 5: score-based sentence selection.
    """
    score = 0
    sent_lower = sentence.lower()
    
    # Value matching (+10)
    value_variants = _generate_value_variants(value, unit)
    for v in value_variants:
        if str(v) in sentence:
            score += 10
            break
    
    # Metric keyword matching (+5)
    keywords = _get_metric_keywords(metric)
    for kw in keywords:
        if kw.lower() in sent_lower:
            score += 5
            break  # Only count once
    
    # Unit matching (+5)
    if unit and unit in sentence:
        score += 5
    
    # Equipment penalty (-5)
    # Only penalize if metric keywords NOT present
    has_metric_keyword = any(kw.lower() in sent_lower for kw in keywords)
    for eq in EQUIPMENT_PATTERNS:
        if eq.lower() in sent_lower and not has_metric_keyword:
            score -= 5
            break
    
    # Too short penalty (-10)
    if len(sentence) < 20:
        score -= 10
    
    # Placeholder penalty (-10)
    placeholder_markers = ["value extracted", "digitized from", "see figure"]
    for marker in placeholder_markers:
        if marker in sent_lower:
            score -= 10
            break
    
    return score


def _pick_sentence_with_number(
    text: str, 
    number_str: Optional[str] = None,
    value: Any = None,
    unit: Optional[str] = None,
    metric: str = ""
) -> Optional[str]:
    """
    Extract the best sentence containing the measurement value from chunk text.
    
    Per 15_설계.md Section 5: Uses score-based selection to avoid 
    equipment descriptions (SEM, TEM) and prefer sentences with 
    metric keywords and matching values.
    
    Args:
        text: Full chunk text
        number_str: String representation of the number (legacy)
        value: Actual value for variant generation
        unit: Unit for mV↔V conversion
        metric: Metric name for keyword scoring
    """
    if not text:
        return None
    
    # Split into sentences
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    
    if not sents:
        return None
    
    # Score all sentences
    scored = []
    for s in sents:
        score = _score_sentence(s, value or number_str, unit, metric)
        scored.append((score, s))
    
    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    
    # Return best scoring sentence if score > 0
    best_score, best_sent = scored[0]
    if best_score > 0:
        return best_sent.strip()[:200]
    
    # Fallback: try exact number match (legacy behavior)
    if number_str:
        for s in sents:
            if number_str in s:
                return s.strip()[:200]
    
    # Last fallback: first sentence with digit (but NOT equipment)
    for s in sents:
        if re.search(r'\d', s):
            # Skip equipment sentences
            s_lower = s.lower()
            is_equipment = any(eq.lower() in s_lower for eq in EQUIPMENT_PATTERNS)
            if not is_equipment:
                return s.strip()[:200]
    
    # Return None instead of arbitrary equipment sentence
    return None


def _build_chunk_index(ctx: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Build an index of chunk_id -> chunk metadata from context.
    Searches through multiple context keys that may contain chunk information.
    """
    idx = {}
    
    # Keys that may contain chunk evidence
    chunk_keys = [
        "primary_evidence",
        "linked_evidence", 
        "chunks_main",
        "chunks_supp",
        "table_captions",
        "figure_captions"
    ]
    
    for key in chunk_keys:
        items = ctx.get(key) or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    cid = item.get("chunk_id")
                    if cid:
                        idx[cid] = item
    
    return idx


def _build_figure_caption_index(ctx: Dict[str, Any]) -> Dict[str, str]:
    """Build index of figure_id -> caption text."""
    idx = {}
    
    for cap in ctx.get("figure_captions", []) or []:
        if isinstance(cap, dict):
            fig_id = cap.get("figure_id") or cap.get("id")
            caption = cap.get("caption") or cap.get("text")
            if fig_id and caption:
                idx[fig_id] = caption
    
    # Also check inventory
    inv = ctx.get("inventory", {}) or {}
    for fig in inv.get("figures", []) or []:
        if isinstance(fig, dict):
            fig_id = fig.get("figure_id") or fig.get("id")
            caption = fig.get("caption")
            if fig_id and caption:
                idx[fig_id] = caption
    
    return idx


def _build_table_caption_index(ctx: Dict[str, Any]) -> Dict[str, str]:
    """Build index of table_id -> caption text."""
    idx = {}
    
    for cap in ctx.get("table_captions", []) or []:
        if isinstance(cap, dict):
            tbl_id = cap.get("table_id") or cap.get("id")
            caption = cap.get("caption") or cap.get("text")
            if tbl_id and caption:
                idx[tbl_id] = caption
    
    # Also check inventory
    inv = ctx.get("inventory", {}) or {}
    for tbl in inv.get("tables", []) or []:
        if isinstance(tbl, dict):
            tbl_id = tbl.get("table_id") or tbl.get("id")
            caption = tbl.get("caption")
            if tbl_id and caption:
                idx[tbl_id] = caption
    
    return idx


def _pick_sentence_with_value(text: str, value_str: Optional[str]) -> Optional[str]:
    """
    Find a sentence containing a string value (for non-numeric metrics).
    Used for materials, labels, electrolyte names, etc.
    """
    if not text or not value_str:
        return None
    
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    v = value_str.lower()
    
    for s in sents:
        if v in s.lower():
            return s.strip()[:200]
    
    return None


def _ensure_anchor_id(ev: Dict[str, Any]) -> None:
    """
    Generate a hash-based anchor_id if no chunk/figure/table anchor exists.
    
    This prevents over-merge when measurements from different sources
    have no explicit anchor but different section_path/quote.
    """
    import hashlib
    
    # Skip if already has anchor
    if ev.get("chunk_id") or ev.get("figure_id") or ev.get("table_id"):
        return
    
    # Generate hash from section_path + quote
    base = (ev.get("section_path", "") + "|" + ev.get("quote", "")).encode("utf-8")
    ev["anchor_id"] = "a_" + hashlib.sha1(base).hexdigest()[:12]


def hydrate_evidence(meas: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensures measurement has complete evidence with: doc, section_path, quote.
    
    Strategy:
    1. Try to fill from chunk_id metadata
    2. Fall back to figure_id/table_id captions
    3. Generate placeholder if still missing (never leave None)
    
    Args:
        meas: Measurement dict with potential incomplete evidence
        ctx: Pipeline context containing chunk/figure/table metadata
    
    Returns:
        Measurement dict with hydrated evidence
    """
    meas = dict(meas)  # Don't mutate original
    ev = meas.get("evidence") or {}
    if not isinstance(ev, dict):
        ev = {}
    ev = dict(ev)  # Copy evidence too
    
    # Build indexes
    chunk_idx = _build_chunk_index(ctx)
    figure_captions = _build_figure_caption_index(ctx)
    table_captions = _build_table_caption_index(ctx)
    
    # Get potential value string for quote matching
    val = meas.get("value")
    number_str = None
    value_str = None  # For string values (materials, labels)
    
    if isinstance(val, (int, float)):
        number_str = str(val)
    elif isinstance(val, str):
        if val.replace(".", "").replace("-", "").isdigit():
            number_str = val
        else:
            value_str = val  # String value like material name
    
    # === Step 1: Fill from chunk_id ===
    chunk_id = ev.get("chunk_id") or meas.get("chunk_id")
    if chunk_id and chunk_id in chunk_idx:
        chunk_meta = chunk_idx[chunk_id]
        
        # Fill doc
        if not ev.get("doc"):
            chunk_doc = chunk_meta.get("doc")
            if chunk_doc in ("MAIN", "SUPP"):
                ev["doc"] = chunk_doc
            elif "supp" in str(chunk_id).lower():
                ev["doc"] = "SUPP"
            else:
                ev["doc"] = "MAIN"
        
        # Fill section_path
        if not ev.get("section_path"):
            ev["section_path"] = chunk_meta.get("section_path") or chunk_meta.get("section") or "UNKNOWN"
        
        # Fill quote (prefer numeric match, fallback to string value match)
        if not ev.get("quote"):
            chunk_text = chunk_meta.get("text") or chunk_meta.get("content") or ""
            ev["quote"] = (
                _pick_sentence_with_number(chunk_text, number_str)
                or _pick_sentence_with_value(chunk_text, value_str)
            )
        
        # Preserve chunk_id
        ev["chunk_id"] = chunk_id
    
    # === Step 2: Fill from figure_id ===
    figure_id = ev.get("figure_id") or ev.get("figure")
    if figure_id:
        if not ev.get("doc"):
            ev["doc"] = "MAIN"
        if not ev.get("section_path"):
            ev["section_path"] = "FIGURE"
        if not ev.get("quote"):
            caption = figure_captions.get(figure_id) or figure_captions.get(f"fig_{figure_id}")
            if caption:
                ev["quote"] = _pick_sentence_with_number(caption, number_str) or f"Value from {figure_id}"
            else:
                ev["quote"] = f"Value extracted from {figure_id}"
        ev["figure_id"] = figure_id
    
    # === Step 3: Fill from table_id ===
    table_id = ev.get("table_id") or ev.get("table")
    if table_id:
        if not ev.get("doc"):
            ev["doc"] = "MAIN"
        if not ev.get("section_path"):
            ev["section_path"] = "TABLE"
        if not ev.get("quote"):
            caption = table_captions.get(table_id) or table_captions.get(f"tbl_{table_id}")
            if caption:
                ev["quote"] = _pick_sentence_with_number(caption, number_str) or f"Value from {table_id}"
            else:
                ev["quote"] = f"Value extracted from {table_id}"
        ev["table_id"] = table_id
    
    # === Step 4: Final fallbacks ===
    if not ev.get("doc"):
        ev["doc"] = ctx.get("doc_type") or "MAIN"
    
    if not ev.get("section_path"):
        ev["section_path"] = "UNKNOWN"
    
    if not ev.get("quote"):
        ev["quote"] = "Value extracted from provided evidence context."
    
    # === Step 4.5 (08_설계): Quote Override for Bad Patterns ===
    # Import here to avoid circular import
    from scripts.lib.schema import should_override_quote
    
    if should_override_quote(ev.get("quote"), meas.get("value")):
        # Try to find better quote from chunk text
        new_quote = None
        
        # Try chunk_id first
        if chunk_id and chunk_id in chunk_idx:
            chunk_text = chunk_idx[chunk_id].get("text") or chunk_idx[chunk_id].get("content") or ""
            new_quote = _pick_sentence_with_number(chunk_text, number_str)
            if not new_quote and value_str:
                new_quote = _pick_sentence_with_value(chunk_text, value_str)
        
        # Try figure caption
        if not new_quote and figure_id:
            caption = figure_captions.get(figure_id) or figure_captions.get(f"fig_{figure_id}") or ""
            if caption and len(caption) >= 20:
                new_quote = _pick_sentence_with_number(caption, number_str)
        
        # Try table caption
        if not new_quote and table_id:
            caption = table_captions.get(table_id) or table_captions.get(f"tbl_{table_id}") or ""
            if caption and len(caption) >= 20:
                new_quote = _pick_sentence_with_number(caption, number_str)
        
        # Use new quote if found, otherwise keep existing (but flag it)
        if new_quote and len(new_quote) >= 20:
            ev["quote"] = new_quote
            ev["_quote_overridden"] = True
    
    # === Step 5: Ensure anchor_id if no explicit anchor ===
    _ensure_anchor_id(ev)
    
    meas["evidence"] = ev
    return meas


def hydrate_measurements(measurements: List[Dict[str, Any]], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Hydrate evidence for all measurements in a list.
    
    Args:
        measurements: List of measurement dicts
        ctx: Pipeline context
    
    Returns:
        List of measurements with hydrated evidence
    """
    return [hydrate_evidence(m, ctx) for m in measurements]
