# scripts/lib/llm_strict.py
"""
Strict LLM Wrapper with Validation and Repair Loop.

This module wraps LLM extractor calls with:
1. Schema validation after each call
2. Automatic repair loop (up to 3 attempts)
3. Error-code-based repair prompts for precise fixes
"""
from __future__ import annotations
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Core Validation (lightweight for repair loop)
# =============================================================================

def validate_extractor_output_quick(obj: Any) -> List[str]:
    """
    Quick validation for repair loop.
    Returns list of error strings.
    """
    errors = []
    
    if not isinstance(obj, dict):
        return ["TOPLEVEL_NOT_OBJECT"]
    
    if "measurements" not in obj:
        errors.append("MISSING_KEY:measurements")
        return errors
    
    if not isinstance(obj["measurements"], list):
        errors.append("MEASUREMENTS_NOT_LIST")
        return errors
    
    for i, m in enumerate(obj.get("measurements", [])):
        if not isinstance(m, dict):
            errors.append(f"[{i}] NOT_OBJECT")
            continue
        
        ev = m.get("evidence") or {}
        
        # doc validation
        if ev.get("doc") not in ("MAIN", "SUPP"):
            errors.append(f"[{i}] EVIDENCE_DOC_INVALID: must be MAIN or SUPP")
        
        # section_path
        if not ev.get("section_path"):
            errors.append(f"[{i}] EVIDENCE_SECTION_MISSING")
        
        # quote
        if not ev.get("quote"):
            errors.append(f"[{i}] EVIDENCE_QUOTE_MISSING")
        
        # anchor check
        anchors = [bool(ev.get("chunk_id")), bool(ev.get("figure_id")), bool(ev.get("table_id"))]
        if sum(anchors) == 0:
            errors.append(f"[{i}] EVIDENCE_ANCHOR_MISSING")
        
        # conditions/tags must be dicts
        if not isinstance(m.get("conditions"), dict):
            errors.append(f"[{i}] CONDITIONS_NOT_OBJECT")
        if not isinstance(m.get("tags"), dict):
            errors.append(f"[{i}] TAGS_NOT_OBJECT")
    
    return errors


# =============================================================================
# Repair Prompt Builder
# =============================================================================

REPAIR_SYSTEM_PROMPT = """You are a JSON repair engine.

You will receive:
1. ORIGINAL JSON output that has schema violations
2. VALIDATION ERRORS that need to be fixed

Fix ONLY what is necessary to satisfy the schema. Follow these rules:
- Top-level must be {"measurements": [...]}
- Each measurement must have: metric, value, unit, confidence, conditions, tags, evidence
- evidence.doc must be "MAIN" or "SUPP" (never null)
- evidence must include section_path (string) and quote (short verbatim snippet)
- evidence must have exactly ONE anchor: chunk_id OR figure_id OR table_id
- conditions and tags must be objects (even if empty {})
- Do NOT invent numbers. If number not in text, set value=null and use figure_id/table_id
- Return a SINGLE valid JSON object, no extra text or markdown
"""


def build_repair_prompt(
    original_output: Dict[str, Any],
    errors: List[str],
    evidence_catalog: Optional[str] = None
) -> str:
    """
    Build a repair prompt to fix schema violations.
    """
    parts = [
        "Fix the following JSON to comply with the schema.\n",
        "ORIGINAL OUTPUT:\n```json\n" + json.dumps(original_output, indent=2, ensure_ascii=False) + "\n```\n",
        "VALIDATION ERRORS:\n" + "\n".join(f"- {e}" for e in errors) + "\n",
    ]
    
    if evidence_catalog:
        parts.append(f"\nEVIDENCE CATALOG (use these for anchor IDs):\n{evidence_catalog}\n")
    
    parts.append("\nReturn ONLY valid JSON, no markdown code blocks.")
    
    return "".join(parts)


# =============================================================================
# Strict Extractor Wrapper
# =============================================================================

async def call_extractor_strict(
    llm_json_call: Callable,
    prompt: str,
    evidence_catalog: Optional[str] = None,
    max_retry: int = 3,
    system_prompt: Optional[str] = None
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Call LLM extractor with strict validation and repair loop.
    
    Args:
        llm_json_call: Async function that takes (prompt, system) and returns dict
        prompt: The extraction prompt
        evidence_catalog: Optional string listing available chunk_id/figure_id/table_id
        max_retry: Maximum repair attempts (default 3)
        system_prompt: Optional system prompt override
    
    Returns:
        (output_dict, remaining_errors)
    """
    # Initial call
    try:
        out = await llm_json_call(prompt, system_prompt)
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return {"measurements": []}, [f"LLM_CALL_FAILED: {e}"]
    
    # Repair loop
    for attempt in range(max_retry):
        errors = validate_extractor_output_quick(out)
        
        if not errors:
            logger.info(f"Validation passed on attempt {attempt + 1}")
            return out, []
        
        logger.warning(f"Attempt {attempt + 1}: {len(errors)} errors, attempting repair")
        
        # Build repair prompt
        repair_prompt = build_repair_prompt(out, errors, evidence_catalog)
        
        try:
            out = await llm_json_call(repair_prompt, REPAIR_SYSTEM_PROMPT)
        except Exception as e:
            logger.error(f"Repair call failed: {e}")
            # Keep previous output
    
    # Final validation
    final_errors = validate_extractor_output_quick(out)
    if final_errors:
        logger.warning(f"After {max_retry} repairs, {len(final_errors)} errors remain")
    
    return out, final_errors


def call_extractor_strict_sync(
    llm_json_call: Callable,
    prompt: str,
    evidence_catalog: Optional[str] = None,
    max_retry: int = 3,
    system_prompt: Optional[str] = None
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Synchronous version of call_extractor_strict.
    """
    # Initial call
    try:
        out = llm_json_call(prompt, system_prompt)
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return {"measurements": []}, [f"LLM_CALL_FAILED: {e}"]
    
    # Repair loop
    for attempt in range(max_retry):
        errors = validate_extractor_output_quick(out)
        
        if not errors:
            logger.info(f"Validation passed on attempt {attempt + 1}")
            return out, []
        
        logger.warning(f"Attempt {attempt + 1}: {len(errors)} errors, attempting repair")
        
        # Build repair prompt
        repair_prompt = build_repair_prompt(out, errors, evidence_catalog)
        
        try:
            out = llm_json_call(repair_prompt, REPAIR_SYSTEM_PROMPT)
        except Exception as e:
            logger.error(f"Repair call failed: {e}")
    
    # Final validation
    final_errors = validate_extractor_output_quick(out)
    return out, final_errors


# =============================================================================
# Utility: Extract with Hydration Pre-applied
# =============================================================================

def postprocess_measurements(
    measurements: List[Dict[str, Any]],
    hydrate_fn: Callable,
    normalize_fn: Callable,
    ctx: Dict[str, Any],
    paper_id: str,
    case_id: str,
    extractor_id: str
) -> List[Dict[str, Any]]:
    """
    Post-process measurements with normalization and hydration.
    
    Order: normalize → hydrate → validate
    This ensures validation runs AFTER hydration to avoid false warnings.
    """
    from scripts.contracts.measurement_contract import validate_measurement
    
    results = []
    
    for m in measurements:
        # Enrich with identifiers
        m2 = {
            "paper_id": paper_id,
            "case_id": case_id,
            **m,
            "extractor_id": extractor_id,
        }
        
        # 1. Normalize
        m2 = normalize_fn(m2)
        if not m2:
            continue
        
        # 2. Hydrate evidence BEFORE validation
        m2 = hydrate_fn(m2, ctx)
        
        # 3. Validate
        errors = validate_measurement(m2)
        if errors:
            m2["_validation_errors"] = errors
        
        results.append(m2)
    
    return results
