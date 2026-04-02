# scripts/lib/multivalue_expander.py
"""
Multi-Value Expander v1.0 (C1+C2 Fixes)

Addresses Round 2 Quality Issues:
- C1: Expand "52.85°, 62.27°, and 68.67°" → 3 separate records
- C2: Auto-generate cycle_life_cycles from large cycle_number conditions

"""
from __future__ import annotations
import re
import copy
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# C1: MULTI-VALUE EXPANSION
# ============================================================================

MULTIVALUE_PATTERN = re.compile(
    r'(\d+\.?\d*)\s*[,，]\s*(\d+\.?\d*)\s*(?:,\s*)?(?:and\s+)?(\d+\.?\d*)',
    re.IGNORECASE
)

METRICS_TO_EXPAND = {
    "contact_angle_deg",
    "corrosion_current_density_uAcm2",
    "specific_capacity_mAh_g",
    "cycle_life_hours",
    "transference_number",
    "zn_adsorption_energy_eV",
    "zn_binding_energy_eV",
}

# Material patterns for assignment
MATERIAL_PATTERNS = [
    (r"TpPa@Zn|TpPa", "TpPa@Zn"),
    (r"TpBD@Zn|TpBD", "TpBD@Zn"),
    (r"TpDATP@Zn|TpDATP", "TpDATP@Zn"),
    (r"G/Zn|graphene.Zn", "G/Zn"),
    (r"bare\s*Zn|Zn\s*anode", "bare Zn"),
]


def extract_multiple_values(quote: str, value: Any) -> List[float]:
    """
    Extract multiple numeric values from a quote/value.
    
    Handles:
    - "52.85°, 62.27°, and 68.67°"
    - "13.23, 8.34, and 21.97 μA cm−2"
    - [28, 37, 52, 81] list values
    """
    if isinstance(value, list):
        return [float(v) for v in value if isinstance(v, (int, float))]
    
    if not quote:
        return []
    
    # Find all numbers in the quote
    numbers = re.findall(r'(\d+\.?\d*)', quote)
    
    # Filter to reasonable values (not too many, not dates)
    if len(numbers) <= 1:
        return []
    
    try:
        floats = [float(n) for n in numbers[:10]]  # Limit to 10
        return floats
    except ValueError:
        return []


def infer_material_from_text(text: str, index: int, total: int) -> Optional[str]:
    """
    Infer material_id from surrounding text based on position.
    
    For patterns like "TpPa, TpBD, TpDATP showed X, Y, Z respectively"
    
    Round 12 Fix: Use positional matching to preserve order of appearance.
    """
    # Round 12: Find ALL material mentions with their positions
    material_positions = []
    for pattern, mat_id in MATERIAL_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            material_positions.append((match.start(), mat_id))
    
    # Sort by position in text (left to right order)
    material_positions.sort(key=lambda x: x[0])
    
    # Remove duplicates while preserving order
    seen = set()
    ordered_materials = []
    for pos, mat_id in material_positions:
        if mat_id not in seen:
            seen.add(mat_id)
            ordered_materials.append(mat_id)
    
    # Match by index
    if len(ordered_materials) >= total and index < len(ordered_materials):
        return ordered_materials[index]
    
    # Fallback: if we have some materials, try to assign
    if index < len(ordered_materials):
        return ordered_materials[index]
    
    return None


def expand_multivalue_record(m: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Expand a single record with multiple values into multiple records.
    
    C1: "52.85°, 62.27°, 68.67°" → 3 records with inferred material_ids
    """
    metric = m.get("metric", "")
    value = m.get("value")
    quote = m.get("evidence", {}).get("quote", "")
    
    # Skip if already a list in value (W1 will handle)
    if isinstance(value, list) and len(value) > 1:
        return expand_list_value_record(m)
    
    # Check if metric should be expanded
    if metric not in METRICS_TO_EXPAND:
        return [m]
    
    # Try to find multiple values in quote
    values = extract_multiple_values(quote, value)
    
    if len(values) <= 1:
        return [m]
    
    # Generate expanded records
    expanded = []
    for i, val in enumerate(values):
        new_m = copy.deepcopy(m)
        new_m["value"] = val
        new_m["_expanded_from"] = f"multivalue_{i+1}_of_{len(values)}"
        
        # Try to infer material_id
        inferred_mat = infer_material_from_text(quote, i, len(values))
        if inferred_mat:
            new_m.setdefault("conditions", {})["material_id"] = inferred_mat
        
        expanded.append(new_m)
    
    logger.debug(f"  Expanded {metric}: 1 → {len(expanded)} records")
    return expanded


def expand_list_value_record(m: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    W1: Expand list values like [28, 37, 52] into individual records.
    
    Pairs with corresponding condition lists if available.
    """
    value = m.get("value")
    if not isinstance(value, list):
        return [m]
    
    conditions = m.get("conditions", {}) or {}
    
    # Find paired condition (e.g., rate_C for overpotential)
    paired_cond = None
    paired_values = None
    
    for cond_key in ["rate_C", "cycle_number", "scan_rate_mV_s"]:
        cond_val = conditions.get(cond_key)
        if isinstance(cond_val, list) and len(cond_val) == len(value):
            paired_cond = cond_key
            paired_values = cond_val
            break
    
    expanded = []
    for i, val in enumerate(value):
        new_m = copy.deepcopy(m)
        new_m["value"] = val
        new_m["_expanded_from"] = f"list_{i+1}_of_{len(value)}"
        
        if paired_cond and paired_values:
            new_m["conditions"][paired_cond] = paired_values[i]
        
        expanded.append(new_m)
    
    logger.debug(f"  Expanded list: 1 → {len(expanded)} records")
    return expanded


# ============================================================================
# C2: CYCLE_LIFE_CYCLES AUTO-GENERATION
# ============================================================================

def auto_generate_cycle_life(measurements: List[Dict]) -> List[Dict]:
    """
    C2: Auto-generate cycle_life_cycles metric from large cycle_number conditions.
    
    If conditions.cycle_number >= 100 and no cycle_life_cycles exists,
    create a new measurement record.
    """
    existing_cycles = set()
    for m in measurements:
        if m.get("metric") == "cycle_life_cycles":
            val = m.get("value")
            if val:
                existing_cycles.add(float(val))
    
    new_records = []
    for m in measurements:
        cycle_num = m.get("conditions", {}).get("cycle_number")
        
        if isinstance(cycle_num, (int, float)) and cycle_num >= 100:
            if cycle_num not in existing_cycles:
                # Create new cycle_life_cycles record
                new_m = {
                    "case_id": m.get("case_id"),
                    "paper_id": m.get("paper_id"),
                    "metric": "cycle_life_cycles",
                    "value": cycle_num,
                    "unit": "cycles",
                    "conditions": copy.deepcopy(m.get("conditions", {})),
                    "tags": copy.deepcopy(m.get("tags", {})),
                    "evidence": copy.deepcopy(m.get("evidence", {})),
                    "confidence": 0.7,  # Lower confidence for auto-generated
                    "_auto_generated": True,
                    "_source_metric": m.get("metric"),
                }
                new_records.append(new_m)
                existing_cycles.add(cycle_num)
                logger.info(f"  C2: Auto-generated cycle_life_cycles={cycle_num}")
    
    return measurements + new_records


# ============================================================================
# MAIN FUNCTION
# ============================================================================

def expand_and_enrich_measurements(measurements: List[Dict]) -> List[Dict]:
    """
    Main function: Apply C1 multi-value expansion and C2 cycle_life generation.
    """
    # C1: Expand multi-value records
    expanded = []
    for m in measurements:
        expanded.extend(expand_multivalue_record(m))
    
    logger.info(f"  C1: Expanded {len(measurements)} → {len(expanded)} records")
    
    # C2: Auto-generate cycle_life_cycles
    enriched = auto_generate_cycle_life(expanded)
    
    if len(enriched) > len(expanded):
        logger.info(f"  C2: Added {len(enriched) - len(expanded)} cycle_life_cycles records")
    
    return enriched
