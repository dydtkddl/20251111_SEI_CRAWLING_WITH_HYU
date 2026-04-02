# scripts/lib/adaptive_planner.py
"""Inclusion-based adaptive extraction plan generator."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Set

# Field to extractor mapping
FIELD_TO_EXTRACTOR = {
    # Input fields
    "protective_layer_thickness_um": "EXTRACT_INPUT",
    "protective_layer_material": "EXTRACT_INPUT",
    "ion_conductivity_mS_cm": "EXTRACT_INPUT",
    "contact_angle_deg": "EXTRACT_INPUT",
    "zn_adsorption_energy_eV": "EXTRACT_INPUT",
    "areal_capacity_mAhcm2": "EXTRACT_INPUT",
    "areal_current_density_mAcm2": "EXTRACT_INPUT",
    # Output fields
    "galvanostatic_cycling_performance_h": "EXTRACT_CYCLING",
    "corrosion_current_density_uAcm2": "EXTRACT_CORROSION",
    "corrosion_potential_V": "EXTRACT_CORROSION",
    "overpotential_mV": "EXTRACT_OVERPOTENTIAL",
    "eis_Rs_Ohm": "EXTRACT_EIS",
    "eis_Rct_Ohm": "EXTRACT_EIS",
    "eis_Rsei_Ohm": "EXTRACT_EIS",
    "electrochemical_impedance_Ohm": "EXTRACT_EIS",
}

# Labels that force extractor execution (for figure-only data)
LABEL_FORCE = {
    "EIS_NYQUIST": "EXTRACT_EIS",
    "CORROSION_TAFEL": "EXTRACT_CORROSION",
    "ELECTROCHEM_CYCLING": "EXTRACT_CYCLING",
}


def build_plan_from_inclusion(
    inclusion_rows: List[Dict[str, Any]],
    cases_obj: Dict[str, Any],
    paper_id: str,
    paper_dir: str,
) -> Dict[str, Any]:
    """
    Build extraction plan from inclusion results.
    
    This implements the "adaptive prompt builder" concept:
    - Analyze inclusion results to determine which extractors are needed
    - Generate case-level task activation flags
    - Prepare output file paths
    
    Args:
        inclusion_rows: List of inclusion result dicts from CHUNK_INCLUDE task
        cases_obj: Cases object from BUILD_CASES task
        paper_id: Paper identifier
        paper_dir: Paper directory path
    
    Returns:
        Plan dict with case_tasks, global_scopes_hint, and output paths
    """
    cases = cases_obj.get("cases", [])
    case_ids = [c.get("case_id_hint") for c in cases]

    # (1) Aggregate fields present across all chunks
    fields_global: Set[str] = set()
    scopes_global: Dict[str, str] = {
        "ionic_conductivity_scope": "UNCLEAR",
        "contact_angle_baseline": "UNCLEAR",
        "overpotential_type": "UNCLEAR",
        "eis_metric_hint": "UNCLEAR"
    }

    for r in inclusion_rows:
        for f in r.get("fields_present", []):
            fields_global.add(f)
        sc = r.get("scope", {}) or {}
        for k in scopes_global:
            v = sc.get(k)
            if v and v != "UNCLEAR":
                scopes_global[k] = v

    # (2) Determine enabled extractors based on fields
    enabled_extractors: Set[str] = set()
    for f in fields_global:
        if f in FIELD_TO_EXTRACTOR:
            enabled_extractors.add(FIELD_TO_EXTRACTOR[f])

    # (3) Force-enable extractors for figure-only data
    # Conservative approach: enable all common extractors
    enabled_extractors.update({
        "EXTRACT_CYCLING",
        "EXTRACT_EIS",
        "EXTRACT_CORROSION",
        "EXTRACT_OVERPOTENTIAL"
    })
    enabled_extractors.add("EXTRACT_INPUT")

    # (4) Build case-level task table
    all_extractors = [
        "EXTRACT_INPUT",
        "EXTRACT_CYCLING",
        "EXTRACT_CORROSION",
        "EXTRACT_EIS",
        "EXTRACT_OVERPOTENTIAL"
    ]
    
    case_tasks: Dict[str, Dict[str, bool]] = {}
    for cid in case_ids:
        case_tasks[cid] = {e: (e in enabled_extractors) for e in all_extractors}

    # (5) Setup output paths
    d = Path(paper_dir) / "derived"
    plan = {
        "paper_id": paper_id,
        "case_tasks": case_tasks,
        "global_scopes_hint": scopes_global,
        "outputs": {
            "measurements_raw_main": str(d / "06_measurements_raw_main.jsonl"),
            "measurements_raw_supp": str(d / "06_measurements_raw_supp.jsonl"),
            "measurements_organized": str(d / "07_measurements_organized.jsonl"),
            "measurements_normalized": str(d / "08_measurements_normalized.jsonl"),
            "qc_report": str(d / "09_qc_report.json"),
            "digitize_tasks": str(d / "10_tasks_digitize.jsonl"),
        }
    }
    return plan
