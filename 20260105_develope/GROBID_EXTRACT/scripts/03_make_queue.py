# scripts/03_make_queue.py
"""
Generate task queue for pipeline execution.

This script creates a task_queue.jsonl file that defines all tasks
to be executed for each paper in the pipeline.

Usage:
    python scripts/03_make_queue.py --data-root data --run-dir runs/run_001
    python scripts/03_make_queue.py --data-root data --run-dir runs/run_001 --paper-ids P-001,P-002
"""
from __future__ import annotations
import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.io_jsonl import write_jsonl


def make_task(
    task_id: int,
    paper_id: str,
    task_type: str,
    inputs: Dict[str, Any],
    outputs: Dict[str, Any],
    priority: int = 50,
    deps: List[str] = None
) -> Dict[str, Any]:
    """Create a task dict."""
    if deps is None:
        deps = []
    return {
        "task_id": f"T-{task_id:08d}",
        "paper_id": paper_id,
        "task_type": task_type,
        "status": "READY",
        "priority": priority,
        "deps": deps,
        "inputs": inputs,
        "outputs": outputs,
        "attempts": 0,
        "max_attempts": 3,
        "last_error": None,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }


def build_queue(data_root: str, run_dir: str, paper_ids: List[str]) -> int:
    """
    Build task queue for all specified papers.
    
    Returns:
        Number of tasks created
    """
    runp = Path(run_dir)
    runp.mkdir(parents=True, exist_ok=True)
    tasks = []
    tid = 1

    for pid in paper_ids:
        pdir = Path(data_root) / "papers" / pid / "derived"
        chunks_main = str(pdir / "01_chunks_main.jsonl")
        chunks_supp = str(pdir / "01_chunks_supp.jsonl")

        labels_out = str(pdir / "02_labels.jsonl")
        incl_out = str(pdir / "03_inclusion.jsonl")
        cases_out = str(pdir / "04_cases.json")
        plan_out = str(pdir / "05_plan.json")

        # 1) CHUNK_CATEGORIZE (main/supp)
        tasks.append(make_task(tid, pid, "CHUNK_CATEGORIZE",
                               inputs={"chunks_path": chunks_main, "doc": "MAIN"},
                               outputs={"path": labels_out},
                               priority=80)); tid += 1
        tasks.append(make_task(tid, pid, "CHUNK_CATEGORIZE",
                               inputs={"chunks_path": chunks_supp, "doc": "SUPP"},
                               outputs={"path": labels_out},
                               priority=80)); tid += 1

        # 2) CHUNK_INCLUDE (main/supp)
        tasks.append(make_task(tid, pid, "CHUNK_INCLUDE",
                               inputs={"chunks_path": chunks_main, "doc": "MAIN"},
                               outputs={"path": incl_out},
                               priority=70)); tid += 1
        tasks.append(make_task(tid, pid, "CHUNK_INCLUDE",
                               inputs={"chunks_path": chunks_supp, "doc": "SUPP"},
                               outputs={"path": incl_out},
                               priority=70)); tid += 1

        # 3) BUILD_CASES
        tasks.append(make_task(tid, pid, "BUILD_CASES",
                               inputs={"paper_id": pid, "paper_dir": str(Path(data_root) / "papers" / pid)},
                               outputs={"path": cases_out},
                               priority=65)); tid += 1

        # 4) BUILD_PLAN
        tasks.append(make_task(tid, pid, "BUILD_PLAN",
                               inputs={"inclusion_path": incl_out, "cases_path": cases_out,
                                       "paper_id": pid, "paper_dir": str(Path(data_root) / "papers" / pid)},
                               outputs={"path": plan_out},
                               priority=60)); tid += 1

        # 5) EXTRACT tasks
        meas_raw = str(pdir / "06_measurements_raw.jsonl")
        
        tasks.append(make_task(tid, pid, "EXTRACT_INPUT",
                               inputs={"plan_path": plan_out, "cases_path": cases_out,
                                       "model_env": "MODEL_MID", "prompt_file": "configs/prompts/extract_input.md",
                                       "paper_dir": str(Path(data_root) / "papers" / pid)},
                               outputs={"path": meas_raw},
                               priority=55)); tid += 1

        tasks.append(make_task(tid, pid, "EXTRACT_CYCLING",
                               inputs={"plan_path": plan_out, "cases_path": cases_out,
                                       "model_env": "MODEL_MID", "prompt_file": "configs/prompts/extract_cycling.md",
                                       "paper_dir": str(Path(data_root) / "papers" / pid)},
                               outputs={"path": meas_raw},
                               priority=50)); tid += 1

        tasks.append(make_task(tid, pid, "EXTRACT_CORROSION",
                               inputs={"plan_path": plan_out, "cases_path": cases_out,
                                       "model_env": "MODEL_MID", "prompt_file": "configs/prompts/extract_corrosion.md",
                                       "paper_dir": str(Path(data_root) / "papers" / pid)},
                               outputs={"path": meas_raw},
                               priority=50)); tid += 1

        tasks.append(make_task(tid, pid, "EXTRACT_EIS",
                               inputs={"plan_path": plan_out, "cases_path": cases_out,
                                       "model_env": "MODEL_LARGE", "prompt_file": "configs/prompts/extract_eis.md",
                                       "paper_dir": str(Path(data_root) / "papers" / pid)},
                               outputs={"path": meas_raw},
                               priority=50)); tid += 1

        tasks.append(make_task(tid, pid, "EXTRACT_OVERPOTENTIAL",
                               inputs={"plan_path": plan_out, "cases_path": cases_out,
                                       "model_env": "MODEL_LARGE", "prompt_file": "configs/prompts/extract_overpotential.md",
                                       "paper_dir": str(Path(data_root) / "papers" / pid)},
                               outputs={"path": meas_raw},
                               priority=45)); tid += 1

        # 6) ORGANIZE_MERGE
        tasks.append(make_task(tid, pid, "ORGANIZE_MERGE",
                               inputs={"paper_id": pid, "cases_path": cases_out,
                                       "measurements_path": meas_raw},
                               outputs={"path": str(pdir / "07_measurements_organized.jsonl")},
                               priority=40)); tid += 1

        # 7) VERIFY_BOUNDARY
        tasks.append(make_task(tid, pid, "VERIFY_BOUNDARY",
                               inputs={"paper_id": pid, "cases_path": cases_out,
                                       "organized_path": str(pdir / "07_measurements_organized.jsonl")},
                               outputs={"path": str(pdir / "07_verify.json")},
                               priority=35)); tid += 1

        # 8) QC_NORMALIZE
        tasks.append(make_task(tid, pid, "QC_NORMALIZE",
                               inputs={"measurements_path": str(pdir / "07_measurements_organized.jsonl")},
                               outputs={"normalized_path": str(pdir / "08_measurements_normalized.jsonl"),
                                        "qc_path": str(pdir / "09_qc_report.json")},
                               priority=30)); tid += 1

    # Write queue
    write_jsonl(runp / "task_queue.jsonl", tasks)
    
    # Write run config
    config = {
        "n_papers": len(paper_ids),
        "n_tasks": len(tasks),
        "created_at": datetime.now().isoformat(),
        "paper_ids": paper_ids
    }
    with open(runp / "run_config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    
    return len(tasks)


def main():
    parser = argparse.ArgumentParser(description="Generate pipeline task queue")
    parser.add_argument("--data-root", type=str, default="data", help="Data root directory")
    parser.add_argument("--run-dir", type=str, required=True, help="Run directory for queue output")
    parser.add_argument("--paper-ids", type=str, help="Comma-separated paper IDs (default: all in data/papers)")
    args = parser.parse_args()
    
    data_root = Path(args.data_root)
    papers_dir = data_root / "papers"
    
    if args.paper_ids:
        paper_ids = [p.strip() for p in args.paper_ids.split(",")]
    else:
        if not papers_dir.exists():
            print(f"Error: Papers directory not found: {papers_dir}")
            return
        paper_ids = [d.name for d in papers_dir.iterdir() if d.is_dir()]
    
    if not paper_ids:
        print("No papers found to process")
        return
    
    print(f"Building queue for {len(paper_ids)} papers...")
    n_tasks = build_queue(args.data_root, args.run_dir, paper_ids)
    print(f"Created {n_tasks} tasks in {args.run_dir}/task_queue.jsonl")


if __name__ == "__main__":
    main()
