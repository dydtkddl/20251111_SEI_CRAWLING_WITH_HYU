# scripts/contracts/measurement_contract.py
"""
Strict JSON Schema for LLM extractor output.

This schema FORCES consistent output structure:
- evidence.doc must be MAIN/SUPP (never null)
- conditions/tags must be objects (even if empty {})
- Exactly ONE anchor: chunk_id OR figure_id OR table_id
- quote required (5-35 words verbatim)
"""
from __future__ import annotations
from typing import Any, Dict, List

# =============================================================================
# Core Measurement Schema
# =============================================================================

EVIDENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["doc", "section_path", "chunk_id", "figure_id", "table_id", "quote"],
    "properties": {
        # doc: MAIN or SUPP only (never null)
        "doc": {"enum": ["MAIN", "SUPP"]},
        
        # section_path: minimum 1 char (UNKNOWN allowed)
        "section_path": {"type": "string", "minLength": 1},
        
        # Anchors: exactly ONE should be non-null
        "chunk_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "figure_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "table_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        
        # quote: short verbatim snippet (5-35 words)
        "quote": {"type": "string", "minLength": 1}
    }
}

MEASUREMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": True,  # Allow extra fields like paper_id, case_id
    "required": ["metric", "value", "unit", "confidence", "evidence", "conditions", "tags"],
    "properties": {
        "metric": {"type": "string", "minLength": 1},
        
        # value: number, array of numbers, string, or null (for FIGURE_REF)
        "value": {
            "anyOf": [
                {"type": "number"},
                {"type": "array", "items": {"type": "number"}, "minItems": 1},
                {"type": "string"},
                {"type": "null"}
            ]
        },
        
        # unit: string or null
        "unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        
        # confidence: 0.0 to 1.0
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        
        # conditions/tags: MUST be objects (even if empty {})
        "conditions": {"type": "object"},
        "tags": {"type": "object"},
        
        # evidence: strict schema
        "evidence": EVIDENCE_SCHEMA,
        
        # value_state: optional enum
        "value_state": {
            "anyOf": [
                {"enum": ["TEXT_EXPLICIT", "TABLE_CELL", "FIGURE_REF", "DIGITIZED"]},
                {"type": "null"}
            ]
        }
    }
}

EXTRACTOR_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["measurements"],
    "properties": {
        "measurements": {
            "type": "array",
            "items": MEASUREMENT_SCHEMA,
            "minItems": 0
        }
    }
}


# =============================================================================
# Tag Enums (for strict validation)
# =============================================================================

TAG_ENUMS = {
    "before_after": ["BEFORE_COATING", "AFTER_COATING", "BEFORE", "AFTER", "UNCLEAR"],
    "eis_metric_type": ["Rct", "Rs", "R0", "Rct+Rs", "FIT", "UNCLEAR"],
    "overpotential_type": ["NUCLEATION", "DEPOSITION", "STEADY", "SERIES", "HYSTERESIS", "UNCLEAR"],
    "cycling_termination": ["CYCLES", "HOURS", "CAPACITY_FADE", "UNCLEAR"]
}


# =============================================================================
# Validation Functions
# =============================================================================

def validate_evidence(ev: Dict[str, Any]) -> List[str]:
    """Validate evidence object. Returns list of error strings."""
    errors = []
    
    if not isinstance(ev, dict):
        return ["EVIDENCE_NOT_OBJECT"]
    
    # doc validation
    doc = ev.get("doc")
    if doc not in ("MAIN", "SUPP"):
        errors.append(f"EVIDENCE_DOC_INVALID: must be MAIN or SUPP, got {doc}")
    
    # section_path
    if not ev.get("section_path"):
        errors.append("EVIDENCE_SECTION_MISSING: section_path required")
    
    # quote
    if not ev.get("quote"):
        errors.append("EVIDENCE_QUOTE_MISSING: quote required")
    
    # anchor validation: exactly ONE
    anchors = [
        bool(ev.get("chunk_id")),
        bool(ev.get("figure_id")),
        bool(ev.get("table_id"))
    ]
    if sum(anchors) == 0:
        errors.append("EVIDENCE_ANCHOR_MISSING: one of chunk_id/figure_id/table_id required")
    elif sum(anchors) > 1:
        errors.append("EVIDENCE_ANCHOR_MULTIPLE: only one anchor allowed")
    
    return errors


def validate_measurement(m: Dict[str, Any]) -> List[str]:
    """
    Validate a single measurement against the contract.
    Returns list of error strings (empty = valid).
    """
    errors = []
    
    if not isinstance(m, dict):
        return ["MEASUREMENT_NOT_OBJECT"]
    
    # Required fields
    for key in ["metric", "value", "confidence", "evidence", "conditions", "tags"]:
        if key not in m:
            errors.append(f"MISSING_KEY:{key}")
    
    # conditions/tags must be objects
    if "conditions" in m and not isinstance(m["conditions"], dict):
        errors.append("CONDITIONS_INVALID: must be an object {}")
    if "tags" in m and not isinstance(m["tags"], dict):
        errors.append("TAGS_INVALID: must be an object {}")
    
    # confidence range
    conf = m.get("confidence")
    if conf is not None and (not isinstance(conf, (int, float)) or conf < 0 or conf > 1):
        errors.append(f"CONFIDENCE_INVALID: must be 0.0-1.0, got {conf}")
    
    # evidence validation
    ev = m.get("evidence")
    if ev:
        errors.extend(validate_evidence(ev))
    
    # value-state consistency
    value = m.get("value")
    if value is None:
        # If null, should have figure_id or table_id
        if ev and not (ev.get("figure_id") or ev.get("table_id")):
            errors.append("VALUE_NULL_NO_FIG_TABLE: if value is null, provide figure_id or table_id")
    else:
        # If value exists and is numeric, quote should ideally contain it (soft)
        if isinstance(value, (int, float)) and ev:
            quote = ev.get("quote") or ""
            val_str = str(value)[:4]
            if val_str and val_str not in quote:
                errors.append(f"SOFT_QUOTE_MISMATCH: value {val_str}... not in quote")
    
    # tag enum validation
    tags = m.get("tags") or {}
    for tag_key, allowed_values in TAG_ENUMS.items():
        if tag_key in tags and tags[tag_key] not in allowed_values:
            errors.append(f"TAG_ENUM_INVALID: tags.{tag_key} must be one of {allowed_values}")
    
    return errors


def validate_extractor_output(output: Dict[str, Any]) -> Dict[str, List]:
    """
    Validate complete extractor output.
    
    Returns:
        {"hard": [...], "soft": [...]} where each is list of {index, errors}
    """
    result = {"hard": [], "soft": []}
    
    if not isinstance(output, dict):
        result["hard"].append({"index": -1, "errors": ["TOPLEVEL_NOT_OBJECT"]})
        return result
    
    if "measurements" not in output:
        result["hard"].append({"index": -1, "errors": ["MISSING_KEY:measurements"]})
        return result
    
    measurements = output.get("measurements")
    if not isinstance(measurements, list):
        result["hard"].append({"index": -1, "errors": ["MEASUREMENTS_NOT_LIST"]})
        return result
    
    for i, m in enumerate(measurements):
        errors = validate_measurement(m)
        hard = [e for e in errors if not e.startswith("SOFT_")]
        soft = [e for e in errors if e.startswith("SOFT_")]
        
        if hard:
            result["hard"].append({"index": i, "errors": hard})
        if soft:
            result["soft"].append({"index": i, "errors": soft})
    
    return result


def has_hard_errors(validation_result: Dict[str, List]) -> bool:
    """Check if validation result has any hard errors."""
    return len(validation_result.get("hard", [])) > 0
