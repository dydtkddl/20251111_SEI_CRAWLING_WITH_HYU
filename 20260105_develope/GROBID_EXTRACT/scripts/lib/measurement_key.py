# scripts/lib/measurement_key.py
# -*- coding: utf-8 -*-
"""
Measurement Key Module

Provides robust measurement grouping/deduplication keys that preserve
data richness by including conditions and tags, not just (case_id, metric).

Enterprise Design Principle:
- Same metric with different conditions/tags = DIFFERENT measurements
- Never lose data by over-aggressive deduplication
"""

from __future__ import annotations
import json
import math
from typing import Any, Dict, FrozenSet, Tuple


def _canonicalize(v: Any) -> Any:
    """
    Canonicalize a value for consistent hashing/comparison.
    Handles floats, lists, dicts, and None values.
    """
    if v is None:
        return None
    
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 6)
    
    if isinstance(v, (list, tuple)):
        return tuple(_canonicalize(x) for x in v)
    
    if isinstance(v, dict):
        return tuple(sorted((k, _canonicalize(v2)) for k, v2 in v.items()))
    
    return v


def _dict_to_frozen(d: Dict[str, Any]) -> FrozenSet[Tuple[str, Any]]:
    """Convert dict to frozenset of (key, canonicalized_value) tuples."""
    if not d:
        return frozenset()
    return frozenset((k, _canonicalize(v)) for k, v in d.items() if v is not None)


def measurement_group_key(m: Dict[str, Any]) -> str:
    """
    Generate a unique grouping key for a measurement that preserves data richness.
    
    Key components:
    - case_id: Experimental case identifier
    - metric: Metric name (canonical)
    - unit: Unit string
    - conditions: Experimental conditions (current_density, temperature, etc.)
    - tags: Semantic tags (before_after, ionic_conductivity_scope, etc.)
    - evidence_anchor: Primary evidence source (chunk_id, figure_id, or table_id)
    
    Returns:
        JSON string key for grouping/deduplication
    """
    case_id = m.get("case_id") or "CASE-UNKNOWN"
    metric = m.get("metric") or "metric-UNKNOWN"
    unit = m.get("unit") or ""
    
    conditions = _canonicalize(m.get("conditions") or {})
    tags = _canonicalize(m.get("tags") or {})
    
    # Evidence anchor for distinguishing same-value different-source
    evidence = m.get("evidence", {}) or {}
    anchor = (
        evidence.get("chunk_id") 
        or evidence.get("figure_id") 
        or evidence.get("table_id") 
        or evidence.get("anchor_id")  # Fallback hash-based anchor
        or ""
    )
    
    # Phase 6: Explicit fields for EIS dedup fix
    # These prevent 15Ω (Rs) and 22Ω (Rs before vs after polarization) from merging
    # NOTE: Use original dict, not the canonicalized tuple
    tags_dict = m.get("tags") or {}
    before_after = tags_dict.get("before_after") if isinstance(tags_dict, dict) else None
    eis_metric_type = tags_dict.get("eis_metric_type") if isinstance(tags_dict, dict) else None
    
    payload = {
        "case_id": case_id,
        "metric": metric,
        "unit": unit,
        "conditions": conditions,
        "tags": tags,
        "before_after": before_after,  # Phase 6: Explicit separation
        "eis_type": eis_metric_type,   # Phase 6: Explicit separation
        "anchor": anchor,
    }
    
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def measurement_dedup_key(m: Dict[str, Any]) -> str:
    """
    Generate a deduplication key for exact-match detection.
    Includes value to catch true duplicates.
    
    Returns:
        JSON string key for exact deduplication
    """
    base_key = measurement_group_key(m)
    base = json.loads(base_key)
    
    # Add value for exact dedup
    val = m.get("value")
    if isinstance(val, float):
        val = round(val, 6)
    base["value"] = val
    
    return json.dumps(base, sort_keys=True, ensure_ascii=False)


def organize_measurements_v2(measurements: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """
    Organize measurements with smart grouping that preserves data richness.
    
    Strategy:
    1. Group by (case_id, metric, conditions, tags, anchor)
    2. Within group: keep highest confidence, accumulate supporting evidence
    3. Never lose unique condition/tag combinations
    
    Args:
        measurements: List of raw measurements
    
    Returns:
        List of organized measurements with preserved richness
    """
    # First pass: exact deduplication
    seen_exact = {}
    unique = []
    for m in measurements:
        if m.get("_llm_not_configured"):
            continue
        
        dedup_key = measurement_dedup_key(m)
        if dedup_key in seen_exact:
            continue
        seen_exact[dedup_key] = True
        unique.append(m)
    
    # Second pass: group by semantic key
    groups: Dict[str, list[Dict[str, Any]]] = {}
    for m in unique:
        group_key = measurement_group_key(m)
        groups.setdefault(group_key, []).append(m)
    
    # Third pass: select best from each group
    result = []
    for group_key, items in groups.items():
        # Sort by confidence (descending)
        items = sorted(items, key=lambda x: float(x.get("confidence") or 0), reverse=True)
        best = dict(items[0])
        
        # Accumulate supporting evidence from other items
        if len(items) > 1:
            support = []
            for it in items[1:]:
                ev = it.get("evidence")
                if ev and isinstance(ev, dict):
                    support.append({
                        "extractor_id": it.get("extractor_id"),
                        "confidence": it.get("confidence"),
                        "evidence": ev
                    })
            if support:
                best["evidence_support"] = support
        
        # P1-3: Detect value conflicts within group and preserve alternatives
        vals = []
        for it in items:
            v = _canonicalize(it.get("value"))
            if v is not None:
                vals.append(v)
        
        unique_vals = sorted(set(str(v) for v in vals))
        if len(unique_vals) >= 2:
            # Multiple distinct values in same group = conflict
            best["value_alternatives"] = [
                {
                    "value": it.get("value"),
                    "confidence": it.get("confidence"),
                    "extractor_id": it.get("extractor_id"),
                }
                for it in items[:5]  # Limit to top 5
            ]
            best.setdefault("qc_flags", []).append("CONFLICT_VALUES_SAME_GROUP")
        
        # C3: Merge material_ids from all items in group
        material_ids = set()
        for it in items:
            mid = it.get("conditions", {}).get("material_id")
            if mid:
                material_ids.add(mid)
        
        if material_ids:
            if len(material_ids) == 1:
                best.setdefault("conditions", {})["material_id"] = list(material_ids)[0]
            else:
                # Multiple material_ids found - store as list
                best.setdefault("conditions", {})["material_ids"] = sorted(material_ids)
                best.setdefault("conditions", {})["material_id"] = sorted(material_ids)[0]
        
        # C3: Merge figure_ids from all items
        figure_ids = set()
        for it in items:
            fid = it.get("evidence", {}).get("figure_id")
            if fid:
                figure_ids.add(fid)
        
        if len(figure_ids) > 1:
            best.setdefault("evidence", {})["figure_ids"] = sorted(figure_ids)
        
        result.append(best)
    
    return result
