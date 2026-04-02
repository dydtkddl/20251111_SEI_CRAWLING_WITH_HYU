# scripts/lib/qc_rules.py
"""QC rules for validating extracted measurements."""
from __future__ import annotations
from typing import Any, Dict, List

# Expected ranges for each metric (min, max)
RANGES = {
    "protective_layer_thickness_um": (0.001, 500.0),
    "ion_conductivity_mS_cm": (1e-4, 1e2),
    "contact_angle_deg": (0.0, 180.0),
    "zn_adsorption_energy_eV": (-20.0, 5.0),  # wide range for DFT values
    "areal_capacity_mAhcm2": (1e-3, 50.0),
    "areal_current_density_mAcm2": (1e-3, 100.0),
    "galvanostatic_cycling_performance_h": (1.0, 1e5),
    "corrosion_current_density_uAcm2": (0.1, 1e5),
    "corrosion_potential_V": (-3.0, 1.0),
    "overpotential_mV": (0.0, 1000.0),
    "eis_Rs_Ohm": (0.01, 1e5),
    "eis_Rct_Ohm": (0.01, 1e5),
    "eis_Rsei_Ohm": (0.01, 1e5),
    "electrochemical_impedance_Ohm": (0.01, 1e5),
}


def _to_float(x):
    """Safely convert to float."""
    try:
        return float(x)
    except Exception:
        return None


# Normalization for overpotential types
OVERPOTENTIAL_TYPE_MAP = {
    "VOLTAGE HYSTERESIS": "HYSTERESIS",
    "voltage hysteresis": "HYSTERESIS",
    "VOLTAGE_HYSTERESIS": "HYSTERESIS",
    "Hysteresis": "HYSTERESIS",
}

VALID_OVERPOTENTIAL_TYPES = {"NUCLEATION", "STEADY", "DEPOSITION", "HYSTERESIS", "SERIES", "UNCLEAR"}


def _normalize_overpotential_type(m: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize overpotential_type tag to standard form."""
    tags = m.get("tags", {}) or {}
    otype = tags.get("overpotential_type")
    if otype and otype in OVERPOTENTIAL_TYPE_MAP:
        tags = dict(tags)
        tags["overpotential_type"] = OVERPOTENTIAL_TYPE_MAP[otype]
        m = dict(m)
        m["tags"] = tags
    return m


def run_qc_checks(measurements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Run QC checks on extracted measurements.
    """
    flags = []
    seen = {}
    unique_measurements = []
    by_case_metric: Dict[tuple, List[Dict[str, Any]]] = {}

    for m in measurements:
        case_id = m.get("case_id")
        metric = m.get("metric", "")
        
        # 0. Clean pipe-delimited Ollama artifacts
        if "|" in metric:
            candidates = [c.strip() for c in metric.split("|")]
            picked = candidates[0]
            for c in candidates:
                if c in RANGES:
                    picked = c
                    break
            m["metric"] = picked
            metric = picked

        # P2-3: Enhanced Deduplication (Metric + Value + Key Conditions + Quote)
        evidence = m.get("evidence", {}) or {}
        quote = (evidence.get("quote") or "").strip()
        val = m.get("value")
        conditions = m.get("conditions", {}) or {}
        
        # Extract key identifiers for dedup
        material_id = conditions.get("material_id", "")
        cell_type = conditions.get("cell_type", "")
        electrolyte = conditions.get("electrolyte", "")
        
        dedupe_key = (
            case_id, 
            metric, 
            str(val), 
            material_id,
            cell_type,
            electrolyte,
            quote[:50]
        )
        
        if dedupe_key in seen:
            continue
        seen[dedupe_key] = True
        
        # Normalize overpotential type before further processing
        m = _normalize_overpotential_type(m)
        unique_measurements.append(m)

        key = (case_id, metric)
        by_case_metric.setdefault(key, []).append(m)

    # 3. Cross-Metric Validation (Thickness Leakage)
    final_cleaned = []
    for m in unique_measurements:
        metric = m.get("metric")
        case_id = m.get("case_id")
        evidence = m.get("evidence", {}) or {}
        quote = (evidence.get("quote") or "").strip()
        
        if metric == "protective_layer_thickness_um":
            v_val = _to_float(m.get("value"))
            # Check if this same quote was used for a material property
            is_leak = False
            for other in unique_measurements:
                other_metric = other.get("metric")
                other_quote = ((other.get("evidence", {}) or {}).get("quote") or "").strip().lower()
                other_val = _to_float(other.get("value"))
                
                # Linkage 1: Same quote
                if (quote.lower() in other_quote or other_quote in quote.lower()) and len(quote) > 10:
                    if other_metric == "graphene_sheet_thickness_nm":
                        is_leak = True
                        break
                
                # Linkage 2: Suspiciously small value + similar quote or case
                if (v_val is not None and v_val < 0.05) and other_metric == "graphene_sheet_thickness_nm":
                    # If the value matches after unit scale (um vs nm), it's highly likely a leak
                    if other_val is not None and abs(v_val * 1000 - other_val) < 0.01:
                        is_leak = True
                        break

            if is_leak:
                flags.append({
                    "case_id": case_id,
                    "metric": "protective_layer_thickness_um",
                    "type": "thickness_leakage_rejected",
                    "message": f"Rejected coating thickness ({v_val} um) as it appears to be a misclassified sheet thickness from context."
                })
                continue
        final_cleaned.append(m)

    # Re-build unique_measurements from final_cleaned
    unique_measurements = final_cleaned

    for m in unique_measurements:
        # 15_설계 P0 FIX: MUST re-read metric and case_id from m at each iteration
        case_id = m.get("case_id")
        metric = m.get("metric", "")
        v = _to_float(m.get("value"))
        if v is None:
            continue
            
        # Clean specific Ollama artifact: double keys
        if "|" in metric:
            # e.g. "galvanostatic_cycling_performance_h|galvanostatic_cycling_cycles" -> "galvanostatic_cycling_performance_h"
            # Split and take the first one that matches our expected ranges
            candidates = metric.split("|")
            picked = candidates[0]
            for c in candidates:
                if c in RANGES:
                    picked = c
                    break
            m["metric"] = picked
            metric = picked
            # Update key for by_case_metric grouping if strict dedupe is needed later, 
            # but usually we just want the final output clean.
        
        # Range check
        if metric in RANGES:
            lo, hi = RANGES[metric]
            if not (lo <= v <= hi):
                flags.append({
                    "type": "RANGE_OUTLIER",
                    "case_id": case_id,
                    "metric": metric,
                    "value": v,
                    "expected_range": [lo, hi],
                    "hint": "unit mismatch or extraction error suspected"
                })

        # Scope/type tag checks
        if metric == "ion_conductivity_mS_cm":
            scope = (m.get("tags", {}) or {}).get("ionic_conductivity_scope")
            if scope in (None, "", "UNCLEAR"):
                flags.append({
                    "type": "MISSING_SCOPE",
                    "case_id": case_id,
                    "metric": metric,
                    "hint": "ionic_conductivity_scope should be COATING/ELECTROLYTE/UNCLEAR; keep UNCLEAR but flag for review"
                })

        if metric == "overpotential_mV":
            otype = (m.get("tags", {}) or {}).get("overpotential_type")
            # Check if type is missing or not in valid set
            if otype is None or otype == "" or (otype not in VALID_OVERPOTENTIAL_TYPES and otype != "UNCLEAR"):
                flags.append({
                    "type": "MISSING_TYPE",
                    "case_id": case_id,
                    "metric": metric,
                    "hint": "overpotential_type should be NUCLEATION/STEADY/DEPOSITION/HYSTERESIS/SERIES/UNCLEAR"
                })

        if metric == "contact_angle_deg":
            baseline = (m.get("tags", {}) or {}).get("contact_angle_baseline")
            if baseline in (None, "", "UNCLEAR"):
                flags.append({
                    "type": "MISSING_BASELINE",
                    "case_id": case_id,
                    "metric": metric,
                    "hint": "contact_angle_baseline should be BARE_ZN/COATED/BOTH/UNCLEAR"
                })

    # P1-1: Separate NOT_FOUND/NULL records (value=None) from real measurements
    # These are "unresolved references" - mentioned but no value extracted
    unresolved_refs = []
    valid_measurements = []
    
    for m in unique_measurements:
        val = m.get("value")
        value_status = (m.get("tags") or {}).get("value_status", "")
        
        # Check if this is a NOT_FOUND/unresolved record
        is_unresolved = (
            val is None or 
            value_status in ("NOT_FOUND", "FIGURE_DIGITIZE_REQUIRED")
        )
        
        if is_unresolved:
            unresolved_refs.append(m)
        else:
            valid_measurements.append(m)
    
    # Conflict detection on valid_measurements only
    conflicts = []
    conflict_groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for m in valid_measurements:
        case_id = m.get("case_id")
        metric = m.get("metric")
        tags = m.get("tags", {}) or {}
        # Key tags that distinguish legitimately different measurements
        before_after = tags.get("before_after", "")
        otype = tags.get("overpotential_type", "")
        conditions = str(m.get("conditions", {}))
        
        conflict_key = (case_id, metric, before_after, otype, conditions)
        conflict_groups.setdefault(conflict_key, []).append(m)
    
    for key, items in conflict_groups.items():
        vals = []
        for it in items:
            v = _to_float(it.get("value"))
            if v is not None:
                vals.append(v)
        if len(set(vals)) >= 2:
            conflicts.append({
                "type": "CONFLICT",
                "case_id": key[0],
                "metric": key[1],
                "values": vals[:10],
                "hint": "multiple values detected within same condition group; check evidence"
            })

    return {
        "n_measurements": len(valid_measurements),
        "n_unresolved": len(unresolved_refs),
        "flags": flags,
        "conflicts": conflicts,
        "cleaned_measurements": valid_measurements,
        "unresolved_refs": unresolved_refs
    }
