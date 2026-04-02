# scripts/lib/io_jsonl.py
"""JSONL and JSON I/O utilities for the extraction pipeline."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class PathEncoder(json.JSONEncoder):
    """Custom JSON encoder that converts Path objects to strings."""
    def default(self, obj):
        if isinstance(obj, Path):
            return str(obj)
        return super().default(obj)


def _ensure_parent(path: Path) -> None:
    """Ensure parent directory exists."""
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: str | Path) -> Any:
    """Read a JSON file and return its contents."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str | Path, obj: Any) -> None:
    """Write an object to a JSON file with pretty formatting."""
    path = Path(path)
    _ensure_parent(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, cls=PathEncoder)


def read_jsonl(path: str | Path) -> List[Dict[str, Any]]:
    """Read a JSONL file and return a list of objects."""
    path = Path(path)
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def append_jsonl(path: str | Path, obj: Dict[str, Any]) -> None:
    """Append a single object to a JSONL file."""
    path = Path(path)
    _ensure_parent(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def write_jsonl(path: str | Path, rows: Iterable[Dict[str, Any]]) -> None:
    """Write multiple objects to a JSONL file (overwrites existing)."""
    path = Path(path)
    _ensure_parent(path)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def safe_get(d: Dict[str, Any], key: str, default=None):
    """Safely get a value from a dictionary."""
    return d.get(key, default)


def chunked(seq: List[Any], n: int) -> Iterable[List[Any]]:
    """Split a sequence into chunks of size n."""
    for i in range(0, len(seq), n):
        yield seq[i:i+n]
