# scripts/lib/schema_enforcer.py
"""
Schema Enforcer v1.0 - Condition Canonicalization & Evidence Validation

This module provides:
- Condition key alias resolution
- Placeholder quote detection
- Current-type slot conflict detection
- Evidence anchor/figure validation

Reference: 11_설계 Schema Enforcer
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import re


# ============================================================================
# Placeholder Quote Detection
# ============================================================================

PLACEHOLDER_PATTERNS = [
    r"Value extracted from fig",
    r"Value extracted",
    r"provided evidence context",
    r"UNKNOWN",
    r"see figure",
    r"digitized",
    r"^fig\s*\d",
    r"^table\s*\d",
    r"^refer to",
    r"^data from",
    r"^from table",
]


def is_placeholder_quote(quote: Optional[str]) -> bool:
    """
    Check if a quote is a placeholder rather than real evidence.
    Returns True if quote should be rejected.
    """
    if not quote or not isinstance(quote, str):
        return True
    
    q = quote.strip()
    
    # Too short to be meaningful
    if len(q) < 15:
        return True
    
    # Check placeholder patterns
    for pattern in PLACEHOLDER_PATTERNS:
        if re.search(pattern, q, flags=re.IGNORECASE):
            return True
    
    return False


# ============================================================================
# Current-Type Slot Management
# ============================================================================

CURRENT_SLOT_KEYS = [
    "areal_current_density_mA_cm2",
    "specific_current_A_g",
    "rate_C"
]


def count_non_null_current_slots(conditions: Dict[str, Any]) -> int:
    """Count how many current-type slots have non-null values."""
    count = 0
    for key in CURRENT_SLOT_KEYS:
        if conditions.get(key) is not None:
            count += 1
    return count


def get_non_null_current_slots(conditions: Dict[str, Any]) -> List[str]:
    """Get list of current-type slots that have non-null values."""
    return [key for key in CURRENT_SLOT_KEYS if conditions.get(key) is not None]


# ============================================================================
# Condition Key Canonicalization
# ============================================================================

# Map common aliases to canonical keys
CONDITION_ALIASES = {
    # Current density variations
    "current_density_mA_cm2": "areal_current_density_mA_cm2",
    "current_density_mA_cm^2": "areal_current_density_mA_cm2",
    "current_density_mAcm2": "areal_current_density_mA_cm2",
    "current_density_mAcm-2": "areal_current_density_mA_cm2",
    "current_density": "areal_current_density_mA_cm2",
    "areal_current_density": "areal_current_density_mA_cm2",
    
    # Specific current variations
    "specific_current": "specific_current_A_g",
    "specific_current_mA_g": "specific_current_A_g",
    
    # Temperature variations
    "temperature": "temperature_C",
    "temperature_c": "temperature_C",
    "temp": "temperature_C",
    "temp_C": "temperature_C",
    
    # Capacity variations
    "areal_capacity": "areal_capacity_mAh_cm2",
    "areal_capacity_mAhcm2": "areal_capacity_mAh_cm2",
    "areal_capacity_mAhcm-2": "areal_capacity_mAh_cm2",
    
    # C-rate variations
    "c_rate": "rate_C",
    "C_rate": "rate_C",
    "c-rate": "rate_C",
}


def canonicalize_conditions(conditions: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Canonicalize condition keys using CONDITION_ALIASES.
    
    Returns a new dict with canonical keys.
    """
    if conditions is None:
        return {}
    
    if not isinstance(conditions, dict):
        return {}
    
    result = {}
    for key, value in conditions.items():
        canonical_key = CONDITION_ALIASES.get(key, key)
        # If canonical key already exists, prefer non-null value
        if canonical_key in result:
            if result[canonical_key] is None and value is not None:
                result[canonical_key] = value
        else:
            result[canonical_key] = value
    
    return result


# ============================================================================
# Evidence Validation Helpers
# ============================================================================

def evidence_has_anchor_or_chunk(evidence: Dict[str, Any]) -> bool:
    """Check if evidence has anchor_id or chunk_id for traceability."""
    return bool(evidence.get("anchor_id") or evidence.get("chunk_id"))


def evidence_has_figure_or_table(evidence: Dict[str, Any]) -> bool:
    """Check if evidence has figure_id or table_id."""
    return bool(evidence.get("figure_id") or evidence.get("table_id"))


def evidence_doc_valid(evidence: Dict[str, Any]) -> bool:
    """Check if evidence.doc is valid (MAIN or SUPP)."""
    return evidence.get("doc") in {"MAIN", "SUPP"}


def evidence_has_section_path(evidence: Dict[str, Any]) -> bool:
    """Check if evidence has a non-empty section_path."""
    section_path = evidence.get("section_path")
    return bool(section_path and isinstance(section_path, str) and section_path.strip())


# ============================================================================
# Measurement Normalization
# ============================================================================

def ensure_keys_exist(obj: Dict[str, Any], keys: List[str], default: Any = None) -> None:
    """Ensure all specified keys exist in dict, setting default if missing."""
    for key in keys:
        if key not in obj:
            obj[key] = default


def normalize_measurement(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a measurement dict to ensure all required keys exist.
    
    - Ensures top-level keys: metric, value, unit, confidence, conditions, tags, evidence
    - Canonicalizes condition keys
    - Initializes missing nested dicts
    """
    if not isinstance(m, dict):
        return {}
    
    m = dict(m)  # Don't mutate original
    
    # Ensure top-level keys
    top_level_keys = ["metric", "value", "unit", "confidence", "conditions", "tags", "evidence"]
    for key in top_level_keys:
        if key not in m:
            if key in ("conditions", "tags", "evidence"):
                m[key] = {}
            else:
                m[key] = None
    
    # Canonicalize conditions
    m["conditions"] = canonicalize_conditions(m.get("conditions"))
    
    # Ensure tags is a dict
    if not isinstance(m.get("tags"), dict):
        m["tags"] = {}
    
    # Ensure evidence is a dict
    if not isinstance(m.get("evidence"), dict):
        m["evidence"] = {}
    
    return m


# ============================================================================
# Value Status Enforcement
# ============================================================================

ALLOWED_VALUE_STATUS = {
    "OK",
    "FIGURE_DIGITIZE_REQUIRED",
    "NOT_FOUND",
    "OK_UNIT_UNCLEAR",
    "OK_CONTEXT_UNCLEAR",
}


def enforce_null_value_status(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enforce value_status when value is null.
    
    If value is null and value_status is missing, infer based on evidence:
    - Has figure_id -> FIGURE_DIGITIZE_REQUIRED
    - Otherwise -> NOT_FOUND
    """
    m = dict(m)
    tags = dict(m.get("tags") or {})
    evidence = m.get("evidence") or {}
    
    value = m.get("value")
    
    if value is None:
        if "value_status" not in tags or tags.get("value_status") is None:
            # Infer based on evidence
            if evidence.get("figure_id"):
                tags["value_status"] = "FIGURE_DIGITIZE_REQUIRED"
            else:
                tags["value_status"] = "NOT_FOUND"
        
        # Validate value_status is in allowed set
        if tags.get("value_status") not in ALLOWED_VALUE_STATUS:
            tags["value_status"] = "NOT_FOUND"
    
    m["tags"] = tags
    return m
