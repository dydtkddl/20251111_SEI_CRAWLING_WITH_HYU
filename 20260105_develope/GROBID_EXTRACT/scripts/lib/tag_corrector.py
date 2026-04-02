# scripts/lib/tag_corrector.py
"""
Tag Auto-Correction Module

Per 15_설계.md Section 3 (Day 0 hotfix):
- Auto-fill missing eis_metric_type from eis_param
- Auto-fill missing zn_adsorption_source from source_type
- Set missing before_after to "UNCLEAR"
"""
from __future__ import annotations
from typing import Any, Dict
import logging

logger = logging.getLogger(__name__)


def auto_correct_tags(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Auto-correct missing or invalid tags based on metric type and available info.
    
    Per 15_설계.md Section 3:
    - eis_metric_type null → derive from eis_param or set UNCLEAR
    - zn_adsorption_source null → derive from source_type or set UNCLEAR
    - before_after null → set UNCLEAR
    
    Args:
        m: Measurement dict
        
    Returns:
        Measurement dict with corrected tags
    """
    m = dict(m)  # Don't mutate original
    metric = m.get("metric", "")
    tags = m.get("tags")
    
    if tags is None:
        tags = {}
        m["tags"] = tags
    elif not isinstance(tags, dict):
        tags = {}
        m["tags"] = tags
    else:
        tags = dict(tags)  # Copy
        m["tags"] = tags
    
    # === EIS Tag Corrections ===
    if metric.startswith("eis_"):
        # Derive eis_metric_type from eis_param if missing
        if tags.get("eis_metric_type") in (None, "", "null"):
            eis_param = tags.get("eis_param")
            if eis_param in ("Rct", "Rs", "Rsei", "R0"):
                tags["eis_metric_type"] = eis_param
            else:
                # Infer from metric name as fallback
                if "Rs_" in metric or metric.endswith("Rs"):
                    tags["eis_metric_type"] = "Rs"
                elif "Rct_" in metric or metric.endswith("Rct"):
                    tags["eis_metric_type"] = "Rct"
                elif "Rsei_" in metric:
                    tags["eis_metric_type"] = "Rsei"
                else:
                    tags["eis_metric_type"] = "UNCLEAR"
    
    # === Ionic Conductivity Scope Corrections ===
    if "ionic_conductivity" in metric or "ion_conductivity" in metric:
        if tags.get("ionic_conductivity_scope") in (None, "", "null"):
            # Infer from metric name
            if "electrolyte" in metric.lower():
                tags["ionic_conductivity_scope"] = "ELECTROLYTE"
            else:
                tags["ionic_conductivity_scope"] = "COATING"
    
    # Also check ionic_conductivity_scope tag for other metrics that may have it
    if tags.get("ionic_conductivity_scope") in (None, "", "null"):
        # Check if this tag exists but is None - set to UNCLEAR
        if "ionic_conductivity_scope" in tags:
            tags["ionic_conductivity_scope"] = "UNCLEAR"
    
    # === DFT Adsorption Corrections ===
    if metric in ("zn_adsorption_energy_eV", "zn_binding_energy_eV"):
        if tags.get("zn_adsorption_source") in (None, "", "null"):
            source_type = tags.get("source_type", "")
            if source_type in ("DFT", "dft", "CALCULATION"):
                tags["zn_adsorption_source"] = "DFT"
            elif source_type in ("EXPERIMENTAL", "TEXT"):
                tags["zn_adsorption_source"] = "EXPERIMENTAL"
            else:
                tags["zn_adsorption_source"] = "UNCLEAR"
    
    # === before_after Handling ===
    # Many metrics need before_after context
    if tags.get("before_after") in (None, "", "null"):
        # Check if we can infer from other tags
        eis_param = tags.get("eis_param", "")
        if "BEFORE" in str(eis_param).upper():
            tags["before_after"] = "BEFORE_COATING"
        elif "AFTER" in str(eis_param).upper():
            tags["before_after"] = "AFTER_COATING"
        else:
            tags["before_after"] = "UNCLEAR"
    
    # === Normalize before_after enum ===
    before_after = tags.get("before_after", "")
    before_after_map = {
        "BEFORE": "BEFORE_COATING",
        "AFTER": "AFTER_COATING",
        "BEFORE_POLARIZATION": "BEFORE_POLARIZATION",
        "AFTER_POLARIZATION": "AFTER_POLARIZATION",
        "BEFORE_CYCLING": "BEFORE_CYCLING",
        "AFTER_CYCLING": "AFTER_CYCLING",
    }
    if before_after in before_after_map:
        tags["before_after"] = before_after_map[before_after]
    
    # === C6: sample_type Consistency by cell_type ===
    conditions = m.get("conditions") or {}
    cell_type = conditions.get("cell_type", "")
    sample_type = tags.get("sample_type", "")
    material_id = conditions.get("material_id", "")
    
    # Infer sample_type from material_id if missing
    if not sample_type and material_id:
        mat_lower = material_id.lower()
        if "bare" in mat_lower or mat_lower == "zn" or mat_lower.startswith("zn||zn"):
            sample_type = "BARE_ZN"
            tags["sample_type"] = sample_type
        elif any(x in mat_lower for x in ["@zn", "/zn", "pani", "cof", "graphene"]):
            sample_type = "COATED"
            tags["sample_type"] = sample_type
    
    # C6 Rule: FULL_CELL should use CONTROL, not BARE_ZN for uncoated
    if cell_type == "FULL_CELL" and sample_type == "BARE_ZN":
        tags["sample_type"] = "CONTROL"
    
    # C6 Rule: Ensure consistency - CONTROL only in FULL_CELL
    if cell_type == "SYMMETRIC" and sample_type == "CONTROL":
        tags["sample_type"] = "BARE_ZN"
    
    return m


def auto_correct_measurements(measurements: list) -> list:
    """Apply tag corrections to all measurements."""
    return [auto_correct_tags(m) if isinstance(m, dict) else m for m in measurements]
