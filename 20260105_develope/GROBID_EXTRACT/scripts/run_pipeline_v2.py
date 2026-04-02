# scripts/run_pipeline_v2.py
"""
Enterprise-Grade AZIB Extraction Pipeline v2.0

Features:
1. Multi-pass extraction with cross-reference linking
2. Context aggregation before LLM calls
3. Strict schema validation with self-correction
4. Table Agent integration
5. Comprehensive error handling and retry logic
6. Progress tracking and detailed logging

Usage:
    $env:MODEL_SMALL = "gpt-4o-mini"
    $env:MODEL_MID = "gpt-4o"  
    $env:MODEL_LARGE = "gpt-4o"
    python scripts/run_pipeline_v2.py --run-dir runs/run_001 --data-root data
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.io_jsonl import read_jsonl, write_jsonl, read_json, write_json, append_jsonl
from scripts.lib.llm_client import call_llm_json, get_model_name, call_for_task, get_model_for_task
from scripts.lib.evidence_packet import build_evidence_packet
from scripts.lib.adaptive_planner import build_plan_from_inclusion
from scripts.lib.normalize_units import normalize_measurements, normalize_one
from scripts.lib.qc_rules import run_qc_checks
from scripts.lib.schema import validate_measurement, parse_llm_output, VALID_METRICS
from scripts.lib.context_linker import build_cross_references, aggregate_context_for_extraction
from scripts.lib.table_agent import extract_from_table, extract_from_table_llm, categorize_table
# === NEW Enterprise Modules ===
from scripts.lib.evidence_hydrator import hydrate_evidence, hydrate_measurements
from scripts.lib.measurement_key import organize_measurements_v2, measurement_group_key
from scripts.lib.case_builder_hybrid import mine_sample_candidates, build_cases_hybrid_sync
# === PATCH 1-3: Strict Schema Enforcement ===
from scripts.lib.contracts import MEASUREMENT_SCHEMA_CONTRACT, ALLOWED_VALUE_STATUS, METRIC_REGISTRY
from scripts.lib.schema import (
    validate_measurement_strict, validate_with_registry, fill_required_keys_with_null, safe_filter_measurements,
    # 09_설계: Contract enforcer functions
    normalize_stage4_output, normalize_measurement, apply_metric_contract,
    validate_stage4_output, build_contract_text, build_correction_hint,
    get_task_required_keys,
    # 10_설계: Contract v1.1 - canonicalization and null value enforcement
    canonicalize_conditions, enforce_null_value_rules,
    # 11_설계: METRIC_REGISTRY v1.0 - strict validation
    validate_measurement_registry, build_registry_correction_hint
)
from scripts.lib.condition_extractor import extract_candidate_conditions, format_candidates_for_prompt

# === 15_설계: NEW Enhancement Modules ===
from scripts.lib.tag_corrector import auto_correct_tags, auto_correct_measurements
from scripts.lib.metric_catalog import canonicalize_metric, is_valid_metric, is_eis_setting, VALID_METRICS as CATALOG_VALID_METRICS
from scripts.lib.eis_postprocessor import inject_eis_conditions, split_multivalue_eis
from scripts.lib.cycling_fallback import has_cycling_content, get_cycling_keywords_found, build_coverage_flag
from scripts.lib.rate_overpotential_fallback import has_rate_content, get_rate_keywords_found, has_overpotential_content, get_overpotential_keywords_found
from scripts.lib.anchor_integrity import validate_anchor_integrity, validate_measurements_anchors, count_unresolved_refs

# === Phase 5: Context Extension ===
from scripts.lib.context_extender import extend_measurement_context
from scripts.lib.experiment_context_extractor import extract_and_merge_experiment_context

# === Round 2 Quality Fixes (C1-C6) ===
from scripts.lib.multivalue_expander import expand_and_enrich_measurements

# === Round 4 Quality Fixes (Unit-Binding QC) ===
from scripts.lib.unit_binding_validator import validate_measurements_batch as qc_validate_batch

# === 16_설계: LLM Tracing ===
from scripts.lib.llm_trace import TraceWriter, TraceContext, init_trace_writer, set_global_writer

# 11_설계 v2: Strict Validator with Issue tracking
try:
    from scripts.lib.validator_strict import (
        validate_measurement_strict as validate_strict_v2,
        validate_measurements_batch,
        build_correction_hint as build_correction_hint_v2,
        has_errors,
        get_errors_only,
        create_safe_failure_measurement,
        Issue,
    )
    STRICT_VALIDATOR_AVAILABLE = True
except ImportError:
    STRICT_VALIDATOR_AVAILABLE = False

# ============================================================================
# Logging Setup
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# ============================================================================
# Pipeline Configuration
# ============================================================================
class PipelineConfig:
    """Pipeline configuration."""
    MAX_RETRIES = 3
    SELF_CORRECTION_ENABLED = True
    VALIDATE_OUTPUT = True
    TABLE_AGENT_ENABLED = True
    CROSS_REF_LINKING = True
    
    # LLM settings
    TEMPERATURE_LOW = 0.1
    TEMPERATURE_MID = 0.3
    
    # Task priorities
    PRIORITIES = {
        "CHUNK_CATEGORIZE": 90,
        "CHUNK_INCLUDE": 85,
        "BUILD_CASES": 80,
        "BUILD_PLAN": 75,
        "TABLE_EXTRACT": 70,
        "EXTRACT_INPUT": 65,
        "EXTRACT_CYCLING": 60,
        "EXTRACT_CORROSION": 60,
        "EXTRACT_RATE": 58,  # Rate performance extraction
        "EXTRACT_KINETICS": 56,  # NEW: Kinetics extraction (transference, diffusion)
        "EXTRACT_EIS": 55,
        "EXTRACT_OVERPOTENTIAL": 55,
        "ORGANIZE_MERGE": 40,
        "VERIFY_BOUNDARY": 35,
        "QC_NORMALIZE": 30,
    }


# ============================================================================
# Task Status
# ============================================================================
TASK_READY = "READY"
TASK_RUNNING = "RUNNING"
TASK_DONE = "DONE"
TASK_FAILED = "FAILED"
TASK_SKIPPED = "SKIPPED"


# ============================================================================
# Experiment Set ID Helper (Stage 7)
# ============================================================================
def _determine_experiment_set(measurement: Dict[str, Any]) -> str:
    """
    Determine experiment set ID based on measurement metadata.
    Groups measurements by sample/material for analysis.
    """
    tags = measurement.get("tags", {})
    conditions = measurement.get("conditions", {})
    
    # Priority 1: Use material_id if available
    material_id = conditions.get("material_id")
    if material_id:
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
    
    return "EXP_UNCLASSIFIED"


# ============================================================================
# Core Pipeline Functions
# ============================================================================
def load_paper_context(data_root: Path, paper_id: str) -> Dict[str, Any]:
    """Load all paper context for multi-pass extraction."""
    paper_dir = data_root / "papers" / paper_id
    derived = paper_dir / "derived"
    
    context = {
        "paper_id": paper_id,
        "paper_dir": paper_dir,
        "derived": derived,
        "inventory": {},
        "chunks_main": [],
        "chunks_supp": [],
        "labels": {},
        "inclusion": [],
        "cases": [],
        "cross_refs": [],
    }
    
    # Load inventory
    inv_path = derived / "00_inventory.json"
    if inv_path.exists():
        context["inventory"] = read_json(inv_path)
    
    # Load chunks
    chunks_main_path = derived / "01_chunks_main.jsonl"
    chunks_supp_path = derived / "01_chunks_supp.jsonl"
    if chunks_main_path.exists():
        context["chunks_main"] = read_jsonl(chunks_main_path)
    if chunks_supp_path.exists():
        context["chunks_supp"] = read_jsonl(chunks_supp_path)
    
    # Load labels (chunk_id -> labels)
    labels_path = derived / "02_labels.jsonl"
    if labels_path.exists():
        for row in read_jsonl(labels_path):
            context["labels"][row.get("chunk_id", "")] = row.get("labels", [])
    
    # Load inclusion
    incl_path = derived / "03_inclusion.jsonl"
    if incl_path.exists():
        context["inclusion"] = read_jsonl(incl_path)
    
    # Load cases
    cases_path = derived / "04_cases.json"
    if cases_path.exists():
        cases_obj = read_json(cases_path)
        context["cases"] = cases_obj.get("cases", [])
    
    return context


def build_cases_llm(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build cases using LLM (Gemini Flash).
    
    This analyzes section headers and introduction/experimental sections 
    to identify distinct experimental configurations.
    """
    paper_id = ctx["paper_id"]
    all_chunks = ctx.get("chunks_main", []) + ctx.get("chunks_supp", [])
    
    # Select relevant chunks (Intro, Experimental, Conclusion)
    relevant_chunks = []
    for ch in all_chunks:
        sp = (ch.get("section_path") or "").lower()
        if any(kw in sp for kw in ["introduction", "experimental", "preparation", "method", "result"]):
            relevant_chunks.append(ch)
            
    # Top 10 chunks to avoid context overflow
    top_chunks = relevant_chunks[:10]
    
    # Figure captions are also very useful
    captions = []
    for fig in ctx["inventory"].get("figures", []):
        captions.append(fig.get("caption", ""))
        
    variables = {
        "paper_id": paper_id,
        "captions_top": json.dumps(captions[:10], ensure_ascii=False),
        "chunks_top": json.dumps([c.get("text") for c in top_chunks], ensure_ascii=False)
    }
    
    prompt_file = "configs/prompts/case_builder.md"
    task_config = get_model_for_task("CASE_BUILDER")
    
    try:
        result, errors = call_with_self_correction(
            model=task_config["model"],
            prompt_file=prompt_file,
            variables=variables,
            cache_key=f"cases:{paper_id}",
            expected_keys=["output"],
            thinking=task_config.get("thinking", False)
        )
        
        cases = result.get("output", {}).get("cases", [])
        if not cases:
            # Fallback to default
            return []
            
        # Add paper_id to each case
        for c in cases:
            c["paper_id"] = paper_id
            
        return cases
    except Exception as e:
        logger.error(f"  Case building failed: {e}")
        return []


def build_cross_refs_for_paper(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build cross-references for a paper."""
    all_chunks = ctx["chunks_main"] + ctx["chunks_supp"]
    return build_cross_references(all_chunks, ctx["labels"], ctx["inventory"])


# ============================================================================
# Self-Correction Loop (09_설계 Enhanced)
# ============================================================================
def call_with_self_correction(
    model: str,
    prompt_file: str,
    variables: Dict[str, Any],
    cache_key: str,
    expected_keys: List[str] = None,
    max_attempts: int = 2,
    thinking: bool = False,
    use_cache: bool = True
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Call LLM with self-correction loop (09_설계 Enhanced).
    
    Features:
    - normalize_stage4_output for any LLM output format
    - normalize_measurement with default_doc enforcement
    - apply_metric_contract for required keys
    - validate_stage4_output with path-based errors
    - build_correction_hint with detailed error feedback
    
    Returns:
        (parsed_output, validation_errors)
    """
    errors = []
    expected_keys = expected_keys or ["measurements"]
    
    # 09_설계: Get task-specific required keys
    task_name = variables.get("task_name", "Stage4")
    default_doc = variables.get("default_doc", "MAIN")
    required_keys = variables.get("_required_keys") or get_task_required_keys(task_name)
    
    # 09_설계: Build contract text
    contract_text = build_contract_text(task_name)
    
    last_errors = None
    last_obj = None
    
    for attempt in range(max_attempts):
        try:
            # 09_설계: Inject contract on first attempt, correction hint on retries
            if attempt == 0:
                variables["_contract_text"] = contract_text
                use_cache_for_call = use_cache # Use provided cache setting for first attempt
            else:
                variables["_correction_hint"] = build_correction_hint(last_errors or [], required_keys)
                # Include previous output for repair reference
                if last_obj:
                    variables["_previous_output"] = json.dumps(last_obj, ensure_ascii=False)
                use_cache_for_call = False # Force fresh generation on retry
            
            result = call_llm_json(
                model=model,
                prompt_file=prompt_file,
                variables=variables,
                cache_key=f"{cache_key}:attempt{attempt}",
                thinking=thinking,
                use_cache=use_cache_for_call
            )
            
            # 09_설계: Normalize output to contract form
            result = normalize_stage4_output(result)
            
            # Auto-fix: Unnest "output" if it exists
            if "output" in result and isinstance(result["output"], dict):
                output_content = result["output"]
                if any(k in output_content for k in expected_keys):
                    result = normalize_stage4_output(output_content)
            
            # 09_설계: Normalize and apply contract to each measurement
            # 10_설계: Add canonicalization and null value enforcement
            # 11_설계: METRIC_REGISTRY v1.0 strict validation
            ms = result.get("measurements", [])
            normalized_ms = []
            registry_errors = []
            
            for i, m in enumerate(ms):
                m = normalize_measurement(m, default_doc=default_doc)
                if not m:  # Empty dict means invalid input
                    continue
                m = apply_metric_contract(m)
                # 10_설계 Contract v1.1: Canonicalize keys and enforce null value rules
                m = canonicalize_conditions(m)
                m = enforce_null_value_rules(m)
                
                # 11_설계: Registry validation - collect errors but don't block yet
                reg_errs = validate_measurement_registry(m, task_name)
                if reg_errs:
                    for err in reg_errs:
                        registry_errors.append(f"measurements[{i}].{err}")
                
                normalized_ms.append(m)
            result["measurements"] = normalized_ms
            
            # 09_설계: Validate with path-based errors
            validation_errors = validate_stage4_output(result)
            
            # 11_설계: Combine registry errors with validation errors
            all_errors = validation_errors + registry_errors
            
            if not all_errors:
                # Success - clean return
                return result, errors
            
            # Validation failed
            errors.extend(all_errors)
            last_errors = all_errors
            last_obj = result
            
            if attempt < max_attempts - 1:
                # Will retry
                continue
            
            # Max attempts exhausted - return best effort with warnings
            errors.append(f"[{task_name}] self_correction_failed: {len(validation_errors)} errors remaining")
            return result, errors
            
        except Exception as e:
            errors.append(str(e))
            if attempt < max_attempts - 1:
                variables["_correction_hint"] = f"JSON parse error: {str(e)[:100]}. Ensure valid JSON output."
                continue
            # Return empty on exception
            return {"measurements": []}, errors
    
    # Fallback
    return last_obj or {"measurements": []}, errors


# ============================================================================
# Extraction Handlers
# ============================================================================
def run_extract_with_context(
    ctx: Dict[str, Any],
    case: Dict[str, Any],
    task_type: str,
    prompt_file: str,
    model_tier: str,
    out_path: Path,
    use_cache: bool = True,
    topk_main: int = 0,  # 19_설계 Phase 4: Override for fallback retry
    topk_supp: int = 0   # 19_설계 Phase 4: Not used in aggregate, but kept for API compatibility
) -> int:
    """
    Run extraction with full context aggregation.
    
    Returns number of measurements extracted.
    """
    paper_id = ctx["paper_id"]
    
    # Ensure case is a dict
    if not isinstance(case, dict):
        case = {"case_id": "CASE-001"}
    
    case_id = case.get("case_id_hint") or case.get("case_id") or "CASE-001"
    
    # Aggregate context with cross-references
    all_chunks = ctx.get("chunks_main", []) + ctx.get("chunks_supp", [])
    enriched_context = aggregate_context_for_extraction(
        chunks=all_chunks,
        case=case,
        task_type=task_type,
        cross_refs=ctx.get("cross_refs", []),
        labels_map=ctx["labels"],
        inventory=ctx["inventory"],
        topk_override=topk_main  # 19_설계 Phase 4: Pass topk for fallback
    )
    
    # Call LLM - Use task-based model routing (HYBRID STRATEGY)
    task_config = get_model_for_task(task_type)
    model = task_config["model"]
    thinking = task_config.get("thinking", False)
    variables = {"EVIDENCE_PACKET_JSON": json.dumps(enriched_context, ensure_ascii=False)}
    
    # 09_설계: Inject task-specific required keys for contract enforcement
    variables["task_name"] = task_type
    variables["default_doc"] = "MAIN"  # Default for main paper extraction
    variables["_required_keys"] = get_task_required_keys(task_type)
    
    try:
        result, errors = call_with_self_correction(
            model=model,
            prompt_file=prompt_file,
            variables=variables,
            cache_key=f"{paper_id}:{case_id}:{task_type}",
            expected_keys=["measurements"],
            thinking=thinking,
            use_cache=use_cache
        )
        
        if errors:
            logger.warning(f"  Validation warnings: {errors[:2]}")
        
        # Handle case where result is not a dict (LLM returned wrong format)
        if not isinstance(result, dict):
            logger.warning(f"  Unexpected response type: {type(result).__name__}")
            result = {"measurements": result if isinstance(result, list) else []}
        
        # Process measurements
        measurements = result.get("measurements", [])
        # Handle case where measurements is not a list
        if not isinstance(measurements, list):
            measurements = [measurements] if measurements else []
        
        derived = result.get("measurements_derived", [])
        if not isinstance(derived, list):
            derived = [derived] if derived else []
            
        digitize = result.get("digitize_needed", [])
        if not isinstance(digitize, list):
            digitize = [digitize] if digitize else []
        
from scripts.lib.deduplicator import deduplicate_measurements

        # Collect all processed measurements first
        processed_measurements = []
        
        for m in measurements + derived:
            if not isinstance(m, dict):
                continue
            m["paper_id"] = paper_id
            m["case_id"] = case_id
            # Include model ID in extractor_id for traceability
            m["extractor_id"] = f"{task_type}_v2_{model}"
            
            # P0-4: Apply normalize → hydrate → validate order
            # Note: normalize_one now internally calls result_aligner!
            m = normalize_one(m)
            if not m:
                continue
            m = hydrate_evidence(m, ctx)
            
            # Tag auto-correction before validation (P0-5: 고도화 계획)
            m = auto_correct_tags(m)
            
            # Validate after hydration and tag correction (reduces false warnings)
            validation_errors = validate_measurement(m) or []
            if validation_errors:
                m["_validation_warnings_post_hydrate"] = validation_errors
            
            processed_measurements.append(m)
            
        # P2-3: De-duplicate results before saving
        final_measurements = deduplicate_measurements(processed_measurements)
        
        # Save to file
        for m in final_measurements:
            append_jsonl(out_path, m)
        
        # Save digitize tasks
        if digitize:
            dig_path = ctx["derived"] / "10_tasks_digitize.jsonl"
            for dt in digitize:
                if not isinstance(dt, dict):
                    continue
                dt["paper_id"] = paper_id
                dt["case_id"] = case_id
                append_jsonl(dig_path, dt)
        
        return len(final_measurements)
        
    except NotImplementedError:
        logger.warning("  LLM not configured - creating placeholder")
        append_jsonl(out_path, {
            "paper_id": paper_id,
            "case_id": case_id,
            "metric": "placeholder",
            "value": None,
            "extractor_id": f"{task_type}_v2",
            "_llm_not_configured": True
        })
        return 0
    except Exception as e:
        logger.error(f"  Extraction error: {e}")
        return 0


def run_table_extraction(ctx: Dict[str, Any], out_path: Path) -> int:
    """Extract values from tables using Table Agent."""
    paper_id = ctx["paper_id"]
    tables = ctx["inventory"].get("tables", [])
    cases = ctx["cases"]
    
    if not tables:
        return 0
    
    total = 0
    for tbl in tables:
        table_id = tbl.get("table_id", "")
        caption = tbl.get("caption", "")
        parsed = tbl.get("parsed")
        
        if not parsed:
            continue
        
        # Parse table rows
        rows = []
        for line in parsed.split("\n"):
            cells = line.split("\t")
            if cells:
                rows.append(cells)
        
        if len(rows) < 2:
            continue
        
        # Try LLM-based extraction (Gemini Flash)
        measurements = extract_from_table_llm(
            table_id=table_id,
            caption=caption,
            rows=rows,
            cases=cases,
            paper_id=paper_id
        )
        
        # Fallback to rules if LLM failed or returned nothing (optional, but safer)
        if not measurements:
            measurements = extract_from_table(
                table_id=table_id,
                caption=caption,
                rows=rows,
                cases=cases,
                paper_id=paper_id
            )
        
        for m in measurements:
            append_jsonl(out_path, m)
            total += 1
    
    logger.info(f"  Table Agent extracted {total} measurements from {len(tables)} tables")
    return total


# ============================================================================
# Full Paper Processing
# ============================================================================
def process_paper_full(data_root: Path, paper_id: str, run_dir: Path, use_cache: bool = True) -> Dict[str, Any]:
    """
    Process a single paper through the entire pipeline.
    
    This is the core function that "completely clears" a paper.
    
    Returns summary dict with stats.
    """
    logger.info(f"=" * 60)
    logger.info(f"Processing: {paper_id}")
    logger.info(f"=" * 60)
    
    stats = {
        "paper_id": paper_id,
        "started_at": datetime.now().isoformat(),
        "stages_completed": [],
        "total_measurements": 0,
        "total_cases": 0,
        "errors": [],
        "status": "IN_PROGRESS"
    }
    
    try:
        # 16_설계: Initialize TraceWriter for this run
        trace_writer = init_trace_writer(str(run_dir), enabled=True)
        logger.info(f"Initialized LLM trace writer at {run_dir}/traces")
        
        # Load context
        ctx = load_paper_context(data_root, paper_id)
        ctx["trace_writer"] = trace_writer  # Make available to all stages
        ctx["run_id"] = run_dir.name  # e.g., "run_001"
        
        # Check prerequisites
        if not ctx["chunks_main"]:
            raise ValueError("No chunks found. Run chunking first.")
        
        # Stage 1: Build cross-references (if enabled)
        if PipelineConfig.CROSS_REF_LINKING:
            logger.info("Stage 1: Building cross-references...")
            ctx["cross_refs"] = build_cross_refs_for_paper(ctx)
            logger.info(f"  Found {len(ctx['cross_refs'])} cross-reference links")
            stats["stages_completed"].append("CROSS_REF")
        
        # Stage 2: Ensure cases exist
        if not ctx["cases"]:
            logger.info("Stage 2: Building cases using LLM...")
            ctx["cases"] = build_cases_llm(ctx)
            
            if not ctx["cases"]:
                logger.warning("  No cases identified by LLM. Using default case.")
                ctx["cases"] = [{
                    "case_id_hint": "CASE-001",
                    "paper_id": paper_id,
                    "coating_label": "Unknown",
                    "cell_type": "UNCLEAR"
                }]
            else:
                logger.info(f"  Identified {len(ctx['cases'])} experimental cases")
                # Save identified cases for traceability
                write_json(ctx["derived"] / "04_cases.json", {"paper_id": paper_id, "cases": ctx["cases"]})
        
        stats["total_cases"] = len(ctx["cases"])
        stats["stages_completed"].append("CASE_BUILD")
        
        # Stage 3: Table extraction
        out_path = ctx["derived"] / "06_measurements_raw.jsonl"
        
        if PipelineConfig.TABLE_AGENT_ENABLED:
            logger.info("Stage 3: Table Agent extraction...")
            table_count = run_table_extraction(ctx, out_path)
            stats["total_measurements"] += table_count
            stats["stages_completed"].append("TABLE_EXTRACT")
        
        # Stage 4: LLM Extractors for each case
        extractors = [
            ("EXTRACT_INPUT", "configs/prompts/extract_input.md", "mid"),
            ("EXTRACT_CYCLING", "configs/prompts/extract_cycling.md", "mid"),
            ("EXTRACT_RATE", "configs/prompts/extract_rate.md", "mid"),
            ("EXTRACT_KINETICS", "configs/prompts/extract_kinetics.md", "mid"),  # NEW: Kinetics
            ("EXTRACT_CORROSION", "configs/prompts/extract_corrosion.md", "mid"),
            ("EXTRACT_EIS", "configs/prompts/extract_eis.md", "large"),
            ("EXTRACT_OVERPOTENTIAL", "configs/prompts/extract_overpotential.md", "large"),
        ]
        
        for case in ctx["cases"]:
            case_id = case.get("case_id_hint") or case.get("case_id")
            logger.info(f"Processing Case: {case_id}")
            
            for task_type, prompt_file, tier in extractors:
                logger.info(f"  Running {task_type}...")
                count = run_extract_with_context(
                    ctx=ctx,
                    case=case,
                    task_type=task_type,
                    prompt_file=prompt_file,
                    model_tier=tier,
                    out_path=out_path,
                    use_cache=use_cache
                )
                
                # 15_설계 Day 2: Cycling fallback - retry with Gemini if 0 extracted and keywords present
                if task_type == "EXTRACT_CYCLING" and count == 0:
                    if has_cycling_content(ctx):
                        logger.warning(f"  CYCLING fallback: 0 results but keywords found, retrying with gemini-flash")
                        # Force gemini-flash for retry by using flash task config
                        from scripts.lib.llm_client import get_model_for_task
                        flash_config = get_model_for_task("EXTRACT_INPUT")  # Uses flash
                        count = run_extract_with_context(
                            ctx=ctx,
                            case=case,
                            task_type="EXTRACT_CYCLING",
                            prompt_file=prompt_file,
                            model_tier="flash",
                            out_path=out_path
                        )
                        if count == 0:
                            # Still 0 - add coverage flag
                            keywords = get_cycling_keywords_found(ctx)
                            flag = build_coverage_flag(paper_id, case_id, "cycling", keywords)
                            flags_path = ctx["derived"] / "11_coverage_flags.jsonl"
                            append_jsonl(flags_path, flag)
                            logger.warning(f"  Added coverage flag: MISSING_EXPECTED_METRIC (cycling, {len(keywords)} keywords found)")
                
                # 19_설계 Phase 4: RATE fallback - retry with expanded evidence if 0 extracted
                if task_type == "EXTRACT_RATE" and count == 0:
                    if has_rate_content(ctx):
                        logger.warning(f"  RATE fallback: 0 results but keywords found, retrying with expanded evidence")
                        count = run_extract_with_context(
                            ctx=ctx,
                            case=case,
                            task_type="EXTRACT_RATE",
                            prompt_file=prompt_file,
                            model_tier="mid",
                            out_path=out_path,
                            topk_main=20,  # Expand to 20 chunks
                            topk_supp=8
                        )
                        if count == 0:
                            keywords = get_rate_keywords_found(ctx)
                            flag = build_coverage_flag(paper_id, case_id, "rate", keywords)
                            flags_path = ctx["derived"] / "11_coverage_flags.jsonl"
                            append_jsonl(flags_path, flag)
                            logger.warning(f"  Added coverage flag: MISSING_EXPECTED_METRIC (rate, {len(keywords)} keywords found)")
                
                # 19_설계 Phase 4: OVERPOTENTIAL fallback
                if task_type == "EXTRACT_OVERPOTENTIAL" and count == 0:
                    if has_overpotential_content(ctx):
                        logger.warning(f"  OVERPOTENTIAL fallback: 0 results but keywords found, retrying with expanded evidence")
                        count = run_extract_with_context(
                            ctx=ctx,
                            case=case,
                            task_type="EXTRACT_OVERPOTENTIAL",
                            prompt_file=prompt_file,
                            model_tier="large",
                            out_path=out_path,
                            topk_main=20,
                            topk_supp=8
                        )
                        if count == 0:
                            keywords = get_overpotential_keywords_found(ctx)
                            flag = build_coverage_flag(paper_id, case_id, "overpotential", keywords)
                            flags_path = ctx["derived"] / "11_coverage_flags.jsonl"
                            append_jsonl(flags_path, flag)
                            logger.warning(f"  Added coverage flag: MISSING_EXPECTED_METRIC (overpotential, {len(keywords)} keywords found)")
                
                stats["total_measurements"] += count
                logger.info(f"    Extracted: {count} measurements")
        
        stats["stages_completed"].append("EXTRACTION")
        
        # Stage 5: Organize with Enterprise-Grade Processing
        logger.info("Stage 5: Enterprise-grade organizing...")
        raw_meas_raw = read_jsonl(out_path) if out_path.exists() else []
        
        # Step 5.1: Normalize metric names and units first
        raw_meas_normalized = [normalize_one(rm) for rm in raw_meas_raw if rm and not rm.get("_llm_not_configured")]
        
        # Step 5.1b (08_설계): Safe filter with type guards - handles tuples and logs drops
        raw_meas = safe_filter_measurements(raw_meas_normalized, stage="stage5_post_normalize")
        logger.info(f"  Normalized: {len(raw_meas_raw)} raw -> {len(raw_meas)} after filtering")
        
        # Step 5.2: Fill required keys with null (METRIC_REGISTRY compliance)
        for i, m in enumerate(raw_meas):
            raw_meas[i] = fill_required_keys_with_null(m)
        
        # Step 5.3: Hydrate evidence (fill missing doc/section_path/quote + quote override)
        hydrated = [hydrate_evidence(m, ctx) for m in raw_meas]
        logger.info(f"  Hydrated evidence for {len(hydrated)} measurements")
        
        # Step 5.3b (15_설계): Tag auto-correction - fill missing eis_metric_type, zn_adsorption_source, before_after
        hydrated = auto_correct_measurements(hydrated)
        logger.info(f"  Applied tag auto-correction (15_설계 Day 0)")
        
        # Step 5.3c (15_설계): EIS conditions injection - move frequency/amplitude to conditions
        hydrated = inject_eis_conditions(hydrated)
        hydrated = split_multivalue_eis(hydrated)  # Split R0/Rs conflicts
        
        # Step 5.3d (15_설계): Anchor integrity - validate figure_id/table_id against inventory
        hydrated = validate_measurements_anchors(hydrated, ctx.get("inventory", {}))
        unresolved_count = count_unresolved_refs(hydrated)
        if unresolved_count > 0:
            logger.warning(f"  [15_설계] Anchor integrity: {unresolved_count} measurements have unresolved refs")
        
        # Step 5.3e (08_설계): Registry-based validation logging (informational)
        registry_errors_total = 0
        for m in hydrated:
            errors = validate_with_registry(m)
            if errors:
                registry_errors_total += len(errors)
        if registry_errors_total > 0:
            logger.warning(f"  [08_설계] Registry validation: {registry_errors_total} total issues across measurements")
        
        # Step 5.4: Smart organize with conditions/tags preservation
        organized = organize_measurements_v2(hydrated)
        logger.info(f"  Organized: {len(raw_meas)} raw -> {len(organized)} unique (preserving conditions/tags)")
        
        organized_path = ctx["derived"] / "07_measurements_organized.jsonl"
        write_jsonl(organized_path, organized)
        stats["stages_completed"].append("ORGANIZE")
        
        # Stage 6: Final Normalize and QC
        logger.info("Stage 6: Final normalization and QC...")
        
        # C1+C2: Expand multi-value records and auto-generate cycle_life_cycles
        logger.info("  Stage 6a: Expanding multi-value records (C1+C2)...")
        try:
            organized = expand_and_enrich_measurements(organized)
            logger.info(f"  After C1+C2: {len(organized)} records")
        except Exception as e:
            logger.warning(f"  C1+C2 expansion failed (non-fatal): {e}")
        
        # Round 4: Unit-Binding QC Validation (drop contaminated records)
        logger.info("  Stage 6b: Unit-Binding QC validation (Round 4)...")
        try:
            organized, qc_summary = qc_validate_batch(organized)
            logger.info(f"  After QC: {qc_summary['kept_count']} kept, {qc_summary['dropped_count']} dropped")
            if qc_summary.get("drop_by_rule"):
                for rule, cnt in qc_summary["drop_by_rule"].items():
                    logger.info(f"    - {rule}: {cnt} dropped")
            stats["qc_validation"] = qc_summary
        except Exception as e:
            logger.warning(f"  QC validation failed (non-fatal): {e}")
        
        normalized = normalize_measurements(organized)
        norm_path = ctx["derived"] / "08_measurements_normalized.jsonl"
        write_jsonl(norm_path, normalized)
        
        qc_report = run_qc_checks(normalized)
        qc_path = ctx["derived"] / "09_qc_report.json"
        write_json(qc_path, qc_report)
        
        # FINAL STEP: Save the cleaned, deduplicated, and normalized measurements as the definitive output
        final_measurements = qc_report.get("cleaned_measurements", normalized)
        final_path = ctx["derived"] / "10_measurements_final.jsonl"
        write_jsonl(final_path, final_measurements)
        
        # P1-1: Save unresolved references (value=None) separately
        unresolved_refs = qc_report.get("unresolved_refs", [])
        if unresolved_refs:
            unresolved_path = ctx["derived"] / "11_unresolved_refs.jsonl"
            write_jsonl(unresolved_path, unresolved_refs)
            logger.info(f"  Unresolved refs: {len(unresolved_refs)} records saved to {unresolved_path.name}")
        
        logger.info(f"  QC: {len(qc_report.get('flags', []))} flags, {len(qc_report.get('conflicts', []))} conflicts")
        logger.info(f"  Final: {len(final_measurements)} unique measurements saved to {final_path.name}")
        stats["stages_completed"].append("QC_NORMALIZE")
        
        # Stage 7: Add experiment_set_id for grouping analysis
        logger.info("Stage 7: Adding experiment_set_id...")
        for m in final_measurements:
            m["experiment_set_id"] = _determine_experiment_set(m)
        
        # Re-save with experiment_set_id
        write_jsonl(final_path, final_measurements)
        
        # Count groups
        exp_groups = {}
        for m in final_measurements:
            exp_id = m.get("experiment_set_id", "UNKNOWN")
            exp_groups[exp_id] = exp_groups.get(exp_id, 0) + 1
        logger.info(f"  Experiment sets: {len(exp_groups)} groups")
        stats["experiment_sets"] = exp_groups
        stats["stages_completed"].append("EXPERIMENT_SET_ID")
        
        # Stage 8: Context Extension (Phase 5 v5.0)
        logger.info("Stage 8: Extending measurement context...")
        try:
            # Build chunks dict for context expansion
            chunks_dict = {}
            for chunk in ctx.get("chunks_main", []):
                cid = chunk.get("chunk_id", "")
                if cid:
                    chunks_dict[cid] = chunk
            for chunk in ctx.get("chunks_supp", []):
                cid = chunk.get("chunk_id", "")
                if cid:
                    chunks_dict[cid] = chunk
            
            # Build figures dict from inventory
            figures_dict = {}
            inventory = ctx.get("inventory", {})
            for fig in inventory.get("figures", []):
                fid = fig.get("figure_id", "")
                if fid:
                    figures_dict[fid] = fig
            
            # Extend context
            final_measurements = extend_measurement_context(
                final_measurements, chunks_dict, figures_dict
            )
            
            # Count context extensions
            ctx_para_count = sum(1 for m in final_measurements if m.get("context_paragraph"))
            cmp_group_count = sum(1 for m in final_measurements if m.get("comparison_group"))
            related_count = sum(1 for m in final_measurements if m.get("related_measurements"))
            
            logger.info(f"  Context extended: {ctx_para_count} paragraphs, {cmp_group_count} comparison groups, {related_count} related links")
            
            # Re-save with context
            write_jsonl(final_path, final_measurements)
            stats["stages_completed"].append("CONTEXT_EXTENSION")
            
            # Stage 8b: Experiment Context LLM Extraction
            logger.info("Stage 8b: Extracting experiment_context via LLM...")
            try:
                final_measurements = extract_and_merge_experiment_context(
                    final_measurements,
                    use_llm=True,
                    model="gemini-2.5-flash"
                )
                
                exp_ctx_count = sum(1 for m in final_measurements if m.get("experiment_context"))
                logger.info(f"  Experiment context: {exp_ctx_count}/{len(final_measurements)} measurements")
                
                # Re-save with experiment_context
                write_jsonl(final_path, final_measurements)
                stats["stages_completed"].append("EXPERIMENT_CONTEXT")
            except Exception as exp_err:
                logger.warning(f"  Experiment context extraction failed (non-fatal): {exp_err}")
                
        except Exception as ctx_err:
            logger.warning(f"  Context extension failed (non-fatal): {ctx_err}")
        
        stats["status"] = "DONE"
        stats["finished_at"] = datetime.now().isoformat()
        
    except Exception as e:
        stats["status"] = "FAILED"
        stats["errors"].append(str(e))
        logger.error(f"Pipeline failed: {e}")
        logger.debug(traceback.format_exc())
    
    # Save paper stats
    stats_path = run_dir / "paper_stats" / f"{paper_id}.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(stats_path, stats)
    
    logger.info(f"Completed: {stats['status']} | {stats['total_measurements']} measurements")
    return stats


# ============================================================================
# Main Entry Point
# ============================================================================
def main():
    parser = argparse.ArgumentParser(description="Enterprise AZIB Extraction Pipeline v2")
    parser.add_argument("--run-dir", type=str, required=True, help="Run directory")
    parser.add_argument("--data-root", type=str, default="data", help="Data root directory")
    parser.add_argument("--paper-id", type=str, help="Process single paper")
    parser.add_argument("--all", action="store_true", help="Process all papers")
    parser.add_argument("--max-papers", type=int, default=0, help="Max papers to process (0=all)")
    parser.add_argument("--no-cache", action="store_true", help="Force re-generation (bypass cache)")
    args = parser.parse_args()
    
    data_root = Path(args.data_root)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    
    # Get paper list
    if args.paper_id:
        paper_ids = [args.paper_id]
    elif args.all:
        papers_dir = data_root / "papers"
        paper_ids = [d.name for d in papers_dir.iterdir() if d.is_dir()]
    else:
        # Try to load from run config
        queue_path = run_dir / "task_queue.jsonl"
        if queue_path.exists():
            tasks = read_jsonl(queue_path)
            paper_ids = list(set(t["paper_id"] for t in tasks))
        else:
            logger.error("No papers specified. Use --paper-id, --all, or create task_queue.jsonl")
            return
    
    if args.max_papers > 0:
        paper_ids = paper_ids[:args.max_papers]
    
    logger.info(f"Processing {len(paper_ids)} papers")
    
    # Process papers
    all_stats = []
    for pid in paper_ids:
        stats = process_paper_full(data_root, pid, run_dir, use_cache=not args.no_cache)
        all_stats.append(stats)
    
    # Summary
    done = sum(1 for s in all_stats if s["status"] == "DONE")
    failed = sum(1 for s in all_stats if s["status"] == "FAILED")
    total_meas = sum(s.get("total_measurements", 0) for s in all_stats)
    
    logger.info("=" * 60)
    logger.info("PIPELINE SUMMARY")
    logger.info(f"Papers: {done} done, {failed} failed")
    logger.info(f"Total measurements: {total_meas}")
    logger.info("=" * 60)
    
    # Save run summary
    summary = {
        "run_dir": str(run_dir),
        "finished_at": datetime.now().isoformat(),
        "papers_done": done,
        "papers_failed": failed,
        "total_measurements": total_meas,
        "paper_stats": all_stats
    }
    write_json(run_dir / "run_summary.json", summary)


if __name__ == "__main__":
    main()
