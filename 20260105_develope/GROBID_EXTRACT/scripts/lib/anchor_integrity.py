# scripts/lib/anchor_integrity.py
"""
Anchor Integrity Checker

Per 15_설계.md Section 8 (Day 2):
- Validate that evidence.figure_id/table_id actually exist in inventory
- If not, remove from evidence and tag as unresolved_ref
- Prevents broken links in UI and ensures data integrity
"""
from __future__ import annotations
from typing import Any, Dict, List, Set
import logging

logger = logging.getLogger(__name__)


def validate_anchor_integrity(
    meas: Dict[str, Any], 
    inventory: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Validate figure_id/table_id exist in inventory.
    
    Per 15_설계.md Section 8.1:
    - If figure_id not in inventory.figures, remove and tag
    - If table_id not in inventory.tables, remove and tag
    
    Args:
        meas: Measurement dict
        inventory: Paper inventory with figures/tables lists
        
    Returns:
        Measurement with validated/corrected anchors
    """
    meas = dict(meas)  # Don't mutate original
    evidence = meas.get("evidence")
    
    if evidence is None:
        return meas
        
    if not isinstance(evidence, dict):
        return meas
    
    evidence = dict(evidence)  # Copy
    meas["evidence"] = evidence
    
    tags = meas.get("tags")
    if tags is None:
        tags = {}
    else:
        tags = dict(tags)
    meas["tags"] = tags
    
    # Build sets of valid IDs
    valid_figures: Set[str] = set()
    valid_tables: Set[str] = set()
    
    for fig in inventory.get("figures", []) or []:
        if isinstance(fig, dict):
            fig_id = fig.get("figure_id") or fig.get("id")
            if fig_id:
                valid_figures.add(fig_id)
    
    for tbl in inventory.get("tables", []) or []:
        if isinstance(tbl, dict):
            tbl_id = tbl.get("table_id") or tbl.get("id")
            if tbl_id:
                valid_tables.add(tbl_id)
    
    unresolved_refs = []
    
    # Check figure_id
    fig_id = evidence.get("figure_id")
    if fig_id and fig_id not in valid_figures:
        unresolved_refs.append(f"Figure {fig_id}")
        evidence["figure_id"] = None
        logger.debug(f"Unresolved figure ref: {fig_id}")
    
    # Check table_id
    table_id = evidence.get("table_id")
    if table_id and table_id not in valid_tables:
        unresolved_refs.append(f"Table {table_id}")
        evidence["table_id"] = None
        logger.debug(f"Unresolved table ref: {table_id}")
    
    # Tag if we found unresolved refs
    if unresolved_refs:
        tags["unresolved_ref"] = True
        tags["unresolved_ref_text"] = "; ".join(unresolved_refs)
    
    return meas


def validate_measurements_anchors(
    measurements: List[Dict[str, Any]],
    inventory: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Validate anchors for all measurements.
    
    Args:
        measurements: List of measurement dicts
        inventory: Paper inventory
        
    Returns:
        List of measurements with validated anchors
    """
    return [
        validate_anchor_integrity(m, inventory) 
        if isinstance(m, dict) else m 
        for m in measurements
    ]


def count_unresolved_refs(measurements: List[Dict[str, Any]]) -> int:
    """Count how many measurements have unresolved references."""
    count = 0
    for m in measurements:
        if isinstance(m, dict):
            tags = m.get("tags", {}) or {}
            if tags.get("unresolved_ref"):
                count += 1
    return count
