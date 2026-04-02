# scripts/lib/context_extender.py
"""
Context Extender Module v5.0

Extends measurement records with:
- context_paragraph: surrounding sentences
- figure_context: figure caption text
- comparison_group: TREATED/CONTROL pairing
- related_measurements: linked metrics
- experiment_context: purpose and conclusions
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple


# ============================================================================
# SENTENCE UTILITIES
# ============================================================================

def split_into_sentences(text: str) -> List[str]:
    """Split text into sentences, handling scientific notation."""
    # Protect common abbreviations and numbers
    text = re.sub(r'(\d+)\. ', r'\1<DOT> ', text)
    text = re.sub(r'(Fig|Table|Eq|et al|vs|i\.e|e\.g)\. ', r'\1<DOT> ', text, flags=re.IGNORECASE)
    
    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    # Restore dots
    sentences = [s.replace('<DOT>', '.') for s in sentences]
    return [s.strip() for s in sentences if s.strip()]


def find_sentence_containing(sentences: List[str], quote: str, threshold: int = 30) -> int:
    """Find index of sentence containing the quote (or closest match)."""
    quote_start = quote[:threshold].lower() if len(quote) > threshold else quote.lower()
    
    for i, sent in enumerate(sentences):
        if quote_start in sent.lower():
            return i
    
    # Fallback: find by overlap
    best_idx, best_overlap = 0, 0
    for i, sent in enumerate(sentences):
        overlap = len(set(quote.lower().split()) & set(sent.lower().split()))
        if overlap > best_overlap:
            best_idx, best_overlap = i, overlap
    
    return best_idx


# ============================================================================
# CONTEXT PARAGRAPH EXPANSION
# ============================================================================

def expand_context_paragraph(
    chunk_id: str,
    quote: str,
    chunks: Dict[str, Dict],
    max_chars: int = 500
) -> str:
    """
    Expand quote to include surrounding sentences.
    
    Args:
        chunk_id: Chunk ID like "C-M-00005"
        quote: Original quote text
        chunks: Dictionary of all chunks {chunk_id: {text, ...}}
        max_chars: Maximum characters for context
        
    Returns:
        Expanded context paragraph (up to max_chars)
    """
    chunk = chunks.get(chunk_id, {})
    text = chunk.get("text", "")
    
    if not text:
        return quote
    
    sentences = split_into_sentences(text)
    if not sentences:
        return quote
    
    quote_idx = find_sentence_containing(sentences, quote)
    
    # Include 1 sentence before and 1-2 after
    start_idx = max(0, quote_idx - 1)
    end_idx = min(len(sentences), quote_idx + 3)
    
    context = " ".join(sentences[start_idx:end_idx])
    
    # Truncate if too long
    if len(context) > max_chars:
        context = context[:max_chars - 3] + "..."
    
    return context


# ============================================================================
# FIGURE CONTEXT LINKING
# ============================================================================

def extract_subfigure(figure_id: str) -> Optional[str]:
    """Extract subfigure letter from figure_id (e.g., 'fig_3a' -> 'a')."""
    match = re.search(r'fig[_]?\d+([a-z])', figure_id, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return None


def link_figure_context(
    figure_id: str,
    paper_figures: Dict[str, Dict]
) -> Dict[str, Any]:
    """
    Link figure ID to caption text.
    
    Args:
        figure_id: Figure ID like "fig_3" or "fig_3a"
        paper_figures: Dictionary of figures {figure_id: {caption, ...}}
        
    Returns:
        Figure context dict with caption
    """
    if not figure_id:
        return {}
    
    # Try exact match first
    fig = paper_figures.get(figure_id, {})
    
    # Try base figure (fig_3a -> fig_3)
    if not fig:
        base_id = re.sub(r'[a-z]$', '', figure_id, flags=re.IGNORECASE)
        fig = paper_figures.get(base_id, {})
    
    return {
        "figure_id": figure_id,
        "caption": fig.get("caption", ""),
        "subfigure": extract_subfigure(figure_id)
    }


# ============================================================================
# COMPARISON GROUP DETECTION
# ============================================================================

SAMPLE_TYPE_ROLES = {
    "COATED": "TREATED",
    "BARE_ZN": "CONTROL",
    "CONTROL": "CONTROL",
    "REFERENCE": "REFERENCE",
}


def detect_comparison_groups(measurements: List[Dict]) -> List[Dict]:
    """
    Detect and link comparison groups (TREATED vs CONTROL).
    
    Groups measurements by (metric, chunk_id) and assigns roles.
    """
    # Group by metric + evidence location
    groups: Dict[Tuple, List[Dict]] = {}
    
    for m in measurements:
        metric = m.get("metric", "")
        chunk_id = m.get("evidence", {}).get("chunk_id", "")
        quote_prefix = (m.get("evidence", {}).get("quote", "") or "")[:50]
        
        key = (metric, chunk_id, quote_prefix)
        groups.setdefault(key, []).append(m)
    
    # Process groups with 2+ members
    for key, items in groups.items():
        if len(items) >= 2:
            _assign_comparison_roles(items)
    
    return measurements


def _assign_comparison_roles(items: List[Dict]) -> None:
    """Assign comparison roles to a group of measurements."""
    group_id = f"CMP-{items[0].get('metric', 'UNK')[:3].upper()}-{id(items) % 1000:03d}"
    
    for m in items:
        sample_type = m.get("tags", {}).get("sample_type", "")
        role = SAMPLE_TYPE_ROLES.get(sample_type, "UNCLEAR")
        
        # Build paired_with list - ONLY DIFFERENT sample_types
        paired_with = []
        for other in items:
            if other is m:
                continue
            
            other_sample = other.get("tags", {}).get("sample_type", "")
            
            # P4 FIX: Skip if same sample_type (avoid 0% comparisons)
            if sample_type == other_sample:
                continue
            
            # P4 FIX: Only pair COATED with BARE_ZN (or vice versa)
            valid_pair = (
                (sample_type in ("COATED", "") and other_sample in ("BARE_ZN", "CONTROL")) or
                (sample_type in ("BARE_ZN", "CONTROL") and other_sample in ("COATED", ""))
            )
            
            if not valid_pair and sample_type and other_sample:
                continue
            
            other_role = SAMPLE_TYPE_ROLES.get(other_sample, "UNCLEAR")
            paired_with.append({
                "measurement_ref": f"{other.get('metric')}@{other.get('value')}",
                "role": other_role,
                "material_id": other.get("conditions", {}).get("material_id", ""),
                "value": other.get("value"),
                "relationship": _infer_relationship(sample_type, other_sample)
            })
        
        # Only add comparison_group if there are valid pairs
        if paired_with:
            m["comparison_group"] = {
                "group_id": group_id,
                "role": role,
                "paired_with": paired_with
            }


def _infer_relationship(sample1: str, sample2: str) -> str:
    """Infer relationship between two sample types."""
    if {sample1, sample2} == {"COATED", "BARE_ZN"}:
        return "COATED_VS_BARE"
    elif "BEFORE" in sample1 or "AFTER" in sample1:
        return "BEFORE_VS_AFTER"
    else:
        return "DIFFERENT_SAMPLES"


# ============================================================================
# RELATED MEASUREMENTS LINKING
# ============================================================================

# Metric relationship rules
RELATIONSHIP_RULES = {
    # (metric1, metric2): relationship_type
    ("eis_Rct_Ohm", "ion_diffusion_coeff_cm2_s"): "SAME_EXPERIMENT",
    ("eis_Rs_Ohm", "eis_Rct_Ohm"): "SAME_EXPERIMENT",
    ("eis_Rct_Ohm", "specific_capacity_mAh_g"): "CAUSAL",
    ("overpotential_mV", "cycle_life_hours"): "CAUSAL",
    ("transference_number", "coulombic_efficiency_pct"): "CAUSAL",
    ("nucleation_overpotential_mV", "deposition_overpotential_mV"): "SAME_EXPERIMENT",
    ("corrosion_current_density_uAcm2", "corrosion_potential_V"): "SAME_EXPERIMENT",
    ("contact_angle_deg", "cycle_life_hours"): "CAUSAL",
    ("protective_layer_thickness_nm", "eis_Rct_Ohm"): "CAUSAL",
}


def link_related_measurements(measurements: List[Dict]) -> List[Dict]:
    """
    Link related measurements based on material_id and metric relationships.
    """
    # Group by material_id
    by_material: Dict[str, List[Dict]] = {}
    for m in measurements:
        mid = m.get("conditions", {}).get("material_id", "") or ""
        by_material.setdefault(mid, []).append(m)
    
    # Find related pairs
    for mid, items in by_material.items():
        if not mid:  # Skip measurements without material_id
            continue
            
        for m in items:
            related = []
            m_metric = m.get("metric", "")
            
            for other in items:
                if other is m:
                    continue
                    
                other_metric = other.get("metric", "")
                
                # Check both directions
                rel = RELATIONSHIP_RULES.get((m_metric, other_metric))
                if not rel:
                    rel = RELATIONSHIP_RULES.get((other_metric, m_metric))
                
                if rel:
                    # Check if same evidence
                    same_evidence = (
                        m.get("evidence", {}).get("chunk_id") == 
                        other.get("evidence", {}).get("chunk_id")
                    )
                    
                    related.append({
                        "metric": other_metric,
                        "material_id": mid,
                        "value": other.get("value"),
                        "relationship": rel,
                        "evidence_overlap": same_evidence
                    })
            
            if related:
                m["related_measurements"] = related
    
    return measurements


# ============================================================================
# MAIN EXTENSION FUNCTION
# ============================================================================

def extend_measurement_context(
    measurements: List[Dict],
    chunks: Dict[str, Dict],
    figures: Dict[str, Dict]
) -> List[Dict]:
    """
    Main function to extend all measurements with context fields.
    
    Args:
        measurements: List of measurement dicts
        chunks: Paper chunks {chunk_id: {text, ...}}
        figures: Paper figures {figure_id: {caption, ...}}
        
    Returns:
        Extended measurements with new context fields
    """
    for m in measurements:
        evidence = m.get("evidence", {})
        
        # 1. Expand context_paragraph
        chunk_id = evidence.get("chunk_id", "")
        quote = evidence.get("quote", "")
        if chunk_id and quote:
            m["context_paragraph"] = expand_context_paragraph(
                chunk_id, quote, chunks
            )
        
        # 2. Link figure_context
        figure_id = evidence.get("figure_id", "")
        if figure_id:
            m["figure_context"] = link_figure_context(figure_id, figures)
    
    # 3. Detect comparison groups
    measurements = detect_comparison_groups(measurements)
    
    # 4. Link related measurements
    measurements = link_related_measurements(measurements)
    
    return measurements
