# scripts/lib/validator_strict.py
"""
Validator Strict v1.0 - Registry-based Strict Validation with Issue Tracking

This module provides:
- Issue dataclass for structured error/warning reporting
- validate_measurement_strict(): registry-based validation with JSONPath errors
- build_correction_hint(): generates LLM-friendly fix instructions

Reference: 11_설계 Validator Strict
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from scripts.lib.metric_registry import METRIC_REGISTRY, MetricSpec
from scripts.lib.schema_enforcer import (
    normalize_measurement,
    count_non_null_current_slots,
    get_non_null_current_slots,
    evidence_has_anchor_or_chunk,
    evidence_has_figure_or_table,
    evidence_doc_valid,
    evidence_has_section_path,
    is_placeholder_quote,
    enforce_null_value_status,
    ALLOWED_VALUE_STATUS,
)


# ============================================================================
# Issue Dataclass
# ============================================================================

@dataclass
class Issue:
    """Structured validation issue with severity, code, path, message, and fix hint."""
    severity: str   # "ERROR" | "WARN"
    code: str       # Machine-readable code like "MISSING_METRIC"
    path: str       # JSONPath-like location: "$.metric", "$.conditions.temperature_C"
    message: str    # Human-readable description
    fix_hint: str   # Actionable fix instruction for LLM
    
    def __str__(self) -> str:
        return f"[{self.severity}] {self.code} at {self.path}: {self.message}"
    
    def to_dict(self) -> Dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "fix_hint": self.fix_hint,
        }


# ============================================================================
# Strict Measurement Validator
# ============================================================================

def validate_measurement_strict(
    m: Dict[str, Any],
    extractor_name: str = None
) -> Tuple[Dict[str, Any], List[Issue]]:
    """
    Validate a measurement against METRIC_REGISTRY with strict rules.
    
    Returns:
        Tuple of (normalized_measurement, list_of_issues)
        
    Strict ERROR rules:
    1. Metric missing or not a string
    2. Metric contains forbidden pattern (|)
    3. Metric not in registry
    4. Required condition key missing (null value OK)
    5. Current-type slot conflict (>1 non-null)
    6. Null value without value_status
    7. Invalid evidence.doc
    8. Missing evidence.section_path
    9. Placeholder quote
    10. Missing anchor_id or chunk_id
    """
    issues: List[Issue] = []
    
    # Normalize measurement first
    m = normalize_measurement(m)
    
    metric = m.get("metric")
    
    # Rule 1: Metric must exist and be a string
    if not isinstance(metric, str) or not metric:
        issues.append(Issue(
            severity="ERROR",
            code="MISSING_METRIC",
            path="$.metric",
            message="metric is missing or not a string",
            fix_hint="Set metric to one of the allowed values from registry."
        ))
        return m, issues  # Can't continue without metric
    
    # Rule 2: Forbid patterns like "|"
    if "|" in metric:
        issues.append(Issue(
            severity="ERROR",
            code="FORBIDDEN_METRIC_PATTERN",
            path="$.metric",
            message=f"metric contains forbidden character '|': {metric}",
            fix_hint="Use a single registered metric name; do not combine metrics with '|'."
        ))
        return m, issues  # Invalid metric name
    
    # Rule 3: Metric must be in registry
    spec: Optional[MetricSpec] = METRIC_REGISTRY.get(metric)
    if spec is None:
        issues.append(Issue(
            severity="ERROR",
            code="METRIC_NOT_IN_REGISTRY",
            path="$.metric",
            message=f"metric not allowed: {metric}",
            fix_hint="Choose one of the registered metrics for this extractor."
        ))
        # Continue with other validations even if metric unknown
    
    # Get conditions and tags
    conditions = m.get("conditions") or {}
    tags = m.get("tags") or {}
    evidence = m.get("evidence") or {}
    value = m.get("value")
    
    # Rule 4: Required condition keys must exist (null value OK)
    if spec:
        for req_key in spec.required_conditions:
            if req_key not in conditions:
                issues.append(Issue(
                    severity="ERROR",
                    code="MISSING_CONDITION_KEY",
                    path=f"$.conditions.{req_key}",
                    message=f"required condition key missing: {req_key}",
                    fix_hint=f"Add conditions.{req_key} with value or null if unknown."
                ))
    
    # Rule 5: Current-type slot mutual exclusivity
    non_null_slots = get_non_null_current_slots(conditions)
    if len(non_null_slots) >= 2:
        issues.append(Issue(
            severity="ERROR",
            code="CURRENT_SLOT_CONFLICT",
            path="$.conditions",
            message=f"multiple current slots filled: {non_null_slots}",
            fix_hint="Keep only ONE of areal_current_density_mA_cm2, specific_current_A_g, rate_C; set others to null."
        ))
    
    # Rule 6: Null value requires value_status
    if value is None:
        if tags.get("value_status") is None:
            issues.append(Issue(
                severity="ERROR",
                code="NULL_VALUE_NEEDS_STATUS",
                path="$.tags.value_status",
                message="value is null but value_status is missing",
                fix_hint="Set tags.value_status to 'FIGURE_DIGITIZE_REQUIRED' or 'NOT_FOUND'."
            ))
        elif tags.get("value_status") not in ALLOWED_VALUE_STATUS:
            issues.append(Issue(
                severity="ERROR",
                code="INVALID_VALUE_STATUS",
                path="$.tags.value_status",
                message=f"invalid value_status: {tags.get('value_status')}",
                fix_hint=f"Use one of: {', '.join(sorted(ALLOWED_VALUE_STATUS))}"
            ))
    
    # Rule 7: Evidence.doc must be MAIN or SUPP
    if not evidence_doc_valid(evidence):
        issues.append(Issue(
            severity="ERROR",
            code="INVALID_EVIDENCE_DOC",
            path="$.evidence.doc",
            message=f"invalid evidence.doc: {evidence.get('doc')}",
            fix_hint="Set evidence.doc to 'MAIN' or 'SUPP'."
        ))
    
    # Rule 8: Evidence.section_path must exist
    if not evidence_has_section_path(evidence):
        issues.append(Issue(
            severity="ERROR",
            code="MISSING_SECTION_PATH",
            path="$.evidence.section_path",
            message="evidence.section_path is missing or empty",
            fix_hint="Provide the actual section heading from the paper."
        ))
    
    # Rule 9: Placeholder quote detection
    if is_placeholder_quote(evidence.get("quote")):
        issues.append(Issue(
            severity="ERROR",
            code="PLACEHOLDER_QUOTE",
            path="$.evidence.quote",
            message="evidence.quote is placeholder or too short",
            fix_hint="Quote must be an actual sentence from the paper text, not a placeholder."
        ))
    
    # Rule 10: Must have anchor_id or chunk_id for traceability
    if not evidence_has_anchor_or_chunk(evidence):
        issues.append(Issue(
            severity="ERROR",
            code="MISSING_TRACE_ANCHOR",
            path="$.evidence",
            message="missing both anchor_id and chunk_id",
            fix_hint="Provide anchor_id or chunk_id for traceability."
        ))
    
    # Optional: Check if spec requires figure/table evidence
    if spec and "(figure_id|table_id)" in spec.evidence_requires:
        if not evidence_has_figure_or_table(evidence):
            issues.append(Issue(
                severity="WARN",
                code="MISSING_FIG_TABLE_ID",
                path="$.evidence",
                message="metric typically requires figure_id or table_id",
                fix_hint="Provide figure_id or table_id if value is from figure/table."
            ))
    
    # Optional: Check for unknown tag keys (WARN only)
    if spec:
        allowed_tags = set(spec.allowed_tags) | {"value_status"}
        for tag_key in tags.keys():
            if tag_key not in allowed_tags and not tag_key.startswith("_"):
                issues.append(Issue(
                    severity="WARN",
                    code="UNKNOWN_TAG_KEY",
                    path=f"$.tags.{tag_key}",
                    message=f"tag key not in allowed set: {tag_key}",
                    fix_hint=f"Use one of: {', '.join(sorted(allowed_tags))}"
                ))
    
    # P1-4: Evidence-Value Validation
    # Check if extracted value appears in the quote (hallucination detection)
    if value is not None:
        quote = (evidence.get("quote") or "").lower()
        value_str = str(value).lower()
        
        # For numeric values, check if the number appears in quote
        # Allow some flexibility (e.g., "150.0" matches "150")
        value_found = False
        try:
            num_val = float(value)
            # Try exact match
            if value_str in quote:
                value_found = True
            # Try integer match for floats like 150.0 -> "150"
            elif str(int(num_val)) in quote:
                value_found = True
            # Try scientific notation match
            elif f"{num_val:.2e}".lower() in quote:
                value_found = True
        except (ValueError, TypeError):
            # Non-numeric value - just check string presence
            if value_str in quote:
                value_found = True
        
        if not value_found and len(quote) > 20:
            issues.append(Issue(
                severity="WARN",
                code="EVIDENCE_VALUE_MISMATCH",
                path="$.evidence.quote",
                message=f"value '{value}' not found in quote - possible hallucination",
                fix_hint="Verify quote contains the extracted value or correct the quote."
            ))
            # Add suspect flag to tags
            if "tags" not in m:
                m["tags"] = {}
            m["tags"]["_evidence_suspect"] = True
    
    return m, issues


# ============================================================================
# Batch Validation
# ============================================================================

def validate_measurements_batch(
    measurements: List[Dict[str, Any]],
    extractor_name: str = None
) -> Tuple[List[Dict[str, Any]], Dict[int, List[Issue]]]:
    """
    Validate a batch of measurements.
    
    Returns:
        Tuple of (normalized_measurements, issues_by_index)
    """
    normalized = []
    issues_by_index = {}
    
    for i, m in enumerate(measurements):
        norm_m, issues = validate_measurement_strict(m, extractor_name)
        normalized.append(norm_m)
        if issues:
            issues_by_index[i] = issues
    
    return normalized, issues_by_index


def count_errors(issues: List[Issue]) -> int:
    """Count issues with severity ERROR."""
    return sum(1 for issue in issues if issue.severity == "ERROR")


def has_errors(issues: List[Issue]) -> bool:
    """Check if any issues have severity ERROR."""
    return any(issue.severity == "ERROR" for issue in issues)


def get_errors_only(issues: List[Issue]) -> List[Issue]:
    """Filter to only ERROR severity issues."""
    return [issue for issue in issues if issue.severity == "ERROR"]


# ============================================================================
# Correction Hint Builder
# ============================================================================

def build_correction_hint(issues: List[Issue], max_issues: int = 12) -> str:
    """
    Build a correction hint for LLM from validation issues.
    
    Returns a formatted string with fix instructions using JSONPath.
    """
    if not issues:
        return ""
    
    # Prioritize ERRORs over WARNs
    errors = get_errors_only(issues)
    if errors:
        issues_to_show = errors[:max_issues]
    else:
        issues_to_show = issues[:max_issues]
    
    lines = [
        "Your previous output violated the schema. You MUST fix these issues:",
        ""
    ]
    
    for i, issue in enumerate(issues_to_show, 1):
        lines.append(f"{i}. [{issue.severity}] {issue.path}: {issue.code}")
        lines.append(f"   FIX: {issue.fix_hint}")
    
    lines.extend([
        "",
        "Return ONLY valid JSON with the same structure.",
        "Do NOT omit any required keys. Use null if value is unknown."
    ])
    
    return "\n".join(lines)


def build_correction_hint_compact(issues: List[Issue], max_issues: int = 8) -> str:
    """
    Build a compact single-line correction hint for LLM.
    """
    if not issues:
        return ""
    
    errors = get_errors_only(issues)[:max_issues]
    
    hints = []
    for issue in errors:
        hints.append(f"Fix {issue.path}: {issue.fix_hint}")
    
    return " | ".join(hints)


# ============================================================================
# Safe Failure Handler
# ============================================================================

def create_safe_failure_measurement(
    original: Dict[str, Any],
    reason: str = "validation_failed"
) -> Dict[str, Any]:
    """
    Create a safe failure measurement when repair fails.
    
    Sets value=null with value_status=NOT_FOUND to prevent data loss
    while marking the measurement as requiring manual review.
    """
    m = normalize_measurement(original)
    
    # Set value to null with proper status
    m["value"] = None
    
    tags = dict(m.get("tags") or {})
    tags["value_status"] = "NOT_FOUND"
    tags["_repair_failed"] = reason
    m["tags"] = tags
    
    # Ensure minimum evidence
    evidence = dict(m.get("evidence") or {})
    if not evidence.get("doc"):
        evidence["doc"] = "MAIN"
    if not evidence.get("section_path"):
        evidence["section_path"] = "UNKNOWN"
    m["evidence"] = evidence
    
    return m
