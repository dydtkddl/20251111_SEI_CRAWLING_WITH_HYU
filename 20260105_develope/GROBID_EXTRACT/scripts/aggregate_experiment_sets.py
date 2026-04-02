# scripts/aggregate_experiment_sets.py
"""
Experiment Set Aggregator v1.0

Groups measurements by experiment_set_id and creates structured
input-output relationship table in CSV and JSONL formats.

Usage:
    python scripts/aggregate_experiment_sets.py --paper S000862232400438X
    python scripts/aggregate_experiment_sets.py --all

Output:
    - 12_experiment_sets.jsonl: One row per experiment set
    - 12_experiment_sets.csv: Flattened table for analysis
"""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Set
from collections import defaultdict


# ============================================================================
# METRIC CLASSIFICATION
# ============================================================================

INPUT_METRICS = {
    # Protective layer properties
    "protective_layer_material",
    "protective_layer_method",
    "protective_layer_thickness_nm",
    "protective_layer_loading_mg_cm2",
    "deposition_potential_V_vs_ref",
    "deposition_time_min",
    # DFT inputs
    "zn_adsorption_energy_eV",
    "zn_binding_energy_eV",
    # Physical properties
    "contact_angle_deg",
    "surface_work_function_eV",
    "youngs_modulus_GPa",
}

OUTPUT_METRICS = {
    # Cycling performance
    "cycle_life_hours",
    "cycle_life_cycles",
    "capacity_retention_pct",
    "coulombic_efficiency_pct",
    "voltage_hysteresis_mV",
    # Rate performance
    "specific_capacity_mAh_g",
    "areal_capacity_mAh_cm2",
    "energy_density_Wh_kg",
    "power_density_W_kg",
    # Kinetics
    "ion_diffusion_coeff_cm2_s",
    "transference_number",
    "capacitive_contribution_pct",
    "b_value_kinetic_exponent",
    # EIS
    "eis_Rs_Ohm",
    "eis_Rct_Ohm",
    "eis_R0_Ohm",
    "eis_Rsei_Ohm",
    # Overpotential
    "overpotential_mV",
    "nucleation_overpotential_mV",
    "deposition_overpotential_mV",
    # Corrosion
    "corrosion_current_density_uAcm2",
    "corrosion_potential_V",
}


# ============================================================================
# AGGREGATION FUNCTIONS
# ============================================================================

def classify_metric(metric: str) -> str:
    """Classify metric as INPUT, OUTPUT, or CONDITION."""
    if metric in INPUT_METRICS:
        return "INPUT"
    elif metric in OUTPUT_METRICS:
        return "OUTPUT"
    else:
        return "CONDITION"


def group_by_experiment_set(measurements: List[Dict]) -> Dict[str, List[Dict]]:
    """Group measurements by experiment_set_id."""
    groups = defaultdict(list)
    for m in measurements:
        exp_id = m.get("experiment_set_id", "UNKNOWN")
        groups[exp_id].append(m)
    return dict(groups)


# ============================================================================
# P2: INPUT INHERITANCE (COATED samples inherit from EXP_COATED_SAMPLE)
# ============================================================================

def inherit_inputs_across_sets(records: List[Dict]) -> List[Dict]:
    """
    P2 Fix: COATED sample sets inherit INPUTs from EXP_COATED_SAMPLE.
    """
    # Group by paper
    by_paper = defaultdict(list)
    for rec in records:
        by_paper[rec.get('paper_id', '')].append(rec)
    
    for paper_id, recs in by_paper.items():
        # Find EXP_COATED_SAMPLE for this paper
        coated_source = next(
            (r for r in recs if r.get('experiment_set_id') == 'EXP_COATED_SAMPLE'),
            None
        )
        
        if coated_source and coated_source.get('inputs'):
            source_inputs = coated_source['inputs']
            
            for rec in recs:
                sample_types = rec.get('sample_types', [])
                has_coated = 'COATED' in sample_types or any('COATED' in str(s) for s in sample_types)
                
                # Inherit inputs if this is a COATED sample with empty inputs
                if has_coated and not rec.get('inputs'):
                    rec['inputs'] = dict(source_inputs)
    
    return records


# ============================================================================
# P3: DFT SEPARATION
# ============================================================================

DFT_METRICS = {"zn_adsorption_energy_eV", "zn_binding_energy_eV"}


def separate_dft_results(records: List[Dict]) -> tuple:
    """
    P3 Fix: Separate DFT-only records from main experiment records.
    
    Returns (main_records, dft_records)
    """
    dft_records = []
    main_records = []
    
    for rec in records:
        inputs = rec.get('inputs', {})
        outputs = rec.get('outputs', [])
        
        # Check if ONLY DFT metrics
        has_only_dft_inputs = inputs and all(k in DFT_METRICS for k in inputs.keys())
        has_no_outputs = not outputs
        
        if has_only_dft_inputs and has_no_outputs:
            dft_records.append(rec)
        else:
            main_records.append(rec)
    
    return main_records, dft_records


# ============================================================================
# P1: MATERIAL ID NORMALIZATION
# ============================================================================

def normalize_material_id(mat_id: str) -> str:
    """
    P1 Fix: Normalize material_id for better grouping.
    'TpPa@Zn battery' -> 'TpPa@Zn'
    """
    if not mat_id:
        return mat_id
    
    import re
    # Remove common suffixes
    mat_id = re.sub(r'\s*(battery|cell|anode|electrode|sample)\s*$', '', mat_id, flags=re.IGNORECASE)
    # Normalize separators
    mat_id = mat_id.strip()
    return mat_id


def aggregate_experiment_set(exp_id: str, measurements: List[Dict]) -> Dict[str, Any]:
    """
    Aggregate all measurements in an experiment set into a structured record.
    
    Returns:
        {
            "experiment_set_id": "MAT_G_Zn",
            "paper_id": "S000862232400438X",
            "material_ids": ["G/Zn", "PANI/G/CC||G/Zn"],
            "cell_types": ["SYMMETRIC", "FULL_CELL"],
            "sample_type": "COATED",
            
            "inputs": {
                "protective_layer_material": "graphene",
                "protective_layer_thickness_nm": 1.02,
                "deposition_time_min": 30,
                ...
            },
            
            "outputs": {
                "cycle_life_hours": {"value": 1000, "conditions": {...}},
                "eis_Rct_Ohm": {"value": 27, "conditions": {...}},
                ...
            },
            
            "context": {
                "electrolyte": "2 M Zn(CF3SO3)2",
                "temperature_C": null,
            },
            
            "comparison_pairs": [
                {"metric": "eis_Rct_Ohm", "treated": 27, "control": 162, "improvement": "83%"}
            ],
            
            "evidence_summary": ["Dynamics analysis", "Anode stability", "Battery performance"]
        }
    """
    record = {
        "experiment_set_id": exp_id,
        "paper_id": measurements[0].get("paper_id", "") if measurements else "",
        "material_ids": [],
        "cell_types": set(),
        "sample_types": set(),
        "inputs": {},
        "outputs": [],
        "context": {},
        "comparison_pairs": [],
        "evidence_sections": set(),
        "measurement_count": len(measurements),
        # Phase 5 v5.0: Experiment context from LLM
        "experiment_purposes": set(),
        "key_findings": [],
        "improvements_summary": [],
    }
    
    for m in measurements:
        metric = m.get("metric", "")
        value = m.get("value")
        conditions = m.get("conditions", {}) or {}
        tags = m.get("tags", {}) or {}
        evidence = m.get("evidence", {}) or {}
        
        # Collect material_ids
        mat_id = conditions.get("material_id")
        if mat_id and mat_id not in record["material_ids"]:
            record["material_ids"].append(mat_id)
        
        # Collect cell types
        cell_type = conditions.get("cell_type")
        if cell_type:
            record["cell_types"].add(cell_type)
        
        # Collect sample types
        sample_type = tags.get("sample_type")
        if sample_type:
            record["sample_types"].add(sample_type)
        
        # Collect evidence sections
        section = evidence.get("section_path")
        if section:
            record["evidence_sections"].add(section)
        
        # Classify and store metric
        category = classify_metric(metric)
        
        if category == "INPUT":
            # Store as simple key-value
            record["inputs"][metric] = value
        
        elif category == "OUTPUT":
            # Store with conditions context
            output_entry = {
                "metric": metric,
                "value": value,
                "unit": m.get("unit"),
                "conditions": {
                    k: v for k, v in conditions.items() 
                    if k in ["areal_current_density_mA_cm2", "specific_current_A_g", 
                             "rate_C", "cycle_number", "temperature_C"]
                    and v is not None
                },
                "quote": (evidence.get("quote") or "")[:100],
            }
            record["outputs"].append(output_entry)
        
        # Extract comparison pairs
        cmp_group = m.get("comparison_group")
        if cmp_group and cmp_group.get("paired_with"):
            for pair in cmp_group.get("paired_with", []):
                if pair.get("relationship") == "COATED_VS_BARE":
                    treated_val = value if cmp_group.get("role") == "TREATED" else pair.get("value")
                    control_val = pair.get("value") if cmp_group.get("role") == "TREATED" else value
                    
                    if treated_val and control_val:
                        try:
                            # Skip list values
                            if isinstance(treated_val, list) or isinstance(control_val, list):
                                continue
                            improvement = ((float(control_val) - float(treated_val)) / float(control_val)) * 100
                            improvement_str = f"{abs(improvement):.1f}% {'↓' if improvement > 0 else '↑'}"
                        except (ValueError, ZeroDivisionError, TypeError):
                            improvement_str = "N/A"
                        
                        record["comparison_pairs"].append({
                            "metric": metric,
                            "treated_value": treated_val,
                            "control_value": control_val,
                            "improvement": improvement_str,
                        })
        
        # Extract common context
        if not record["context"].get("electrolyte"):
            record["context"]["electrolyte"] = conditions.get("electrolyte")
        if not record["context"].get("temperature_C"):
            record["context"]["temperature_C"] = conditions.get("temperature_C")
        
        # Extract experiment_context (Phase 5 v5.0)
        exp_ctx = m.get("experiment_context", {})
        if exp_ctx:
            purpose = exp_ctx.get("purpose")
            if purpose:
                record["experiment_purposes"].add(purpose)
            
            key_finding = exp_ctx.get("key_finding")
            if key_finding:
                record["key_findings"].append({
                    "metric": metric,
                    "finding": key_finding,
                    "improvement": exp_ctx.get("improvement")
                })
    
    # Convert sets to lists for JSON serialization
    record["cell_types"] = list(record["cell_types"])
    record["sample_types"] = list(record["sample_types"])
    record["evidence_sections"] = list(record["evidence_sections"])
    record["experiment_purposes"] = list(record["experiment_purposes"])
    
    # Deduplicate comparison pairs
    seen_pairs = set()
    unique_pairs = []
    for pair in record["comparison_pairs"]:
        key = (pair["metric"], pair["treated_value"], pair["control_value"])
        if key not in seen_pairs:
            seen_pairs.add(key)
            unique_pairs.append(pair)
    record["comparison_pairs"] = unique_pairs
    
    return record


def flatten_for_csv(records: List[Dict]) -> List[Dict]:
    """Flatten aggregated records for CSV export."""
    rows = []
    
    for rec in records:
        base_row = {
            "experiment_set_id": rec["experiment_set_id"],
            "paper_id": rec["paper_id"],
            "material_ids": ", ".join(rec.get("material_ids", [])),
            "cell_types": ", ".join(rec.get("cell_types", [])),
            "sample_types": ", ".join(rec.get("sample_types", [])),
            "electrolyte": rec.get("context", {}).get("electrolyte", ""),
            "measurement_count": rec.get("measurement_count", 0),
        }
        
        # Add input columns
        for metric, value in rec.get("inputs", {}).items():
            base_row[f"IN_{metric}"] = value
        
        # Add key output columns (take first value for each metric)
        output_by_metric = {}
        for out in rec.get("outputs", []):
            metric = out.get("metric")
            if metric and metric not in output_by_metric:
                output_by_metric[metric] = out.get("value")
        
        for metric, value in output_by_metric.items():
            base_row[f"OUT_{metric}"] = value
        
        # Add comparison summary
        comparisons = []
        for pair in rec.get("comparison_pairs", []):
            comparisons.append(f"{pair['metric']}: {pair['improvement']}")
        base_row["improvements"] = "; ".join(comparisons[:5])  # Top 5
        
        # Add experiment_context fields (Phase 5 v5.0)
        purposes = rec.get("experiment_purposes", [])
        base_row["experiment_purposes"] = "; ".join(purposes[:3])
        
        key_findings = rec.get("key_findings", [])
        findings_strs = [f.get("finding", "") for f in key_findings[:3]]
        base_row["key_findings"] = "; ".join(findings_strs)
        
        rows.append(base_row)
    
    return rows


# ============================================================================
# MAIN FUNCTIONS
# ============================================================================

def process_paper(paper_dir: Path) -> List[Dict]:
    """Process a single paper and return aggregated experiment sets."""
    final_path = paper_dir / "derived" / "10_measurements_final.jsonl"
    
    if not final_path.exists():
        return []
    
    measurements = []
    with open(final_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                measurements.append(json.loads(line))
    
    if not measurements:
        return []
    
    # Group by experiment set
    groups = group_by_experiment_set(measurements)
    
    # Aggregate each group
    records = []
    for exp_id, meas_list in groups.items():
        record = aggregate_experiment_set(exp_id, meas_list)
        records.append(record)
    
    return records


def main():
    parser = argparse.ArgumentParser(description="Aggregate experiment sets from measurements")
    parser.add_argument("--paper", help="Single paper ID to process")
    parser.add_argument("--all", action="store_true", help="Process all papers")
    parser.add_argument("--data-root", default="data", help="Data root directory")
    parser.add_argument("--output-dir", default=".", help="Output directory for aggregated files")
    args = parser.parse_args()
    
    data_root = Path(args.data_root)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    all_records = []
    
    if args.paper:
        paper_dir = data_root / "papers" / args.paper
        records = process_paper(paper_dir)
        all_records.extend(records)
        print(f"Processed {args.paper}: {len(records)} experiment sets")
    
    elif args.all:
        papers_dir = data_root / "papers"
        for paper_dir in papers_dir.iterdir():
            if paper_dir.is_dir():
                records = process_paper(paper_dir)
                all_records.extend(records)
                if records:
                    print(f"Processed {paper_dir.name}: {len(records)} experiment sets")
    
    if not all_records:
        print("No records to aggregate")
        return
    
    # P2: Inherit inputs from EXP_COATED_SAMPLE to other COATED sets
    all_records = inherit_inputs_across_sets(all_records)
    print(f"P2: Inherited inputs across {len(all_records)} experiment sets")
    
    # P3: Separate DFT results
    main_records, dft_records = separate_dft_results(all_records)
    print(f"P3: Separated {len(dft_records)} DFT-only records from {len(main_records)} main records")
    
    # Save main JSONL
    jsonl_path = output_dir / "12_experiment_sets.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for rec in main_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"Saved {len(main_records)} experiment sets to {jsonl_path}")
    
    # Save DFT JSONL separately
    if dft_records:
        dft_path = output_dir / "12_dft_results.jsonl"
        with open(dft_path, "w", encoding="utf-8") as f:
            for rec in dft_records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"Saved {len(dft_records)} DFT records to {dft_path}")
    
    # Save CSV (main records only, without DFT)
    csv_rows = flatten_for_csv(main_records)
    if csv_rows:
        # Get all columns
        all_cols = set()
        for row in csv_rows:
            all_cols.update(row.keys())
        
        # Order columns: base first, then IN_, then OUT_
        base_cols = ["experiment_set_id", "paper_id", "material_ids", "cell_types", 
                     "sample_types", "electrolyte", "measurement_count"]
        in_cols = sorted([c for c in all_cols if c.startswith("IN_")])
        out_cols = sorted([c for c in all_cols if c.startswith("OUT_")])
        other_cols = sorted([c for c in all_cols if c not in base_cols and not c.startswith("IN_") and not c.startswith("OUT_")])
        
        ordered_cols = base_cols + in_cols + out_cols + other_cols
        
        csv_path = output_dir / "12_experiment_sets.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=ordered_cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"Saved CSV to {csv_path}")
    
    # Print summary
    print("\n=== SUMMARY ===")
    for rec in all_records:
        print(f"\n[{rec['experiment_set_id']}]")
        print(f"  Paper: {rec['paper_id']}")
        print(f"  Materials: {', '.join(rec.get('material_ids', []))}")
        print(f"  Inputs: {len(rec.get('inputs', {}))}, Outputs: {len(rec.get('outputs', []))}")
        if rec.get("comparison_pairs"):
            print(f"  Improvements:")
            for pair in rec["comparison_pairs"][:3]:
                print(f"    - {pair['metric']}: {pair['treated_value']} vs {pair['control_value']} ({pair['improvement']})")


if __name__ == "__main__":
    main()
