# scripts/lib/repair_measurement.py
# -*- coding: utf-8 -*-
"""
Individual Measurement Repair Module

Repairs individual measurements that fail strict validation.
Uses targeted LLM calls to fix only the problematic measurement.

This is more efficient than re-running the entire extraction.
"""

import json
from typing import Any, Dict, List, Tuple, Callable

from scripts.lib.schema import validate_measurement_strict
from scripts.lib.contracts import MEASUREMENT_SCHEMA_CONTRACT


def build_measurement_repair_prompt(
    bad_m: Dict[str, Any],
    errors: List[str],
    evidence_context: Dict[str, Any],
    candidate_conditions: Dict[str, Any]
) -> str:
    """
    Build targeted repair prompt for single measurement.
    
    Args:
        bad_m: The measurement that failed validation
        errors: List of validation error strings
        evidence_context: Related evidence/chunk context
        candidate_conditions: Pre-extracted candidate conditions
        
    Returns:
        Repair prompt string
    """
    error_block = "\n".join(f"- {e}" for e in errors[:10])
    
    return f"""
You are repairing ONE measurement JSON object.

## ERRORS TO FIX
{error_block}

## CANDIDATE CONDITIONS (pre-extracted, use these)
{json.dumps(candidate_conditions, indent=2, ensure_ascii=False) if candidate_conditions else "{}"}

## EVIDENCE CONTEXT (ground truth)
{json.dumps(evidence_context, indent=2, ensure_ascii=False) if evidence_context else "No additional context"}

## BAD MEASUREMENT (fix this)
{json.dumps(bad_m, indent=2, ensure_ascii=False)}

## REPAIR RULES
1. Keep numeric value unless clearly wrong
2. Fill missing tags/conditions from evidence or candidate conditions
3. evidence.doc MUST be "MAIN" or "SUPP" (never null)
4. evidence.section_path is REQUIRED
5. evidence.quote MUST be >= 20 chars, real sentence (not "4a." or "Fig. 3")
6. conditions and tags MUST be objects (even if empty {{}})
7. If value is null, tags.value_status MUST be "FIGURE_DIGITIZE_REQUIRED" or "NOT_FOUND"

Return ONLY the corrected JSON object. No markdown, no explanation.
""".strip()


def repair_one_measurement(
    llm_call_fn: Callable[[str], Dict[str, Any]],
    bad_m: Dict[str, Any],
    errors: List[str],
    evidence_context: Dict[str, Any] = None,
    candidate_conditions: Dict[str, Any] = None,
    max_attempts: int = 2
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Attempt to repair a single measurement.
    
    Args:
        llm_call_fn: Function that takes prompt string and returns parsed JSON
        bad_m: The measurement that failed validation
        errors: List of validation error strings
        evidence_context: Related evidence/chunk context
        candidate_conditions: Pre-extracted candidate conditions
        max_attempts: Maximum repair attempts
        
    Returns:
        Tuple of (repaired_measurement, remaining_errors)
    """
    current_m = bad_m
    current_errors = errors
    
    for attempt in range(max_attempts):
        prompt = build_measurement_repair_prompt(
            current_m, 
            current_errors,
            evidence_context or {},
            candidate_conditions or {}
        )
        
        try:
            fixed = llm_call_fn(prompt)
            
            # Validate the fixed measurement
            if not isinstance(fixed, dict):
                current_errors = ["REPAIR_RETURNED_NON_DICT"]
                continue
                
            new_errors = validate_measurement_strict(fixed)
            
            if not new_errors:
                # Success!
                return fixed, []
            
            # Update for next attempt
            current_m = fixed
            current_errors = new_errors
            
        except Exception as e:
            return current_m, current_errors + [f"REPAIR_FAILED: {str(e)}"]
    
    # Max attempts reached, return with remaining errors
    return current_m, current_errors


def repair_measurements_batch(
    llm_call_fn: Callable[[str], Dict[str, Any]],
    measurements: List[Dict[str, Any]],
    evidence_context: Dict[str, Any] = None,
    candidate_conditions: Dict[str, Any] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Repair a batch of measurements.
    
    Args:
        llm_call_fn: Function that takes prompt string and returns parsed JSON
        measurements: List of measurements to repair
        evidence_context: Related evidence/chunk context
        candidate_conditions: Pre-extracted candidate conditions
        
    Returns:
        Tuple of (repaired_measurements, failed_measurements)
    """
    repaired = []
    failed = []
    
    for m in measurements:
        errors = validate_measurement_strict(m)
        
        if not errors:
            # Already valid
            repaired.append(m)
            continue
        
        # Try to repair
        fixed_m, remaining_errors = repair_one_measurement(
            llm_call_fn=llm_call_fn,
            bad_m=m,
            errors=errors,
            evidence_context=evidence_context,
            candidate_conditions=candidate_conditions
        )
        
        if remaining_errors:
            # Still has errors, add to failed list
            fixed_m["_repair_errors"] = remaining_errors
            failed.append(fixed_m)
        else:
            repaired.append(fixed_m)
    
    return repaired, failed
