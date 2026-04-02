# scripts/lib/eis_postprocessor.py
"""
EIS Postprocessor Module

Per 15_설계.md Section 6 (Day 1):
- Extract EIS settings (frequency_range_Hz, ac_amplitude_V) from standalone metrics
- Inject them as conditions into EIS resistance measurements (Rct, Rs, R0)
- Remove the standalone setting metrics (they are configuration, not performance)
"""
from __future__ import annotations
from typing import Any, Dict, List
import logging

logger = logging.getLogger(__name__)


def inject_eis_conditions(measurements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Inject EIS settings as conditions to EIS measurements, remove standalone settings.
    
    Per 15_설계.md Section 6.1:
    - eis_frequency_range_Hz and eis_ac_amplitude_V are settings, not metrics
    - They should be conditions on eis_*_Ohm metrics, not standalone measurements
    
    Args:
        measurements: List of measurement dicts
        
    Returns:
        Filtered list with settings injected as conditions
    """
    # Group by paper_id/case_id for proper scoping
    grouped: Dict[tuple, Dict] = {}
    
    for m in measurements:
        if not isinstance(m, dict):
            continue
        key = (m.get("paper_id"), m.get("case_id"))
        if key not in grouped:
            grouped[key] = {"settings": {}, "eis_metrics": [], "others": []}
        
        metric = m.get("metric", "")
        
        # Collect settings
        if metric == "eis_frequency_range_Hz":
            grouped[key]["settings"]["frequency_range_Hz"] = m.get("value")
            logger.debug(f"Collected EIS frequency range: {m.get('value')}")
        elif metric == "eis_ac_amplitude_V":
            grouped[key]["settings"]["ac_amplitude_V"] = m.get("value")
            logger.debug(f"Collected EIS AC amplitude: {m.get('value')}")
        elif metric.startswith("eis_") and metric.endswith("_Ohm"):
            grouped[key]["eis_metrics"].append(m)
        else:
            grouped[key]["others"].append(m)
    
    # Inject settings and build result
    result = []
    
    for key, group in grouped.items():
        settings = group["settings"]
        
        # Inject settings into EIS metrics
        for m in group["eis_metrics"]:
            m = dict(m)  # Don't mutate original
            conditions = m.get("conditions")
            if conditions is None:
                conditions = {}
            else:
                conditions = dict(conditions)
            m["conditions"] = conditions
            
            # Inject frequency range if not already present
            if settings.get("frequency_range_Hz") and conditions.get("frequency_range_Hz") is None:
                conditions["frequency_range_Hz"] = settings["frequency_range_Hz"]
                
            # Inject AC amplitude if not already present
            if settings.get("ac_amplitude_V") and conditions.get("ac_amplitude_V") is None:
                conditions["ac_amplitude_V"] = settings["ac_amplitude_V"]
            
            result.append(m)
        
        # Add non-EIS measurements as-is
        result.extend(group["others"])
        
        # Log injection stats
        if settings and group["eis_metrics"]:
            logger.info(f"Injected EIS conditions ({settings}) into {len(group['eis_metrics'])} measurements")
    
    return result


def split_multivalue_eis(measurements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Split EIS measurements that have multiple values in alternatives.
    
    Per 15_설계.md Section 6.3:
    When text says "R0 and Rs were 15 and 22 Ω", split into separate measurements.
    
    Args:
        measurements: List of measurement dicts
        
    Returns:
        Expanded list with multi-value measurements split
    """
    result = []
    
    for m in measurements:
        if not isinstance(m, dict):
            result.append(m)
            continue
            
        metric = m.get("metric", "")
        alternatives = m.get("value_alternatives", [])
        
        # Only process EIS metrics with alternatives
        if not metric.startswith("eis_") or not alternatives:
            result.append(m)
            continue
        
        # Check for R0/Rs confusion
        evidence = m.get("evidence", {}) or {}
        quote = (evidence.get("quote") or "").lower()
        
        # Detect patterns like "R0 and Rs" or "Rs and R0"
        has_multi_r = ("r0" in quote and "rs" in quote) or ("r₀" in quote and "rₛ" in quote)
        
        if has_multi_r and len(alternatives) >= 2:
            # Split into separate measurements
            logger.info(f"Splitting multi-R EIS measurement with {len(alternatives)} alternatives")
            
            for i, alt in enumerate(alternatives):
                new_m = dict(m)
                new_m["value"] = alt.get("value")
                new_m["confidence"] = alt.get("confidence", m.get("confidence", 0.5))
                
                # Try to infer which R this is based on order
                # R0 is typically first, Rs second
                if i == 0 and "eis_Rct" in metric:
                    new_m["metric"] = "eis_R0_Ohm"
                elif i == 1 and "eis_Rct" in metric:
                    new_m["metric"] = "eis_Rs_Ohm"
                
                # Remove alternatives from the split measurement
                new_m.pop("value_alternatives", None)
                new_m.pop("qc_flags", None)
                
                result.append(new_m)
        else:
            result.append(m)
    
    return result
