# -*- coding: utf-8 -*-
"""
LLM 기반 Supplementary Section 분류 (Heading + Paragraph Content)
파일명 예: 07_2.filter_sup_sections_by_llm_with_content.py

목표
- supplementary_grobid_results_clean.jsonl에서 "섹션(heading + paragraphs)" 단위로
  Experimental/Methods/Synthesis/Preparation(특히 Zn anode ex-situ 보호층/코팅 관련) 여부를 LLM으로 판별.
- 결과를 섹션 단위 JSONL/CSV + 문서 요약 + YES 섹션 모음으로 저장.

특징(상업용 운영 지향)
- 섹션 텍스트를 통째로 넣되, 너무 길면 max_chars 한도에서 "레시피 가능성 높은 문단 우선" 샘플링
- rule-based prefilter로 명백한 NO를 LLM 호출 없이 컷
- 디스크 캐시로 동일 입력 재분류 방지(cache.jsonl)
- Ollama(/api/generate) 기반 robust JSON 파싱 (코드블럭/잡음 제거)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from tqdm import tqdm

try:
    import pandas as pd  # optional
except Exception:
    pd = None

# =============================================================================
# Paths (defaults)
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_JSONL = BASE_DIR / "07_filter_captions_out" / "supplementary_grobid_results_clean.jsonl"
DEFAULT_OUTPUT_DIR = BASE_DIR / "07_filter_captions_out" / "classification_results_sup_sections"

# =============================================================================
# Prompt (Heading + Content)
# =============================================================================
CLASSIFICATION_PROMPT = """You are an expert in analyzing SUPPLEMENTARY INFORMATION (SI) sections for aqueous zinc-ion batteries (AZIB), focusing on Zn ANODE protective layers formed EX-SITU (pre-cycling coatings / artificial interphases / engineered interfaces).

You will receive:
1) A section heading
2) The section content (paragraphs)

Task:
Decide if this section is LIKELY to contain EXPERIMENTAL METHODS / MATERIAL PREPARATION / SYNTHESIS / FABRICATION procedures relevant to:
- Zn anode surface treatment or protective layer formation (ex-situ pre-cycling), OR
- AZIB cell/battery assembly / electrolyte preparation / protocol-like methods.

IMPORTANT:
- We optimize for RECIPE / PREPARATION evidence (steps, materials, conditions), NOT results.
- Do NOT answer YES just because you see characterization terms or electrochemical measurement names.

========================
HARD-NO (OVERRIDE) ❌
Answer NO if the content is mainly:
- results/discussion/analysis/performance/mechanism/regulation/evolution
- characterization-only descriptions without fabrication/protocol steps
- electrochemical measurement/testing descriptions without fabrication/protocol steps
- figure/table/caption-like content only

Examples of NO-dominant signals:
"electrochemical performance", "cycling performance", "mechanism", "morphology evolution",
"XRD patterns", "SEM images", "CV curves", "EIS spectra", "results and discussion"

========================
HARD-YES (OVERRIDE) ✅
Answer YES if the content includes ANY clear method/protocol/recipe signals such as:
- preparation/fabrication/synthesis/coating/deposition/growth/dip-coating/soaking/drying/annealing steps
- units/conditions (mg, mL, mmol, °C, h, rpm, vacuum, etc.) in procedural context
- Zn foil/anode treatment steps, coated Zn specimen preparation
- electrolyte preparation or cell assembly protocols

Also YES if:
- Heading or content indicates broad experimental/methods umbrella sections AND the content contains protocol-like steps.

========================
DECISION OUTPUT FORMAT (STRICT)
Respond ONLY with a JSON object in this exact format:
{{"decision":"YES/NO","confidence":0.0-1.0,"reason":"brief explanation referencing heading/content"}}

========================
INPUT
Section heading: "{heading}"

Section content (may be truncated if very long):
\"\"\"
{content}
\"\"\"
"""

# =============================================================================
# Heuristics (prefilter + content sampling)
# =============================================================================
RESULTSISH_RE = re.compile(
    r"\b(results?|discussion|conclusion|summary|findings|analysis|performance|mechanism|regulation|evolution|behavior|kinetics|dynamics)\b",
    re.IGNORECASE
)

MEASURE_ONLY_RE = re.compile(
    r"\b(electrochemical measurements?|electrochemical testing|measurement methods?|characterization techniques?)\b",
    re.IGNORECASE
)

CAPTIONISH_RE = re.compile(
    r"^\s*(Figure|Fig\.?|Table|Tab\.?|Scheme|Sch\.?)\s*(S?\d+|[IVX]+|[A-Z])\b",
    re.IGNORECASE
)

# Method verbs / units used for scoring & rule YES (content-based)
METHOD_VERB_RE = re.compile(
    r"\b(prepar|synthes|fabricat|coat|deposit|grow|immerse|dip|soak|spray|cast|anneal|dry|stir|mix|dissolv|filter|wash)\w*\b",
    re.IGNORECASE
)
UNIT_RE = re.compile(
    r"\b(\d+(\.\d+)?\s*(mg|g|kg|mL|L|µL|mmol|mol|M|wt%|vol%|°C|K|h|min|s|rpm))\b",
    re.IGNORECASE
)
ZN_SIGNAL_RE = re.compile(
    r"\b(zinc|zn)\b|@zn|zn@\b|zn\s*foil\b|anode\b|negative electrode\b|protective layer\b|artificial layer\b|interface layer\b",
    re.IGNORECASE
)

def normalize_heading(h: str) -> str:
    if not h:
        return ""
    h = h.strip()
    # common SI numbering removal
    patterns = [
        r'^[SVs]*\d+(\.\d+)*\s*[\.\)]?\s*',      # S1. 2.1. etc
        r'^\([0-9]+\)\s*',
        r'^[0-9]+\)\s*',
        r'^\[[0-9]+\]\s*',
    ]
    for p in patterns:
        h = re.sub(p, "", h, flags=re.IGNORECASE)
    return h.strip()

def stable_doc_id(doc: Dict[str, Any], fallback_i: int) -> str:
    for k in ("paper_id", "id", "source_file"):
        v = doc.get(k, "")
        if v:
            if k == "source_file":
                try:
                    return Path(str(v)).stem
                except Exception:
                    return f"doc_{fallback_i}"
            return str(v)
    return f"doc_{fallback_i}"

def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def is_obvious_no(heading: str, content: str) -> Optional[str]:
    """
    Return reason string if we can confidently mark NO without LLM.
    Conservative: only do for clear cases.
    """
    h = (heading or "").strip()
    c = (content or "").strip()
    hh = h.lower()

    # caption-like section
    if CAPTIONISH_RE.match(h):
        return "Caption-like heading (Figure/Table/Scheme)."

    # pure measurement-only heading and content lacks method verbs/units
    if MEASURE_ONLY_RE.search(hh) and not (METHOD_VERB_RE.search(c) and UNIT_RE.search(c)):
        return "Measurement/characterization-only heading and no procedural recipe in content."

    # results-ish dominates both heading and content (and no method signal)
    if RESULTSISH_RE.search(hh) and not (METHOD_VERB_RE.search(c) and UNIT_RE.search(c)):
        return "Results/analysis-oriented heading with no clear procedural recipe in content."

    return None

def paragraph_score(p: str) -> int:
    """
    Score paragraph by how likely it contains recipe/method evidence.
    Higher score => more likely useful to show to LLM.
    """
    if not p:
        return 0
    s = 0
    if METHOD_VERB_RE.search(p):
        s += 3
    if UNIT_RE.search(p):
        s += 3
    if ZN_SIGNAL_RE.search(p):
        s += 2
    # penalize obvious caption-ish
    if CAPTIONISH_RE.match(p.strip()):
        s -= 3
    # penalize results-ish
    if RESULTSISH_RE.search(p):
        s -= 1
    return s

def build_section_content(
    paragraphs: List[str],
    max_chars: int,
    max_paras: int,
) -> Tuple[str, Dict[str, Any]]:
    """
    Build content string for LLM:
    - prioritize high-score paragraphs
    - also keep some beginning/end context if room
    """
    paras = [p.strip() for p in paragraphs if isinstance(p, str) and p.strip()]
    meta = {
        "total_paras": len(paras),
        "total_chars": sum(len(p) for p in paras),
        "used_paras": 0,
        "used_chars": 0,
        "truncated": False,
    }
    if not paras:
        return "", meta

    # rank by score
    scored = [(paragraph_score(p), i, p) for i, p in enumerate(paras)]
    scored.sort(key=lambda x: (x[0], -len(x[2])), reverse=True)

    selected: List[Tuple[int, str]] = []  # (orig_idx, paragraph)
    used = 0

    # pick top scored paragraphs first
    for _, i, p in scored:
        if len(selected) >= max_paras:
            break
        if used + len(p) + 2 > max_chars:
            continue
        selected.append((i, p))
        used += len(p) + 2

    # add some head/tail context if room (avoid duplicates)
    def try_add(idx: int):
        nonlocal used
        if idx < 0 or idx >= len(paras):
            return
        if any(i == idx for i, _ in selected):
            return
        p = paras[idx]
        if len(selected) >= max_paras:
            return
        if used + len(p) + 2 > max_chars:
            return
        selected.append((idx, p))
        used += len(p) + 2

    # add first 1~2 paragraphs if room
    try_add(0)
    try_add(1)
    # add last 1~2 paragraphs if room
    try_add(len(paras) - 2)
    try_add(len(paras) - 1)

    # sort back to original order
    selected.sort(key=lambda x: x[0])

    content = "\n\n".join(p for _, p in selected)
    meta["used_paras"] = len(selected)
    meta["used_chars"] = len(content)
    meta["truncated"] = (meta["used_paras"] < meta["total_paras"]) or (meta["used_chars"] < meta["total_chars"])
    return content, meta

# =============================================================================
# Ollama client + robust JSON parse
# =============================================================================
def extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """
    Try to extract a JSON object from noisy LLM output.
    Supports code fences and extra text.
    """
    if not text:
        return None
    t = text.strip()

    # strip code fences
    if "```" in t:
        # prefer ```json
        if "```json" in t:
            t = t.split("```json", 1)[1]
            t = t.split("```", 1)[0].strip()
        else:
            # take the first fenced block
            parts = t.split("```")
            if len(parts) >= 2:
                t = parts[1].strip()

    # direct parse
    try:
        return json.loads(t)
    except Exception:
        pass

    # find first {...}
    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if not m:
        return None
    candidate = m.group(0)
    try:
        return json.loads(candidate)
    except Exception:
        return None

def call_ollama_llm(
    heading: str,
    content: str,
    ollama_url: str,
    model: str,
    timeout: int,
    temperature: float,
    top_p: float,
    max_retries: int,
    backoff_sec: float,
) -> Dict[str, Any]:
    prompt = CLASSIFICATION_PROMPT.format(heading=heading, content=content)

    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "top_p": top_p,
                    }
                },
                timeout=timeout
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                time.sleep(backoff_sec * (attempt + 1))
                continue

            result = r.json()
            out = (result.get("response") or "").strip()
            parsed = extract_first_json_object(out)
            if not parsed or "decision" not in parsed:
                return {"decision": "ERROR", "confidence": 0.0, "reason": "LLM output not parseable as required JSON"}

            decision = str(parsed.get("decision", "")).strip().upper()
            if decision not in ("YES", "NO"):
                return {"decision": "ERROR", "confidence": 0.0, "reason": f"Invalid decision: {decision}"}

            conf = parsed.get("confidence", 0.5)
            try:
                conf = float(conf)
            except Exception:
                conf = 0.5
            conf = max(0.0, min(1.0, conf))

            reason = str(parsed.get("reason", "")).strip()
            return {"decision": decision, "confidence": conf, "reason": reason}

        except requests.Timeout:
            last_err = "Timeout"
            time.sleep(backoff_sec * (attempt + 1))
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(backoff_sec * (attempt + 1))

    return {"decision": "ERROR", "confidence": 0.0, "reason": f"LLM call failed after retries: {last_err}"}

# =============================================================================
# Cache (disk)
# =============================================================================
def load_cache(cache_path: Path) -> Dict[str, Dict[str, Any]]:
    cache: Dict[str, Dict[str, Any]] = {}
    if not cache_path.exists():
        return cache
    with cache_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                k = obj.get("cache_key", "")
                if k:
                    cache[k] = obj
            except Exception:
                continue
    return cache

def append_cache(cache_path: Path, entry: Dict[str, Any]) -> None:
    with cache_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

# =============================================================================
# Section extraction (supports children if present)
# =============================================================================
def iter_sections(sections: Any) -> Iterable[Tuple[List[int], Dict[str, Any]]]:
    """
    Yield (path_indices, section_dict)
    Supports both flat and tree with children.
    """
    def rec(nodes: Any, path: List[int]):
        if not isinstance(nodes, list):
            return
        for i, sec in enumerate(nodes):
            if not isinstance(sec, dict):
                continue
            yield path + [i], sec
            children = sec.get("children", None)
            if children:
                yield from rec(children, path + [i])

    yield from rec(sections, [])

# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="LLM Based Supplementary Section Classification (Heading + Content)")
    parser.add_argument("--input_jsonl", default=str(DEFAULT_INPUT_JSONL), help="Input cleaned supplementary JSONL")
    parser.add_argument("--out_dir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory")
    parser.add_argument("--ollama_url", default="http://localhost:11434", help="Ollama server URL")
    parser.add_argument("--llm_model", default="qwen2.5:14b-instruct", help="LLM model name")

    parser.add_argument("--max_chars", type=int, default=12000, help="Max chars of section content sent to LLM")
    parser.add_argument("--max_paras", type=int, default=30, help="Max paragraphs sampled per section to LLM")
    parser.add_argument("--min_paras", type=int, default=1, help="Minimum paragraphs required to classify a section")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds for Ollama")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=1.5)

    parser.add_argument("--no_prefilter", action="store_true", help="Disable rule-based obvious NO shortcut")
    parser.add_argument("--use_cache", action="store_true", help="Enable disk cache to avoid re-calling LLM")
    parser.add_argument("--cache_file", default="", help="Cache JSONL path (default: out_dir/cache.jsonl)")

    args = parser.parse_args()

    input_path = Path(args.input_jsonl)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise FileNotFoundError(f"Input JSONL not found: {input_path}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    out_sections_jsonl = out_dir / f"supp_sections_classification_{ts}.jsonl"
    out_sections_csv = out_dir / f"supp_sections_classification_{ts}.csv"
    out_doc_summary_json = out_dir / f"supp_docs_summary_{ts}.json"
    out_doc_summary_csv = out_dir / f"supp_docs_summary_{ts}.csv"
    out_yes_sections_jsonl = out_dir / f"supp_sections_yes_{ts}.jsonl"

    cache_path = Path(args.cache_file) if args.cache_file else (out_dir / "cache.jsonl")
    cache: Dict[str, Dict[str, Any]] = {}
    if args.use_cache:
        cache = load_cache(cache_path)

    # accumulate
    section_rows: List[Dict[str, Any]] = []
    doc_summary: Dict[str, Dict[str, Any]] = {}

    yes_section_count = 0
    no_section_count = 0
    error_section_count = 0
    skipped_section_count = 0
    prefilter_no_count = 0
    cache_hit_count = 0

    # first pass count sections for progress bar
    total_sections = 0
    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except Exception:
                continue
            secs = doc.get("sections", [])
            for _, sec in iter_sections(secs):
                # sections may be empty or have no paragraphs
                total_sections += 1

    pbar = tqdm(total=total_sections, desc="Classifying sections")

    with input_path.open("r", encoding="utf-8") as f:
        for di, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except Exception:
                continue

            doc_id = stable_doc_id(doc, di)
            source_file = str(doc.get("source_file", ""))

            if doc_id not in doc_summary:
                doc_summary[doc_id] = {
                    "doc_id": doc_id,
                    "source_file": source_file,
                    "total_sections": 0,
                    "yes_sections": 0,
                    "no_sections": 0,
                    "error_sections": 0,
                    "skipped_sections": 0,
                }

            sections = doc.get("sections", [])
            for path_idx, sec in iter_sections(sections):
                doc_summary[doc_id]["total_sections"] += 1
                pbar.update(1)

                heading_raw = str(sec.get("heading", "") or "")
                heading = normalize_heading(heading_raw)

                paras = sec.get("paragraphs", [])
                if not isinstance(paras, list):
                    paras = []

                # require min_paras to classify
                if len([p for p in paras if isinstance(p, str) and p.strip()]) < args.min_paras:
                    skipped_section_count += 1
                    doc_summary[doc_id]["skipped_sections"] += 1
                    section_rows.append({
                        "doc_id": doc_id,
                        "source_file": source_file,
                        "section_path": ".".join(map(str, path_idx)),
                        "heading": heading,
                        "decision": "SKIP",
                        "confidence": 0.0,
                        "reason": f"Too few paragraphs (<{args.min_paras})",
                        "used_paras": 0,
                        "total_paras": len(paras),
                        "truncated": False,
                        "cache_hit": False,
                        "prefiltered": False,
                    })
                    continue

                content, meta = build_section_content(paras, max_chars=args.max_chars, max_paras=args.max_paras)

                # prefilter obvious NO
                if not args.no_prefilter:
                    reason_no = is_obvious_no(heading, content)
                    if reason_no:
                        prefilter_no_count += 1
                        no_section_count += 1
                        doc_summary[doc_id]["no_sections"] += 1

                        section_rows.append({
                            "doc_id": doc_id,
                            "source_file": source_file,
                            "section_path": ".".join(map(str, path_idx)),
                            "heading": heading,
                            "decision": "NO",
                            "confidence": 0.95,
                            "reason": f"[PREFILTER] {reason_no}",
                            "used_paras": meta["used_paras"],
                            "total_paras": meta["total_paras"],
                            "truncated": meta["truncated"],
                            "cache_hit": False,
                            "prefiltered": True,
                        })
                        continue

                # cache key
                cache_key = sha1_text((heading + "\n\n" + content).strip())
                if args.use_cache and cache_key in cache:
                    cache_hit_count += 1
                    cached = cache[cache_key]
                    decision = cached.get("decision", "ERROR")
                    conf = float(cached.get("confidence", 0.0))
                    reason = str(cached.get("reason", ""))
                else:
                    # call LLM
                    result = call_ollama_llm(
                        heading=heading,
                        content=content,
                        ollama_url=args.ollama_url,
                        model=args.llm_model,
                        timeout=args.timeout,
                        temperature=args.temperature,
                        top_p=args.top_p,
                        max_retries=args.retries,
                        backoff_sec=args.backoff,
                    )
                    decision = result["decision"]
                    conf = result["confidence"]
                    reason = result["reason"]

                    if args.use_cache:
                        entry = {
                            "cache_key": cache_key,
                            "heading": heading,
                            "decision": decision,
                            "confidence": conf,
                            "reason": reason,
                            "model": args.llm_model,
                            "created_at": ts,
                        }
                        append_cache(cache_path, entry)
                        cache[cache_key] = entry

                # update counters
                if decision == "YES":
                    yes_section_count += 1
                    doc_summary[doc_id]["yes_sections"] += 1
                elif decision == "NO":
                    no_section_count += 1
                    doc_summary[doc_id]["no_sections"] += 1
                else:
                    error_section_count += 1
                    doc_summary[doc_id]["error_sections"] += 1

                row = {
                    "doc_id": doc_id,
                    "source_file": source_file,
                    "section_path": ".".join(map(str, path_idx)),
                    "heading": heading,
                    "decision": decision,
                    "confidence": conf,
                    "reason": reason,
                    "used_paras": meta["used_paras"],
                    "total_paras": meta["total_paras"],
                    "truncated": meta["truncated"],
                    "cache_hit": bool(args.use_cache and cache_key in cache),
                    "prefiltered": False,
                    # store a short excerpt for auditing
                    "content_excerpt": (content[:800] + " ...") if len(content) > 900 else content,
                }
                section_rows.append(row)

    pbar.close()

    # Write section results JSONL
    with out_sections_jsonl.open("w", encoding="utf-8") as f:
        for r in section_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # YES sections only JSONL
    with out_yes_sections_jsonl.open("w", encoding="utf-8") as f:
        for r in section_rows:
            if r.get("decision") == "YES":
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Write CSV (using pandas if available)
    if pd is not None:
        df = pd.DataFrame(section_rows)
        df.to_csv(out_sections_csv, index=False, encoding="utf-8-sig")
    else:
        # csv module fallback
        fieldnames = list(section_rows[0].keys()) if section_rows else []
        with out_sections_csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in section_rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})

    # Doc summary outputs
    doc_rows = list(doc_summary.values())
    with out_doc_summary_json.open("w", encoding="utf-8") as f:
        json.dump(doc_rows, f, ensure_ascii=False, indent=2)

    if pd is not None:
        pd.DataFrame(doc_rows).to_csv(out_doc_summary_csv, index=False, encoding="utf-8-sig")
    else:
        fieldnames = list(doc_rows[0].keys()) if doc_rows else []
        with out_doc_summary_csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in doc_rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})

    # Summary print
    print("=" * 72)
    print("Supplementary Section Classification (Heading + Content) DONE")
    print(f"Input: {input_path}")
    print(f"Output dir: {out_dir}")
    print("-" * 72)
    print(f"Sections: YES={yes_section_count}, NO={no_section_count}, ERROR={error_section_count}, SKIP={skipped_section_count}")
    print(f"Prefilter NO: {prefilter_no_count}")
    print(f"Cache: {'ON' if args.use_cache else 'OFF'} | hits={cache_hit_count} | path={cache_path if args.use_cache else '(n/a)'}")
    print("-" * 72)
    print(f"Section JSONL: {out_sections_jsonl.name}")
    print(f"Section CSV : {out_sections_csv.name}")
    print(f"YES-only JSONL: {out_yes_sections_jsonl.name}")
    print(f"Doc summary JSON: {out_doc_summary_json.name}")
    print(f"Doc summary CSV : {out_doc_summary_csv.name}")
    print("=" * 72)

if __name__ == "__main__":
    main()
