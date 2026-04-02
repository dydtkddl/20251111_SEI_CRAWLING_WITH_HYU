# scripts/add_experiment_set_id.py
"""
Post-processing script to add experiment_set_id to measurements.

This script groups measurements by sample/material based on:
- tags.sample_type (COATED, BARE_ZN, CONTROL, etc.)
- tags.before_after (AFTER_COATING, BEFORE_COATING)
- conditions.material_id (e.g., 'PANI/G/CC||G/Zn')

Output: Adds 'experiment_set_id' field to each measurement for grouping analysis.
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Any


def determine_experiment_set(measurement: Dict[str, Any]) -> str:
    """
    Determine experiment set ID based on measurement metadata.
    
    Hierarchy:
    1. material_id from conditions (most specific)
    2. sample_type + before_after from tags
    3. Default based on before_after alone
    """
    tags = measurement.get("tags", {})
    conditions = measurement.get("conditions", {})
    
    # Priority 1: Use material_id if available
    material_id = conditions.get("material_id")
    if material_id:
        # Clean up material_id to be a valid ID
        # e.g., "PANI/G/CC||G/Zn" -> "PANI_G_CC-G_Zn"
        clean_id = material_id.replace("/", "_").replace("||", "-").replace(" ", "_")
        return f"MAT_{clean_id}"
    
    # Priority 2: Use sample_type + before_after
    sample_type = tags.get("sample_type")
    before_after = tags.get("before_after")
    
    if sample_type:
        if sample_type == "COATED":
            return "EXP_COATED_SAMPLE"
        elif sample_type == "BARE_ZN":
            return "EXP_BARE_ZINC"
        elif sample_type == "CONTROL":
            return "EXP_CONTROL"
        else:
            return f"EXP_{sample_type}"
    
    # Priority 3: Use before_after alone
    if before_after:
        if before_after == "AFTER_COATING":
            return "EXP_COATED_SAMPLE"
        elif before_after == "BEFORE_COATING":
            return "EXP_BARE_ZINC"
    
    # Default: Unknown
    return "EXP_UNCLASSIFIED"


def process_measurements(input_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """
    Process measurements and add experiment_set_id.
    Returns a dict mapping experiment_set_id to list of measurements.
    """
    measurements = []
    
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    m = json.loads(line)
                    m["experiment_set_id"] = determine_experiment_set(m)
                    measurements.append(m)
                except json.JSONDecodeError:
                    pass
    
    # Group by experiment_set_id
    grouped = {}
    for m in measurements:
        exp_id = m["experiment_set_id"]
        if exp_id not in grouped:
            grouped[exp_id] = []
        grouped[exp_id].append(m)
    
    return measurements, grouped


def main():
    if len(sys.argv) < 2:
        print("Usage: python add_experiment_set_id.py <measurements.jsonl>")
        sys.exit(1)
    
    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"Error: File not found: {input_path}")
        sys.exit(1)
    
    measurements, grouped = process_measurements(input_path)
    
    # Write enriched measurements
    output_path = input_path.with_name(input_path.stem + "_with_exp_id.jsonl")
    with open(output_path, "w", encoding="utf-8") as f:
        for m in measurements:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    
    print(f"\n{'='*60}")
    print("EXPERIMENT SET ANALYSIS")
    print(f"{'='*60}")
    print(f"\nTotal measurements: {len(measurements)}")
    print(f"Unique experiment sets: {len(grouped)}")
    print(f"\nGROUPING SUMMARY:")
    print("-" * 60)
    
    for exp_id, exp_measurements in sorted(grouped.items()):
        metrics = set(m.get("metric", "unknown") for m in exp_measurements)
        print(f"\n[{exp_id}] ({len(exp_measurements)} measurements)")
        print(f"  Metrics: {', '.join(sorted(metrics))}")
    
    print(f"\n\nOutput saved to: {output_path}")
    
    # Also print table format for easy analysis
    print(f"\n\n{'='*60}")
    print("DETAILED EXPERIMENT SET TABLE")
    print(f"{'='*60}\n")
    
    print(f"{'Experiment Set':<30} | {'Count':>5} | {'Metrics':>50}")
    print("-" * 90)
    for exp_id, exp_measurements in sorted(grouped.items()):
        metrics = ", ".join(sorted(set(m.get("metric", "?")[:15] for m in exp_measurements)))
        print(f"{exp_id:<30} | {len(exp_measurements):>5} | {metrics[:50]}")


if __name__ == "__main__":
    main()
