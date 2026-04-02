# scripts/run_pipeline.py
"""
Main pipeline worker for AZIB ex-situ extraction.

This script processes tasks from task_queue.jsonl:
- Picks READY tasks by priority
- Executes LLM-based extraction
- Updates task status (DONE/FAILED)
- Implements retry logic

Usage:
    set MODEL_SMALL=gpt-4o-mini
    set MODEL_MID=gpt-4o
    set MODEL_LARGE=gpt-4o
    python scripts/run_pipeline.py --run-dir runs/run_001
"""
from __future__ import annotations
import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.io_jsonl import read_jsonl, write_jsonl, read_json, write_json, append_jsonl
from scripts.lib.llm_client import call_llm_json, get_model_name
from scripts.lib.evidence_packet import build_evidence_packet
from scripts.lib.adaptive_planner import build_plan_from_inclusion
from scripts.lib.normalize_units import normalize_measurements
from scripts.lib.qc_rules import run_qc_checks

TASK_READY = "READY"
TASK_RUNNING = "RUNNING"
TASK_DONE = "DONE"
TASK_FAILED = "FAILED"


def load_queue(queue_path: Path) -> List[Dict[str, Any]]:
    """Load task queue from JSONL file."""
    return read_jsonl(queue_path)


def save_queue(queue_path: Path, tasks: List[Dict[str, Any]]) -> None:
    """Save task queue to JSONL file."""
    write_jsonl(queue_path, tasks)


def pick_next_task(tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick next READY task with highest priority."""
    ready = [t for t in tasks if t["status"] == TASK_READY]
    if not ready:
        return None
    ready.sort(key=lambda x: (-x.get("priority", 0), x["task_id"]))
    return ready[0]


def mark_task(tasks: List[Dict[str, Any]], task_id: str, **updates) -> Dict[str, Any]:
    """Update task with given fields."""
    for t in tasks:
        if t["task_id"] == task_id:
            t.update(updates)
            t["updated_at"] = datetime.now().isoformat()
            return t
    raise KeyError(f"Task not found: {task_id}")


def run_chunk_categorize(task: Dict[str, Any], data_root: str) -> None:
    """Execute CHUNK_CATEGORIZE task."""
    chunks_path = Path(task["inputs"]["chunks_path"])
    out_path = Path(task["outputs"]["path"])
    
    if not chunks_path.exists():
        print(f"    Skipping: chunks file not found: {chunks_path}")
        return
    
    chunks = read_jsonl(chunks_path)
    model = get_model_name("small")
    
    for ch in chunks:
        paper_id = ch.get("paper_id", "unknown")
        payload = {
            "chunk_id": ch["chunk_id"],
            "section_path": ch.get("section_path", ""),
            "chunk_text": ch.get("text", "")
        }
        try:
            resp = call_llm_json(
                model=model,
                prompt_file="configs/prompts/categorizer.md",
                variables=payload,
                cache_key=f"{paper_id}:{ch['chunk_id']}:categorize"
            )
            append_jsonl(out_path, {
                "paper_id": paper_id,
                "doc": ch.get("doc", "MAIN"),
                "chunk_id": ch["chunk_id"],
                "labels": resp.get("labels", []),
                "confidence": resp.get("confidence", 0.0)
            })
        except NotImplementedError:
            # LLM not configured - create placeholder
            append_jsonl(out_path, {
                "paper_id": paper_id,
                "doc": ch.get("doc", "MAIN"),
                "chunk_id": ch["chunk_id"],
                "labels": ["IRRELEVANT"],
                "confidence": 0.0,
                "_llm_not_configured": True
            })


def run_chunk_include(task: Dict[str, Any], data_root: str) -> None:
    """Execute CHUNK_INCLUDE task."""
    chunks_path = Path(task["inputs"]["chunks_path"])
    out_path = Path(task["outputs"]["path"])
    
    if not chunks_path.exists():
        print(f"    Skipping: chunks file not found: {chunks_path}")
        return
    
    chunks = read_jsonl(chunks_path)
    model = get_model_name("small")
    
    for ch in chunks:
        paper_id = ch.get("paper_id", "unknown")
        payload = {
            "chunk_id": ch["chunk_id"],
            "section_path": ch.get("section_path", ""),
            "chunk_text": ch.get("text", "")
        }
        try:
            resp = call_llm_json(
                model=model,
                prompt_file="configs/prompts/inclusion.md",
                variables=payload,
                cache_key=f"{paper_id}:{ch['chunk_id']}:include"
            )
            append_jsonl(out_path, {
                "paper_id": paper_id,
                "doc": ch.get("doc", "MAIN"),
                "chunk_id": ch["chunk_id"],
                **resp
            })
        except NotImplementedError:
            append_jsonl(out_path, {
                "paper_id": paper_id,
                "doc": ch.get("doc", "MAIN"),
                "chunk_id": ch["chunk_id"],
                "fields_present": [],
                "value_location": {},
                "scope": {},
                "_llm_not_configured": True
            })


def run_build_cases(task: Dict[str, Any], data_root: str) -> None:
    """Execute BUILD_CASES task."""
    paper_id = task["inputs"]["paper_id"]
    paper_dir = Path(task["inputs"]["paper_dir"])
    out_path = Path(task["outputs"]["path"])
    
    # Load inventory and chunks for evidence
    inv_path = paper_dir / "derived" / "00_inventory.json"
    chunks_main = read_jsonl(paper_dir / "derived" / "01_chunks_main.jsonl")
    
    inventory = read_json(inv_path) if inv_path.exists() else {"figures": [], "tables": []}
    
    # Select top chunks for case building
    top_chunks = chunks_main[:10]  # Simple selection for now
    
    model = get_model_name("mid")
    
    payload = {
        "paper_id": paper_id,
        "captions_top": json.dumps([f["caption"] for f in inventory.get("figures", [])[:5]], ensure_ascii=False),
        "chunks_top": json.dumps([{"section_path": c.get("section_path"), "text": c.get("text", "")[:500]} for c in top_chunks], ensure_ascii=False)
    }
    
    try:
        resp = call_llm_json(
            model=model,
            prompt_file="configs/prompts/case_builder.md",
            variables=payload,
            cache_key=f"{paper_id}:build_cases"
        )
        write_json(out_path, resp)
    except NotImplementedError:
        # Create placeholder case
        write_json(out_path, {
            "cases": [{
                "case_id_hint": "CASE-001",
                "coating_label": None,
                "material_raw": None,
                "electrolyte_raw": None,
                "cell_type": "UNCLEAR",
                "evidence": []
            }],
            "notes": "LLM not configured - placeholder case",
            "_llm_not_configured": True
        })


def run_build_plan(task: Dict[str, Any], data_root: str) -> None:
    """Execute BUILD_PLAN task."""
    inclusion_path = Path(task["inputs"]["inclusion_path"])
    cases_path = Path(task["inputs"]["cases_path"])
    paper_id = task["inputs"]["paper_id"]
    paper_dir = task["inputs"]["paper_dir"]
    out_path = Path(task["outputs"]["path"])
    
    inclusion_rows = read_jsonl(inclusion_path) if inclusion_path.exists() else []
    cases_obj = read_json(cases_path) if cases_path.exists() else {"cases": []}
    
    plan = build_plan_from_inclusion(inclusion_rows, cases_obj, paper_id, paper_dir)
    write_json(out_path, plan)


def run_extract(task: Dict[str, Any], data_root: str) -> None:
    """Execute EXTRACT_* tasks."""
    plan_path = Path(task["inputs"]["plan_path"])
    cases_path = Path(task["inputs"]["cases_path"])
    paper_dir = Path(task["inputs"]["paper_dir"])
    model_env = task["inputs"]["model_env"]
    prompt_file = task["inputs"]["prompt_file"]
    out_path = Path(task["outputs"]["path"])
    task_type = task["task_type"]
    
    if not cases_path.exists():
        print(f"    Skipping: cases file not found")
        return
    
    plan = read_json(plan_path) if plan_path.exists() else {"case_tasks": {}}
    cases_obj = read_json(cases_path)
    cases = cases_obj.get("cases", [])
    paper_id = task["paper_id"]
    
    model = os.environ.get(model_env, get_model_name("mid"))
    
    for c in cases:
        case_id = c.get("case_id_hint") or c.get("case_id")
        
        # Check if this extractor should run for this case
        if case_id and case_id in plan.get("case_tasks", {}):
            if not plan["case_tasks"][case_id].get(task_type, True):
                continue
        
        # Build evidence packet
        try:
            packet = build_evidence_packet(
                data_root=data_root,
                paper_id=paper_id,
                case=c,
                task_type=task_type
            )
        except Exception as e:
            print(f"    Warning: Failed to build evidence packet: {e}")
            continue
        
        payload = {
            "EVIDENCE_PACKET_JSON": json.dumps(packet, ensure_ascii=False)
        }
        
        try:
            resp = call_llm_json(
                model=model,
                prompt_file=prompt_file,
                variables=payload,
                cache_key=f"{paper_id}:{case_id}:{task_type}"
            )
            
            # Write measurements
            for m in resp.get("measurements", []):
                m2 = {
                    "paper_id": paper_id,
                    "case_id": case_id,
                    **m,
                    "extractor_id": f"{task_type}_v1"
                }
                append_jsonl(out_path, m2)
            
            # Write digitize tasks if any
            digitize_path = paper_dir / "derived" / "10_tasks_digitize.jsonl"
            for dt in resp.get("digitize_needed", []):
                append_jsonl(digitize_path, {
                    "paper_id": paper_id,
                    "case_id": case_id,
                    **dt
                })
                
        except NotImplementedError:
            # LLM not configured
            append_jsonl(out_path, {
                "paper_id": paper_id,
                "case_id": case_id,
                "metric": "placeholder",
                "value": None,
                "extractor_id": f"{task_type}_v1",
                "_llm_not_configured": True
            })


def run_qc_normalize(task: Dict[str, Any], data_root: str) -> None:
    """Execute QC_NORMALIZE task."""
    meas_path = Path(task["inputs"]["measurements_path"])
    norm_path = Path(task["outputs"]["normalized_path"])
    qc_path = Path(task["outputs"]["qc_path"])
    
    measurements = read_jsonl(meas_path) if meas_path.exists() else []
    
    # Normalize
    normalized = normalize_measurements(measurements)
    write_jsonl(norm_path, normalized)
    
    # QC
    qc_report = run_qc_checks(normalized)
    write_json(qc_path, qc_report)


def run_single_task(task: Dict[str, Any], data_root: str) -> None:
    """Execute a single task based on its type."""
    task_type = task["task_type"]
    
    if task_type == "CHUNK_CATEGORIZE":
        run_chunk_categorize(task, data_root)
    elif task_type == "CHUNK_INCLUDE":
        run_chunk_include(task, data_root)
    elif task_type == "BUILD_CASES":
        run_build_cases(task, data_root)
    elif task_type == "BUILD_PLAN":
        run_build_plan(task, data_root)
    elif task_type.startswith("EXTRACT_"):
        run_extract(task, data_root)
    elif task_type == "QC_NORMALIZE":
        run_qc_normalize(task, data_root)
def run_organize_merge(task: Dict[str, Any], data_root: str) -> None:
    """Execute ORGANIZE_MERGE task."""
    cases_path = Path(task["inputs"]["cases_path"])
    meas_raw_path = Path(task["inputs"]["measurements_path"])
    out_path = Path(task["outputs"]["path"])
    paper_id = task["paper_id"]
    
    if not cases_path.exists() or not meas_raw_path.exists():
        print(f"    Skipping: input files not found")
        return

    cases_obj = read_json(cases_path)
    meas_raw = read_jsonl(meas_raw_path)
    
    # Simple merge logic (can be enhanced with LLM if needed, but heuristic is faster/cheaper)
    # Strategy: Group by case_id + metric.
    # If multiple values, keep the one with higher confidence or better evidence.
    
    grouped = {}
    for m in meas_raw:
        if m.get("_llm_not_configured"): 
            continue
        cid = m.get("case_id")
        metric = m.get("metric")
        if not cid or not metric: 
            continue
        
        k = (cid, metric)
        if k not in grouped:
            grouped[k] = []
        grouped[k].append(m)
    
    merged = []
    
    for (cid, metric), items in grouped.items():
        # Sort by confidence + evidence quality
        # Bonus for having figure_id or table_id
        items.sort(key=lambda x: (
            x.get("confidence", 0) + (0.2 if x.get("evidence", {}).get("figure_id") else 0),
            len(x.get("evidence", {}).get("quote", ""))
        ), reverse=True)
        
        best = items[0]
        merged.append(best)
        
        # If conflicts exist (different values), we could flag them here
        # For now, we rely on QC_NORMALIZE step to detect conflicts logic
        
    write_jsonl(out_path, merged)


def run_verify_boundary(task: Dict[str, Any], data_root: str) -> None:
    """Execute VERIFY_BOUNDARY task."""
    cases_path = Path(task["inputs"]["cases_path"])
    organized_path = Path(task["inputs"]["organized_path"])
    out_path = Path(task["outputs"]["path"])
    paper_id = task["paper_id"]
    
    if not cases_path.exists() or not organized_path.exists():
        print(f"    Skipping inputs")
        return
        
    cases = read_json(cases_path)
    measurements = read_jsonl(organized_path)
    
    model = get_model_name("large")
    
    payload = {
        "paper_id": paper_id,
        "cases_json": json.dumps(cases.get("cases", []), ensure_ascii=False),
        "merged_json": json.dumps(measurements, ensure_ascii=False)
    }
    
    try:
        resp = call_llm_json(
            model=model,
            prompt_file="configs/prompts/verifier.md",
            variables=payload,
            cache_key=f"{paper_id}:verify_boundary",
            thinking=True  # Enable CoT for verification
        )
        write_json(out_path, resp)
    except NotImplementedError:
        write_json(out_path, {
            "verdicts": [], 
            "notes": "LLM not configured",
            "_llm_not_configured": True
        })


def run_single_task(task: Dict[str, Any], data_root: str) -> None:
    """Execute a single task based on its type."""
    task_type = task["task_type"]
    
    if task_type == "CHUNK_CATEGORIZE":
        run_chunk_categorize(task, data_root)
    elif task_type == "CHUNK_INCLUDE":
        run_chunk_include(task, data_root)
    elif task_type == "BUILD_CASES":
        run_build_cases(task, data_root)
    elif task_type == "BUILD_PLAN":
        run_build_plan(task, data_root)
    elif task_type.startswith("EXTRACT_"):
        run_extract(task, data_root)
    elif task_type == "QC_NORMALIZE":
        run_qc_normalize(task, data_root)
    elif task_type == "ORGANIZE_MERGE":
        run_organize_merge(task, data_root)
    elif task_type == "VERIFY_BOUNDARY":
        run_verify_boundary(task, data_root)
    else:
        raise ValueError(f"Unknown task_type: {task_type}")
    else:
        raise ValueError(f"Unknown task_type: {task_type}")


def run(run_dir: str, data_root: str, max_tasks: int = 0) -> None:
    """
    Main pipeline loop.
    
    Args:
        run_dir: Directory containing task_queue.jsonl
        data_root: Root data directory
        max_tasks: Maximum tasks to process (0 = unlimited)
    """
    run_path = Path(run_dir)
    queue_path = run_path / "task_queue.jsonl"
    
    if not queue_path.exists():
        print(f"Error: Queue file not found: {queue_path}")
        return
    
    tasks = load_queue(queue_path)
    processed = 0
    
    while True:
        if max_tasks > 0 and processed >= max_tasks:
            print(f"Reached max_tasks limit: {max_tasks}")
            break
        
        task = pick_next_task(tasks)
        if task is None:
            print("No more READY tasks")
            break
        
        task_id = task["task_id"]
        paper_id = task["paper_id"]
        task_type = task["task_type"]
        
        print(f"[{processed+1}] Processing {task_id}: {paper_id} / {task_type}")
        
        mark_task(tasks, task_id, status=TASK_RUNNING)
        save_queue(queue_path, tasks)
        
        try:
            run_single_task(task, data_root)
            mark_task(tasks, task_id, status=TASK_DONE)
            print(f"    ✓ Done")
            
        except Exception as e:
            attempts = task.get("attempts", 0) + 1
            mark_task(tasks, task_id,
                      status=TASK_FAILED if attempts >= task.get("max_attempts", 3) else TASK_READY,
                      attempts=attempts,
                      last_error=str(e))
            print(f"    ✗ Error: {e}")
            if attempts < task.get("max_attempts", 3):
                print(f"    Will retry ({attempts}/{task.get('max_attempts', 3)})")
        
        save_queue(queue_path, tasks)
        processed += 1
    
    # Summary
    done = len([t for t in tasks if t["status"] == TASK_DONE])
    failed = len([t for t in tasks if t["status"] == TASK_FAILED])
    ready = len([t for t in tasks if t["status"] == TASK_READY])
    print(f"\nSummary: {done} done, {failed} failed, {ready} remaining")


def main():
    parser = argparse.ArgumentParser(description="Run AZIB extraction pipeline")
    parser.add_argument("--run-dir", type=str, required=True, help="Run directory with task_queue.jsonl")
    parser.add_argument("--data-root", type=str, default="data", help="Data root directory")
    parser.add_argument("--max-tasks", type=int, default=0, help="Max tasks to process (0=unlimited)")
    args = parser.parse_args()
    
    run(args.run_dir, args.data_root, args.max_tasks)


if __name__ == "__main__":
    main()
