# scripts/lib/contract_utils.py
"""
Contract v1.1 Utilities - Normalize, Validate, and Build Repair Hints

This module provides:
- normalize_stage4_output(): Fix LLM output structure (list wrapping, etc.)
- validate_stage4_output(): Strict validation against contract
- build_contract_text(): Generate contract text for prompt injection
- build_correction_hint(): Generate repair hints for self-correction loop
- Multi-value guards to prevent data loss
- Series structure validation

Reference: 12_설계.md + 13_설계.md Contract v1.1
"""
from __future__ import annotations
from typing import Any, Dict, List, Tuple
import json
import math
import re

from .contracts import (
    METRIC_REGISTRY, 
    ALLOWED_EVIDENCE_DOC,  
    CURRENT_TYPE_SLOTS,
    BAD_QUOTE_PATTERNS,
    KEY_ALIAS
)

# Marker patterns that indicate placeholder quotes
PLACEHOLDER_MARKERS = [
    "Value extracted",
    "UNKNOWN",
    "TBD",
    "Digitized from",
    "See figure",
    "From table",
]


def _is_placeholder_quote(q: str) -> bool:
    """Check if quote is a placeholder (not real evidence)."""
    if not isinstance(q, str):
        return True
    s = q.strip()
    if len(s) < 20:
        return True
    
    # Check for placeholder markers
    for marker in PLACEHOLDER_MARKERS:
        if marker.lower() in s.lower():
            return True
    
    # Check for bad quote patterns
    for pattern in BAD_QUOTE_PATTERNS:
        if re.search(pattern, s, re.IGNORECASE):
            return True
    
    return False


def build_contract_text(task_name: str = "") -> str:
    """
    Build contract text for prompt injection.
    
    Args:
        task_name: Optional task name for task-specific contract rules
        
    Returns:
        Contract text string for insertion into prompts
    """
    return (
        "OUTPUT CONTRACT (MUST FOLLOW)\n"
        "1) Output MUST be a SINGLE valid JSON object.\n"
        "2) Top-level keys MUST be: measurements (list), digitize_tasks (list), warnings (list).\n"
        "3) For each measurement: metric,value,unit,confidence,evidence,conditions,tags are required.\n"
        "4) evidence.doc MUST be 'MAIN' or 'SUPP' (never null). evidence.section_path and evidence.quote are required.\n"
        "5) Do NOT output null value. If numeric value is not explicitly stated in text, create a digitize_tasks item instead.\n"
        "6) evidence.quote must be actual text from paper (NOT placeholder like 'Value extracted from...').\n"
        "7) At least one of anchor_id, chunk_id, figure_id, or table_id must be present for traceability.\n"
    )


def normalize_stage4_output(obj: Any, task_name: str = "") -> Dict[str, Any]:
    """
    Normalize LLM output to ensure correct structure.
    
    Handles cases where LLM returns:
    - A list instead of dict
    - A single measurement dict
    - Missing top-level keys
    
    Args:
        obj: Raw LLM output
        task_name: Task name for logging
        
    Returns:
        Normalized dict with measurements/digitize_tasks/warnings keys
    """
    # LLM returned a list - wrap it
    if isinstance(obj, list):
        return {
            "measurements": obj, 
            "digitize_tasks": [], 
            "warnings": ["wrapped_list_output"]
        }
    
    if isinstance(obj, dict):
        out = dict(obj)
        out.setdefault("measurements", [])
        out.setdefault("digitize_tasks", [])
        out.setdefault("warnings", [])
        
        # Check if this is a single measurement masquerading as top-level
        if "metric" in out and "value" in out and isinstance(out.get("evidence"), dict):
            return {
                "measurements": [out], 
                "digitize_tasks": [], 
                "warnings": ["wrapped_single_measurement"]
            }
        
        return out
    
    # Unrecognized type
    return {
        "measurements": [], 
        "digitize_tasks": [], 
        "warnings": [f"invalid_top_level_type:{type(obj).__name__}"]
    }


def fill_required_keys(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure required keys exist in measurement with default values.
    
    Args:
        m: Measurement dict
        
    Returns:
        Measurement dict with all required keys
    """
    m = dict(m)
    m.setdefault("tags", {})
    m.setdefault("conditions", {})
    m.setdefault("evidence", {})
    
    # Ensure None values are replaced with empty dicts
    if m["tags"] is None: 
        m["tags"] = {}
    if m["conditions"] is None: 
        m["conditions"] = {}
    if m["evidence"] is None: 
        m["evidence"] = {}
    
    m.setdefault("unit", None)
    m.setdefault("confidence", 0.5)
    
    return m


def apply_metric_contract(m: Dict[str, Any], task_name: str = "") -> Dict[str, Any]:
    """
    Apply metric-specific contract rules.
    
    Currently just ensures required keys exist.
    Actual validation happens in validate_stage4_output.
    
    Args:
        m: Measurement dict
        task_name: Task name
        
    Returns:
        Processed measurement dict
    """
    m = fill_required_keys(m)
    return m


def _validate_evidence(e: Dict[str, Any]) -> List[str]:
    """
    Validate evidence dict against strict rules.
    
    Args:
        e: Evidence dict
        
    Returns:
        List of error messages (empty if valid)
    """
    errs = []
    
    # doc must be MAIN or SUPP
    doc = e.get("doc")
    if doc not in ALLOWED_EVIDENCE_DOC:
        errs.append(f"evidence.doc must be MAIN or SUPP (got: {doc})")
    
    # section_path must exist and be non-empty
    section_path = e.get("section_path")
    if not section_path or (isinstance(section_path, str) and not section_path.strip()):
        errs.append("evidence.section_path missing or empty")
    
    # quote quality check
    q = e.get("quote")
    if _is_placeholder_quote(q):
        errs.append("evidence.quote missing or placeholder (must be actual text from paper)")
    
    # traceability anchor - at least one required
    if not (e.get("anchor_id") or e.get("chunk_id") or e.get("figure_id") or e.get("table_id")):
        errs.append("evidence.trace_id missing (need anchor_id/chunk_id/figure_id/table_id)")
    
    return errs


def _validate_value_type(metric: str, v: Any) -> List[str]:
    """
    Validate value type for metric.
    
    Args:
        metric: Metric name
        v: Value
        
    Returns:
        List of error messages (empty if valid)
    """
    spec = METRIC_REGISTRY.get(metric, {})
    errs = []
    
    #  null value check
    if v is None:
        errs.append("value is null (use digitize_tasks instead)")
        return errs
    
    # Basic type validation
    # Most metrics expect number
    if not isinstance(v, (int, float, str, list)):
        errs.append(f"value has invalid type: {type(v).__name__}")
        return errs
    
    # NaN check for floats
    if isinstance(v, float) and math.isnan(v):
        errs.append("value is NaN")
    
    # Boolean masquerading as int
    if isinstance(v, bool):
        errs.append("value cannot be boolean")
    
    return errs


def _validate_conditions_tags(metric: str, m: Dict[str, Any]) -> List[str]:
    """
    Validate required conditions and tags for metric.
    
    Args:
        metric: Metric name
        m: Measurement dict
        
    Returns:
        List of error messages (empty if valid)
    """
    spec = METRIC_REGISTRY.get(metric, {})
    cond = m.get("conditions") or {}
    tags = m.get("tags") or {}
    errs = []
    
    # Validate required tags
    req_tags = spec.get("required_tags", {})
    for tag_key, allowed_values in req_tags.items():
        tag_val = tags.get(tag_key)
        
        if tag_val in (None, "", []):
            errs.append(f"missing required tag: {tag_key}")
        elif allowed_values and tag_val not in allowed_values:
            errs.append(f"invalid tag {tag_key}: '{tag_val}' not in {allowed_values}")
    
    # Validate required conditions
    req_cond = spec.get("required_conditions", {})
    for cond_key, cond_type in req_cond.items():
        cond_val = cond.get(cond_key)
        
        # Allow null for flexible conditions
        if "null" in str(cond_type):
            continue
        
        if cond_val in (None, "", []):
            errs.append(f"missing required condition: {cond_key}")
    
    return errs


def _count_numbers_in_quote(quote: str) -> int:
    """Count numeric values in quote (for multi-value detection)."""
    nums = re.findall(r"[-+]?\d+\.\d+|[-+]?\d+", quote or "")
    return len(nums)


def _validate_multi_value(metric: str, m: Dict[str, Any]) -> List[str]:
    """
    Check if quote contains multiple values that should be split.
    
    Per 13_설계.md: prevent data loss from multi-value extraction.
    
    Args:
        metric: Metric name
        m: Measurement dict
        
    Returns:
        List of error messages
    """
    errs = []
    
    # DFT adsorption energy often has multiple values
    if metric == "zn_adsorption_energy_eV":
        q = (m.get("evidence") or {}).get("quote") or ""
        num_count = _count_numbers_in_quote(q)
        
        if num_count >= 3:
            # Quote has 3+ numbers but single measurement
            # This likely means multiple adsorption sites
            has_split_tag = (m.get("tags") or {}).get("multi_value_split")
            if not has_split_tag:
                errs.append(
                    "quote contains multiple energies -> must split into separate measurements "
                    "(set tags.multi_value_split=true for each, with different tags.adsorption_site)"
                )
    
    return errs


def _validate_series_structure(metric: str, m: Dict[str, Any]) -> List[str]:
    """
    Validate series data structure.
    
    Per 13_설계.md: series should be objects with x/y values, not bare lists.
    
    Args:
        metric: Metric name
        m: Measurement dict
        
    Returns:
        List of error messages
    """
    errs = []
    
    # Overpotential series need proper x-y mapping
    if metric == "overpotential_mV":
        v = m.get("value")
        if isinstance(v, list) and len(v) > 1:
            # This looks like a series but is a bare list
            errs.append(
                "overpotential series must be object with x_values/y_values, not bare list "
                "(prevents x-y mapping loss)"
            )
    
    return errs


def validate_stage4_output(stage4: Dict[str, Any], task_name: str = "") -> Tuple[bool, List[str]]:
    """
    Validate Stage4 output against contract.
    
    Args:
        stage4: Stage4 output dict
        task_name: Task name for task-specific rules
        
    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errs: List[str] = []
    
    # Check top-level structure
    if not isinstance(stage4.get("measurements"), list):
        errs.append("top.measurements must be list")
        return False, errs
    
    if not isinstance(stage4.get("digitize_tasks"), list):
        # This is a warning, not a hard error
        if "digitize_tasks" not in stage4:
            stage4["digitize_tasks"] = []
    
    # Validate each measurement
    for i, raw_m in enumerate(stage4["measurements"]):
        if not isinstance(raw_m, dict):
            errs.append(f"m[{i}] not dict (got {type(raw_m).__name__})")
            continue
        
        m = fill_required_keys(raw_m)
        metric = m.get("metric")
        
        if not metric:
            errs.append(f"m[{i}].metric missing")
            continue
        
        # Validate value type
        val_errs = _validate_value_type(metric, m.get("value"))
        errs += [f"m[{i}].{e}" for e in val_errs]
        
        # Validate evidence
        ev_errs = _validate_evidence(m.get("evidence") or {})
        errs += [f"m[{i}].{e}" for e in ev_errs]
        
        # Validate conditions/tags
        ct_errs = _validate_conditions_tags(metric, m)
        errs += [f"m[{i}].{e}" for e in ct_errs]
        
        # 13_설계: Multi-value guard
        mv_errs = _validate_multi_value(metric, m)
        errs += [f"m[{i}].{e}" for e in mv_errs]
        
        # 13_설계: Series structure guard
        series_errs = _validate_series_structure(metric, m)
        errs += [f"m[{i}].{e}" for e in series_errs]
    
    return (len(errs) == 0), errs


def build_correction_hint(validation_errors: List[str], task_name: str = "") -> str:
    """
    Build correction hint for LLM self-repair.
    
    Args:
        validation_errors: List of validation errors
        task_name: Task name
        
    Returns:
        Correction hint string for prompt injection
    """
    head = [
        "SELF-CORRECTION REQUIRED.",
        "Fix the JSON to satisfy the OUTPUT CONTRACT exactly.",
        "Do not add any explanation text outside JSON.",
        "",
        "Key rules:",
        "- evidence.doc must be MAIN or SUPP",
        "- evidence.section_path and evidence.quote must be present",
        "- evidence.quote must be actual text from paper (NOT placeholder)",
        "- value must not be null; if numeric not in text, move to digitize_tasks",
        "- fill required conditions/tags for each metric",
        "",
        "VALIDATION ERRORS:",
    ]
    
    # Limit errors to prevent overwhelming LLM
    body = validation_errors[:60]
    
    tail = [
        "",
        "Return ONLY corrected JSON object with keys: measurements, digitize_tasks, warnings."
    ]
    
    return "\n".join(head + body + tail)
