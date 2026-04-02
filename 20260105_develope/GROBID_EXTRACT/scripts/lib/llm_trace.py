# scripts/lib/llm_trace.py
# -*- coding: utf-8 -*-
"""
LLM Tracing Module

Per 16_설계.md:
- TraceContext: bundles run/paper/case/task context for each LLM call
- TraceWriter: stores all LLM calls to run_dir/traces/ as JSONL + artifacts
- Ensures 100% traceability of all LLM calls with templates, variables, rendered prompts, responses
"""
from __future__ import annotations

import os
import json
import time
import uuid
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional, Iterable, List

# -----------------------------
# Helpers
# -----------------------------
def _now_ms() -> int:
    """Current timestamp in milliseconds."""
    return int(time.time() * 1000)


def _sha256_text(s: str) -> str:
    """SHA256 hash of a string."""
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def _ensure_dir(p: str) -> None:
    """Create directory if it doesn't exist."""
    os.makedirs(p, exist_ok=True)


def _json_dump(obj: Any) -> str:
    """JSON serialize with sorting and utf-8."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _redact_in_obj(obj: Any, redact_keys: Iterable[str]) -> Any:
    """
    Recursively redact sensitive keys in dict-like objects.
    Per 16_설계.md: API keys must be masked.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in redact_keys:
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact_in_obj(v, redact_keys)
        return out
    if isinstance(obj, list):
        return [_redact_in_obj(x, redact_keys) for x in obj]
    return obj


def _atomic_append_line(path: str, line: str) -> None:
    """
    Best-effort atomic append to a file.
    - open in append mode
    - flush + fsync
    Per 16_설계.md: Windows single-process safe.
    """
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
        f.flush()
        os.fsync(f.fileno())


# -----------------------------
# Trace Context
# -----------------------------
@dataclass
class TraceContext:
    """
    Context for a single LLM call.
    Per 16_설계.md Section 3-2: bundles all contextual information.
    """
    run_id: str
    paper_id: Optional[str] = None
    case_id: Optional[str] = None
    
    stage: Optional[str] = None       # e.g., "Stage4", "Stage2"
    task_type: Optional[str] = None   # e.g., "EXTRACT_EIS"
    extractor_id: Optional[str] = None
    
    chunk_id: Optional[str] = None
    figure_id: Optional[str] = None
    table_id: Optional[str] = None
    
    # Attempt tracking for self-correction
    attempt: int = 0
    max_attempts: int = 1
    parent_call_id: Optional[str] = None  # For retry chains
    
    # Free-form extra metadata
    meta: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict, filtering None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}


# -----------------------------
# Trace Writer
# -----------------------------
class TraceWriter:
    """
    Writes LLM call traces to run_dir/traces/.
    
    Per 16_설계.md Section 1 and 3-3:
    - llm_calls.jsonl: append-only log of all calls
    - artifacts/<call_id>/: template, variables, rendered_prompt, response, errors
    """
    
    def __init__(
        self,
        run_dir: str,
        trace_subdir: str = "traces",
        redact_keys: Optional[Iterable[str]] = None,
        max_inline_chars: int = 2000,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.run_dir = run_dir
        self.trace_dir = os.path.join(run_dir, trace_subdir)
        self.calls_path = os.path.join(self.trace_dir, "llm_calls.jsonl")
        self.artifacts_dir = os.path.join(self.trace_dir, "artifacts")
        self.max_inline_chars = max_inline_chars
        
        # Default keys to redact (per 16_설계.md Section 7)
        self.redact_keys = set(redact_keys or [
            "Authorization", "api_key", "API_KEY", 
            "GEMINI_API_KEY", "OPENAI_API_KEY",
            "access_token", "secret_key"
        ])
        
        if enabled:
            _ensure_dir(self.trace_dir)
            _ensure_dir(self.artifacts_dir)
    
    def new_call_id(self) -> str:
        """
        Generate a unique call ID.
        Format: <timestamp_ms>_<uuid_short>
        """
        return f"{_now_ms()}_{uuid.uuid4().hex[:12]}"
    
    def write_artifact_text(self, call_id: str, name: str, text: str) -> str:
        """
        Write a text artifact file.
        Returns the path to the artifact.
        """
        if not self.enabled:
            return ""
        d = os.path.join(self.artifacts_dir, call_id)
        _ensure_dir(d)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text if text is not None else "")
        return path
    
    def write_artifact_json(self, call_id: str, name: str, obj: Any) -> str:
        """
        Write a JSON artifact file with redaction.
        Returns the path to the artifact.
        """
        if not self.enabled:
            return ""
        d = os.path.join(self.artifacts_dir, call_id)
        _ensure_dir(d)
        path = os.path.join(d, name)
        safe = _redact_in_obj(obj, self.redact_keys)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_json_dump(safe))
        return path
    
    def append_call(self, record: Dict[str, Any]) -> None:
        """
        Append a call record to llm_calls.jsonl.
        Per 16_설계.md: atomic append-only.
        """
        if not self.enabled:
            return
        safe = _redact_in_obj(record, self.redact_keys)
        _atomic_append_line(self.calls_path, _json_dump(safe))
    
    def maybe_externalize_text(
        self, 
        call_id: str, 
        field_name: str, 
        text: str
    ) -> Dict[str, Any]:
        """
        For large text fields, store externally and return path reference.
        
        Returns dict with either:
        - {field_name: text, field_name_sha256: hash} if inline
        - {field_name_path: path, field_name_sha256: hash, field_name_chars: len} if external
        """
        if text is None:
            return {field_name: None}
        
        sha = _sha256_text(text)
        
        if len(text) <= self.max_inline_chars:
            return {field_name: text, f"{field_name}_sha256": sha}
        
        # Externalize large text
        p = self.write_artifact_text(call_id, f"{field_name}.txt", text)
        return {
            f"{field_name}_path": p,
            f"{field_name}_sha256": sha,
            f"{field_name}_chars": len(text)
        }
    
    def create_call_record(
        self,
        call_id: str,
        trace_ctx: Optional[TraceContext],
        model: str,
        provider: Optional[str],
        kind: str,  # "TEXT" or "JSON"
        cache_key: str,
        task_type: Optional[str] = None,
        prompt_file: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create base call record with common fields.
        """
        record = {
            "call_id": call_id,
            "ts_start_ms": _now_ms(),
            "kind": kind,
            "model": model,
            "provider": provider,
            "cache_key": cache_key,
            "task_type": task_type,
            "prompt_file": prompt_file,
        }
        
        if trace_ctx:
            record["trace_ctx"] = trace_ctx.to_dict()
            record["paper_id"] = trace_ctx.paper_id
            record["case_id"] = trace_ctx.case_id
            record["stage"] = trace_ctx.stage
            record["attempt"] = trace_ctx.attempt
            record["max_attempts"] = trace_ctx.max_attempts
            record["parent_call_id"] = trace_ctx.parent_call_id
        
        if extra:
            record["extra"] = extra
        
        return record
    
    def finalize_call_record(
        self,
        record: Dict[str, Any],
        ok: bool,
        error: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Finalize and append a call record.
        """
        record["ts_end_ms"] = _now_ms()
        record["latency_ms"] = record["ts_end_ms"] - record.get("ts_start_ms", 0)
        record["ok"] = ok
        
        if error:
            record["error"] = error
        
        self.append_call(record)


# -----------------------------
# Global Writer (Optional Singleton)
# -----------------------------
_global_writer: Optional[TraceWriter] = None


def get_global_writer() -> Optional[TraceWriter]:
    """Get the global trace writer if set."""
    return _global_writer


def set_global_writer(writer: TraceWriter) -> None:
    """Set the global trace writer."""
    global _global_writer
    _global_writer = writer


def init_trace_writer(run_dir: str, enabled: bool = True) -> TraceWriter:
    """
    Initialize and set the global trace writer.
    Call this once at pipeline start.
    """
    writer = TraceWriter(run_dir=run_dir, enabled=enabled)
    set_global_writer(writer)
    return writer
