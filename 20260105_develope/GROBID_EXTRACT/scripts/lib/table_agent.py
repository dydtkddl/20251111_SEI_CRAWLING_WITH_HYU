# scripts/lib/table_agent.py
"""
Enterprise Table Agent: Specialized extraction from tables.

Tables often contain critical structured data that requires different
processing than free text. This agent:
1. Categorizes tables by content type
2. Maps columns to metrics
3. Extracts values with row-case matching
"""
from __future__ import annotations
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from scripts.lib.llm_client import call_llm_json, get_model_for_task



# ============================================================================
# Table Type Definitions
# ============================================================================
TABLE_TYPES = {
    "COATING_PROPERTY": {
        "keywords": ["thickness", "coating", "layer", "material", "composition", "conductivity"],
        "metrics": ["protective_layer_thickness_um", "ion_conductivity_mS_cm", "protective_layer_material"]
    },
    "ELECTROCHEM_CYCLING": {
        "keywords": ["cycling", "cycle", "stability", "lifespan", "performance", "hours", "symmetric"],
        "metrics": ["galvanostatic_cycling_performance_h", "galvanostatic_cycling_cycles"]
    },
    "CORROSION": {
        "keywords": ["corrosion", "tafel", "icorr", "ecorr", "polarization"],
        "metrics": ["corrosion_current_density_uAcm2", "corrosion_potential_V"]
    },
    "EIS": {
        "keywords": ["impedance", "eis", "nyquist", "rs", "rct", "resistance"],
        "metrics": ["eis_Rs_Ohm", "eis_Rct_Ohm", "eis_Rsei_Ohm"]
    },
    "CONTACT_ANGLE": {
        "keywords": ["contact angle", "wettability", "hydrophilic", "hydrophobic"],
        "metrics": ["contact_angle_deg"]
    }
}


def categorize_table(caption: str, headers: List[str]) -> Optional[str]:
    """Determine table type from caption and headers."""
    combined = (caption + " " + " ".join(headers)).lower()
    
    scores = {}
    for ttype, config in TABLE_TYPES.items():
        score = sum(1 for kw in config["keywords"] if kw.lower() in combined)
        scores[ttype] = score
    
    if not scores:
        return None
    
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


# ============================================================================
# Column Mapping
# ============================================================================
COLUMN_PATTERNS = {
    "sample": re.compile(r"sample|specimen|electrode|anode|material|name", re.I),
    "thickness_um": re.compile(r"thickness|thick\.?|layer", re.I),
    "ion_conductivity": re.compile(r"ionic?\s*conduct|σ|conductivity", re.I),
    "contact_angle": re.compile(r"contact\s*angle|CA|θ", re.I),
    "cycling_hours": re.compile(r"cycling|lifespan|time|hours?|h$", re.I),
    "cycling_cycles": re.compile(r"cycles?|number", re.I),
    "icorr": re.compile(r"icorr|corrosion\s*current|j\s*corr", re.I),
    "ecorr": re.compile(r"ecorr|corrosion\s*potential|e\s*corr", re.I),
    "rs": re.compile(r"^rs$|R\s*s|solution\s*resist", re.I),
    "rct": re.compile(r"^rct$|R\s*ct|charge\s*transfer", re.I),
}


def map_columns(headers: List[str]) -> Dict[int, str]:
    """Map column indices to metric names."""
    mapping = {}
    for i, h in enumerate(headers):
        for metric, pattern in COLUMN_PATTERNS.items():
            if pattern.search(h):
                mapping[i] = metric
                break
    return mapping


# ============================================================================
# Value Extraction from Parsed Table
# ============================================================================
def parse_table_value(cell: str) -> Tuple[Optional[float], Optional[str]]:
    """Parse a table cell into (value, unit)."""
    if not cell:
        return None, None
    
    # Clean
    cell = cell.strip().replace(",", "")
    
    # Try to find number
    match = re.search(r"([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", cell)
    if not match:
        return None, None
    
    value = float(match.group(1))
    
    # Extract unit
    unit = None
    unit_match = re.search(r"(µm|um|nm|mS/cm|µS/cm|S/cm|mA/cm|µA/cm|mV|V|Ω|ohm|°|deg|h|hours?)", cell, re.I)
    if unit_match:
        unit = unit_match.group(1)
    
    return value, unit


def extract_from_table(
    table_id: str,
    caption: str,
    rows: List[List[str]],
    cases: List[Dict[str, Any]],
    paper_id: str
) -> List[Dict[str, Any]]:
    """
    Extract measurements from a parsed table.
    
    Args:
        table_id: Table identifier
        caption: Table caption
        rows: Parsed table rows (first row is usually header)
        cases: List of case dicts to match samples to
        paper_id: Paper identifier
    
    Returns:
        List of measurement dicts
    """
    if not rows or len(rows) < 2:
        return []
    
    headers = rows[0]
    table_type = categorize_table(caption, headers)
    if not table_type:
        return []
    
    col_map = map_columns(headers)
    if not col_map:
        return []
    
    # Find sample column
    sample_col = None
    for i, h in enumerate(headers):
        if COLUMN_PATTERNS["sample"].search(h):
            sample_col = i
            break
    
    measurements = []
    
    for row in rows[1:]:
        if len(row) <= max(col_map.keys()):
            continue
        
        # Match row to case
        sample_name = row[sample_col] if sample_col is not None else None
        matched_case_id = match_sample_to_case(sample_name, cases) if sample_name else None
        
        if not matched_case_id:
            matched_case_id = cases[0].get("case_id") if cases else "UNKNOWN"
        
        for col_idx, metric_type in col_map.items():
            if col_idx >= len(row):
                continue
            
            cell = row[col_idx]
            value, unit = parse_table_value(cell)
            
            if value is None:
                continue
            
            # Map metric type to actual metric name
            metric_name = map_metric_type_to_name(metric_type, table_type)
            if not metric_name:
                continue
            
            measurements.append({
                "paper_id": paper_id,
                "case_id": matched_case_id,
                "metric": metric_name,
                "value": value,
                "unit": unit,
                "confidence": 0.9,  # Tables usually have high confidence
                "evidence": {
                    "doc": "MAIN",
                    "table_id": table_id,
                    "section_path": "",
                    "quote": f"From Table {table_id}: {caption[:50]}..."
                },
                "extractor_id": "TABLE_AGENT_v2"
            })
    
    return measurements


def match_sample_to_case(sample_name: str, cases: List[Dict[str, Any]]) -> Optional[str]:
    """Match a sample name to a case."""
    if not sample_name or not cases:
        return None
    
    sample_lower = sample_name.lower()
    
    for case in cases:
        coating = (case.get("coating_label") or "").lower()
        material = (case.get("material_raw") or "").lower()
        
        if sample_lower in coating or coating in sample_lower:
            return case.get("case_id") or case.get("case_id_hint")
        if sample_lower in material or material in sample_lower:
            return case.get("case_id") or case.get("case_id_hint")
    
    return None


def map_metric_type_to_name(metric_type: str, table_type: str) -> Optional[str]:
    """Map column metric type to full metric name."""
    mapping = {
        "thickness_um": "protective_layer_thickness_um",
        "ion_conductivity": "ion_conductivity_mS_cm",
        "contact_angle": "contact_angle_deg",
        "cycling_hours": "galvanostatic_cycling_performance_h",
        "cycling_cycles": "galvanostatic_cycling_cycles",
        "icorr": "corrosion_current_density_uAcm2",
        "ecorr": "corrosion_potential_V",
        "rs": "eis_Rs_Ohm",
        "rct": "eis_Rct_Ohm",
    }
    return mapping.get(metric_type)


def extract_from_table_llm(
    table_id: str,
    caption: str,
    rows: List[List[str]],
    cases: List[Dict[str, Any]],
    paper_id: str
) -> List[Dict[str, Any]]:
    """
    Extract measurements from a table using LLM (Gemini Flash).
    
    This is preferred for complex tables where rule-based matching fails.
    """
    if not rows or len(rows) < 2:
        return []
        
    # Prepare variables for prompt
    task_config = get_model_for_task("TABLE_AGENT")
    model = task_config["model"]
    thinking = task_config.get("thinking", False)
    
    # Filter rows to avoid context overflow (max 20 rows)
    display_rows = rows[:20]
    
    variables = {
        "PAPER_ID": paper_id,
        "TABLE_ID": table_id,
        "TABLE_CAPTION": caption,
        "CASES_JSON": json.dumps(cases, ensure_ascii=False),
        "TABLE_ROWS_JSON": json.dumps(display_rows, ensure_ascii=False)
    }
    
    prompt_file = str(Path(__file__).parent.parent.parent / "configs" / "prompts" / "table_agent.md")
    
    try:
        result = call_llm_json(
            model=model,
            prompt_file=prompt_file,
            variables=variables,
            cache_key=f"table:{paper_id}:{table_id}",
            thinking=thinking
        )
        
        measurements = result.get("measurements", [])
        if not isinstance(measurements, list):
            measurements = [measurements] if measurements else []
            
        # Add metadata and cleanup
        for m in measurements:
            if not isinstance(m, dict): continue
            m["extractor_id"] = f"TABLE_AGENT_LLM_{model}"
            m["paper_id"] = paper_id
            if "evidence" not in m:
                m["evidence"] = {
                    "doc": "MAIN",
                    "table_id": table_id,
                    "quote": f"Extracted from Table {table_id} by {model}"
                }
        
        return [m for m in measurements if isinstance(m, dict)]
        
    except Exception as e:
        print(f"[TableAgent] LLM extraction failed for Table {table_id}: {e}")
        return []

