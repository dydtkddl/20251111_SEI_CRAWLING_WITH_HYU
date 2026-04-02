# scripts/lib/safety_guards.py
"""
Safety Guards for Pipeline Stability

Defensive type checking and guards to prevent runtime crashes.
Reference: 13_설계.md section 6
"""
from __future__ import annotations
from typing import Any, List, Dict
import logging

logger = logging.getLogger(__name__)


def ensure_dict_list(items: Any) -> List[Dict[str, Any]]:
    """
    Ensure a list contains only dicts (tuple crash protection).
    
    Per 13_설계.md: Stage5 crashes with 'tuple' object has no attribute 'get'
    when measurements list contains tuples instead of dicts.
    
    Args:
        items: List or other iterable
        
    Returns:
        List containing only dict items
    """
    if not isinstance(items, (list, tuple)):
        logger.warning(f"ensure_dict_list received non-list type: {type(items)}")
        return []
    
    out = []
    for i, x in enumerate(items):
        if isinstance(x, dict):
            out.append(x)
        elif isinstance(x, (tuple, list)):
            # Tuple/list instead of dict - this causes .get() crashes
            logger.warning(f"Filtered out non-dict at index {i}: {type(x)}")
        else:
            logger.warning(f"Filtered out unexpected type at index {i}: {type(x)}")
    
    return out


def safe_measurements_filter(measurements: List[Any]) -> List[Dict[str, Any]]:
    """
    Filter measurements to remove invalid entries.
    
    Removes:
    - Non-dict items (tuples, lists, None)
    - Dicts missing 'metric' key
    - Dicts with _llm_not_configured flag
    
    Args:
        measurements: Raw measurements list
        
    Returns:
        Filtered measurements list (all dicts)
    """
    safe = []
    
    for i, rm in enumerate(measurements or []):
        # Type check
        if not isinstance(rm, dict):
            logger.debug(f"Skipping non-dict measurement at index {i}: {type(rm)}")
            continue
        
        # Skip LLM not configured
        if rm.get("_llm_not_configured"):
            logger.debug(f"Skipping _llm_not_configured measurement at index {i}")
            continue
        
        # Must have metric
        if not rm.get("metric"):
            logger.warning(f"Skipping measurement without metric at index {i}")
            continue
        
        safe.append(rm)
    
    return safe


def build_digitize_task(meas: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a digitize task from a measurement with null value.
    
    Per 13_설계.md: null value measurements should be converted to digitize tasks.
    
    Args:
        meas: Measurement dict with null value
        
    Returns:
        Digitize task dict
    """
    ev = meas.get("evidence") or {}
    
    return {
        "task_type": "DIGITIZE_FIGURE",
        "paper_id": meas.get("paper_id"),
        "case_id": meas.get("case_id"),
        "metric": meas.get("metric"),
        "figure_id": ev.get("figure_id"),
        "caption": ev.get("caption"),
        "hint": ev.get("quote") or "digitize curve/point from figure",
        "conditions": meas.get("conditions", {}),
        "tags": meas.get("tags", {}),
        "unit": meas.get("unit"),
    }
