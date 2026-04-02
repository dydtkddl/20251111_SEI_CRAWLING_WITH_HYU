# -*- coding: utf-8 -*-
"""
Supplementary GROBID JSONL Caption Cleaner (Production-grade)

Goal
- Read a JSONL produced by GROBID supplementary pipeline.
- Remove figure/table/scheme captions from:
  1) abstract_paragraphs
  2) sections[*].paragraphs (+ keep sentences aligned)
  3) caption_blocks (prevent leakage into kept JSONL)
- Export:
  - kept JSONL (cleaned docs)
  - removed JSONL (removed items only)
  - kept CSV (flat)
  - removed CSV (flat)
  - optional: method-like captions CSV/JSONL (captions that look like procedures)

Key upgrades
- Uses 'kind' to drop caption sections robustly
- Detects captions not only at paragraph start but also mid-paragraph (split)
- Prevents caption_blocks leakage (critical)
- Optionally preserves "method-like captions" (procedural captions in SI)

Usage
- Place this script next to your project and adjust BASE_DIR / INPUT_FILE / OUTPUT_DIR if needed.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# =============================================================================
# Configuration
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent

INPUT_FILE = BASE_DIR / "06_run_supplementary_out" / "supplementary_grobid_results.jsonl"
OUTPUT_DIR = BASE_DIR / "07_filter_captions_out"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

KEPT_JSONL = OUTPUT_DIR / "supplementary_grobid_results_clean.jsonl"
REMOVED_JSONL = OUTPUT_DIR / "removed_captions.jsonl"

KEPT_CSV = OUTPUT_DIR / "kept_content.csv"
REMOVED_CSV = OUTPUT_DIR / "removed_content.csv"

# Optional: preserve captions that look like methods/recipes (SI sometimes contains key procedures in captions)
SAVE_METHODLIKE = True
METHODLIKE_JSONL = OUTPUT_DIR / "methodlike_captions.jsonl"
METHODLIKE_CSV = OUTPUT_DIR / "methodlike_captions.csv"

# If True, drop caption_blocks entirely from kept docs (recommended)
DROP_CAPTION_BLOCKS_IN_KEPT = True

# =============================================================================
# Regex / Heuristics
# =============================================================================

# Caption labels (start or anywhere)
# Covers: Figure 1, Fig. 1, Figure S1, Fig S1, Table S1, Tab. 2, Scheme 1, etc.
CAPTION_LABEL_ANYWHERE_RE = re.compile(
    r"\b(?:Figure|Fig\.?|Table|Tab\.?|Scheme|Sch\.?|Chart|Graph)\s*(?:S?\d+[A-Za-z]?|[IVX]+|[A-Z])\b",
    re.IGNORECASE
)

CAPTION_START_RE = re.compile(
    r"^\s*(?:Figure|Fig\.?|Table|Tab\.?|Scheme|Sch\.?|Chart|Graph)\s*(?:S?\d+[A-Za-z]?|[IVX]+|[A-Z])\b",
    re.IGNORECASE
)

# Also allow SI-only labels like "S1." at the beginning (weaker)
S_ONLY_START_RE = re.compile(r"^\s*S\d+\s*[\.:]\s+", re.IGNORECASE)

# Detect result-ish lines we should NOT treat as captions by regex-only (guardrail)
# (Optional; keep conservative)
RESULTSISH_RE = re.compile(
    r"\b(results?|discussion|conclusion|performance|mechanism|regulation|evolution|behavior)\b",
    re.IGNORECASE
)

# "Method-like" heuristics:
# - procedure verbs + units/conditions
METHOD_VERB_RE = re.compile(
    r"\b(prepar|synthes|fabricat|coat|deposit|grow|immerse|dip|soak|spray|cast|anneal|dry|stir|mix|dissolv)\w*\b",
    re.IGNORECASE
)
UNIT_RE = re.compile(
    r"\b(\d+(\.\d+)?\s*(mg|g|kg|mL|L|µL|mmol|mol|M|wt%|vol%|°C|K|h|min|s|rpm))\b",
    re.IGNORECASE
)
CONDITION_RE = re.compile(
    r"\b(overnight|room temperature|RT|under vacuum|argon|nitrogen|air-dry|freeze-dry)\b",
    re.IGNORECASE
)


# =============================================================================
# Data model helpers
# =============================================================================
@dataclass
class RemovedItem:
    paper_id: str
    source: str
    heading: str
    index: int
    text: str
    reason: str
    action: str  # "removed" | "methodlike_kept"


def safe_get_paper_id(doc: Dict[str, Any], fallback_idx: int) -> str:
    paper_id = doc.get("paper_id", "") or doc.get("id", "")
    if paper_id:
        return str(paper_id)

    source = doc.get("source_file", "")
    if source:
        try:
            return Path(source).stem
        except Exception:
            return f"doc_{fallback_idx}"
    return f"doc_{fallback_idx}"


def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00ad", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


# =============================================================================
# Caption 판단 로직
# =============================================================================
def looks_like_caption_start(text: str) -> bool:
    """Caption-looking if starts with Figure/Fig/Table/... or SI-only S1. prefix."""
    if not text:
        return False
    t = normalize_text(text)
    if CAPTION_START_RE.match(t):
        # guardrail: sometimes section titles like "Figure of merit results" exist (rare)
        return True
    if S_ONLY_START_RE.match(t):
        return True
    return False


def find_caption_label_positions(text: str) -> List[Tuple[int, int, str]]:
    """
    Find all caption label occurrences anywhere in paragraph.
    Returns list of (start, end, matched_text).
    """
    t = normalize_text(text)
    out = []
    for m in CAPTION_LABEL_ANYWHERE_RE.finditer(t):
        out.append((m.start(), m.end(), m.group(0)))
    return out


def is_method_like_caption(text: str) -> bool:
    """
    Captions sometimes contain procedures in SI.
    We keep them optionally in a separate bucket.
    """
    t = normalize_text(text)
    if not t:
        return False
    # must have at least one method verb
    if not METHOD_VERB_RE.search(t):
        return False
    # and at least one unit/condition signal
    if UNIT_RE.search(t) or CONDITION_RE.search(t):
        return True
    return False


def split_mid_paragraph_caption(text: str) -> Optional[Tuple[str, str, str]]:
    """
    If a caption label appears mid-paragraph, split into (kept_prefix, removed_suffix, label).
    We only split when the label is not at the very beginning, and looks plausible.
    """
    t = normalize_text(text)
    if not t:
        return None

    positions = find_caption_label_positions(t)
    if not positions:
        return None

    # if the first match starts at 0 -> it's start caption (handled elsewhere)
    first_start, first_end, first_label = positions[0]
    if first_start <= 0:
        return None

    # Basic plausibility: require some reasonable prefix length
    if first_start < 20:
        # too close to beginning; could be legitimate start caption
        return None

    prefix = t[:first_start].strip()
    suffix = t[first_start:].strip()

    # If suffix is tiny, ignore
    if len(suffix) < 10:
        return None

    # avoid splitting in highly results-like sentences if you want; keep conservative
    # (But captions in SI can appear after results too. So do NOT block by RESULTSISH here.)
    return prefix, suffix, first_label


def heading_is_caption_like(heading: str) -> bool:
    h = normalize_text(heading)
    if not h:
        return False
    return bool(looks_like_caption_start(h) or CAPTION_START_RE.match(h))


def section_kind_is_caption(kind: str) -> bool:
    k = (kind or "").strip().lower()
    return k in {"figure_caption", "table_caption", "caption_from_body", "table_content"}


# =============================================================================
# Core processing
# =============================================================================
def process_document(doc: Dict[str, Any], doc_idx: int) -> Tuple[Dict[str, Any], List[RemovedItem], List[RemovedItem]]:
    """
    Returns:
      - cleaned_doc
      - removed_items
      - methodlike_items (captions that were not removed but preserved in methodlike bucket)
    """
    paper_id = safe_get_paper_id(doc, doc_idx)

    removed: List[RemovedItem] = []
    methodlike: List[RemovedItem] = []

    # -------------------------
    # 0) caption_blocks leakage handling
    # -------------------------
    caption_blocks = doc.get("caption_blocks", None)

    if DROP_CAPTION_BLOCKS_IN_KEPT:
        if caption_blocks is not None:
            # log removed caption_blocks (optional, but useful)
            # treat each caption block paragraph as removed
            if isinstance(caption_blocks, list):
                for bi, b in enumerate(caption_blocks):
                    heading = normalize_text(b.get("heading", "")) if isinstance(b, dict) else ""
                    paras = b.get("paragraphs", []) if isinstance(b, dict) else []
                    if isinstance(paras, list):
                        for pi, ptxt in enumerate(paras):
                            txt = normalize_text(str(ptxt))
                            if not txt:
                                continue
                            removed.append(RemovedItem(
                                paper_id=paper_id,
                                source=f"caption_blocks[{bi}]",
                                heading=heading,
                                index=pi,
                                text=txt,
                                reason="caption_blocks field removed from kept JSONL (leak prevention)",
                                action="removed"
                            ))
            doc.pop("caption_blocks", None)
    else:
        # If you prefer to keep caption_blocks but filter them:
        if isinstance(caption_blocks, list):
            new_blocks = []
            for bi, b in enumerate(caption_blocks):
                if not isinstance(b, dict):
                    continue
                heading = normalize_text(b.get("heading", ""))
                paras = b.get("paragraphs", [])
                if not isinstance(paras, list):
                    continue

                # Decide per paragraph
                kept_paras = []
                for pi, ptxt in enumerate(paras):
                    txt = normalize_text(str(ptxt))
                    if not txt:
                        continue
                    if looks_like_caption_start(txt) or heading_is_caption_like(heading):
                        removed.append(RemovedItem(
                            paper_id=paper_id,
                            source=f"caption_blocks[{bi}]",
                            heading=heading,
                            index=pi,
                            text=txt,
                            reason="Caption detected in caption_blocks",
                            action="removed"
                        ))
                    else:
                        kept_paras.append(txt)

                if kept_paras:
                    nb = dict(b)
                    nb["paragraphs"] = kept_paras
                    new_blocks.append(nb)

            doc["caption_blocks"] = new_blocks

    # -------------------------
    # 1) abstract_paragraphs
    # -------------------------
    new_abs: List[str] = []
    abs_list = doc.get("abstract_paragraphs", [])
    if isinstance(abs_list, list):
        for ai, ptxt in enumerate(abs_list):
            txt = normalize_text(str(ptxt))
            if not txt:
                continue

            # mid-paragraph split first
            mid = split_mid_paragraph_caption(txt)
            if mid:
                prefix, suffix, lab = mid
                # keep prefix
                if prefix:
                    new_abs.append(prefix)
                # remove suffix as caption
                if SAVE_METHODLIKE and is_method_like_caption(suffix):
                    methodlike.append(RemovedItem(
                        paper_id=paper_id, source="abstract", heading="", index=ai,
                        text=suffix, reason=f"Mid-paragraph caption split (label={lab}) -> methodlike", action="methodlike_kept"
                    ))
                else:
                    removed.append(RemovedItem(
                        paper_id=paper_id, source="abstract", heading="", index=ai,
                        text=suffix, reason=f"Mid-paragraph caption split (label={lab})", action="removed"
                    ))
                continue

            # start caption?
            if looks_like_caption_start(txt):
                if SAVE_METHODLIKE and is_method_like_caption(txt):
                    methodlike.append(RemovedItem(
                        paper_id=paper_id, source="abstract", heading="", index=ai,
                        text=txt, reason="Caption-like but method-like (kept separately)", action="methodlike_kept"
                    ))
                else:
                    removed.append(RemovedItem(
                        paper_id=paper_id, source="abstract", heading="", index=ai,
                        text=txt, reason="Caption start pattern in abstract", action="removed"
                    ))
                continue

            new_abs.append(txt)
    doc["abstract_paragraphs"] = new_abs

    # -------------------------
    # 2) sections
    # -------------------------
    new_sections: List[Dict[str, Any]] = []
    sections = doc.get("sections", [])

    if isinstance(sections, list):
        for si, sec in enumerate(sections):
            if not isinstance(sec, dict):
                continue

            kind = str(sec.get("kind", "section"))
            heading = normalize_text(sec.get("heading", ""))

            # If section itself is caption container -> remove whole section
            is_caption_section = section_kind_is_caption(kind) or heading_is_caption_like(heading)

            if is_caption_section:
                paras = sec.get("paragraphs", [])
                if isinstance(paras, list):
                    for pi, ptxt in enumerate(paras):
                        txt = normalize_text(str(ptxt))
                        if not txt:
                            continue
                        if SAVE_METHODLIKE and is_method_like_caption(txt):
                            methodlike.append(RemovedItem(
                                paper_id=paper_id,
                                source=f"section_{si}",
                                heading=heading,
                                index=pi,
                                text=txt,
                                reason=f"Caption section dropped ({kind}) but method-like kept separately",
                                action="methodlike_kept"
                            ))
                        else:
                            removed.append(RemovedItem(
                                paper_id=paper_id,
                                source=f"section_{si}",
                                heading=heading,
                                index=pi,
                                text=txt,
                                reason=f"Caption section dropped ({kind})",
                                action="removed"
                            ))
                continue  # drop entire section

            # Otherwise filter paragraphs inside normal section
            paras = sec.get("paragraphs", [])
            sents_list = sec.get("sentences", [])

            if not isinstance(paras, list):
                paras = []
            if not isinstance(sents_list, list):
                sents_list = []

            new_paras: List[str] = []
            new_sents: List[Any] = []

            for pi, ptxt in enumerate(paras):
                txt = normalize_text(str(ptxt))
                if not txt:
                    continue

                # align sentences
                sents = sents_list[pi] if pi < len(sents_list) else []

                # mid-paragraph split
                mid = split_mid_paragraph_caption(txt)
                if mid:
                    prefix, suffix, lab = mid
                    if prefix:
                        new_paras.append(prefix)
                        new_sents.append(sents)  # sentence alignment is imperfect after split; keep original
                    # suffix removed or preserved
                    if SAVE_METHODLIKE and is_method_like_caption(suffix):
                        methodlike.append(RemovedItem(
                            paper_id=paper_id,
                            source=f"section_{si}",
                            heading=heading,
                            index=pi,
                            text=suffix,
                            reason=f"Mid-paragraph caption split (label={lab}) -> methodlike",
                            action="methodlike_kept"
                        ))
                    else:
                        removed.append(RemovedItem(
                            paper_id=paper_id,
                            source=f"section_{si}",
                            heading=heading,
                            index=pi,
                            text=suffix,
                            reason=f"Mid-paragraph caption split (label={lab})",
                            action="removed"
                        ))
                    continue

                # start caption?
                if looks_like_caption_start(txt):
                    if SAVE_METHODLIKE and is_method_like_caption(txt):
                        methodlike.append(RemovedItem(
                            paper_id=paper_id,
                            source=f"section_{si}",
                            heading=heading,
                            index=pi,
                            text=txt,
                            reason="Caption-like paragraph but method-like kept separately",
                            action="methodlike_kept"
                        ))
                    else:
                        removed.append(RemovedItem(
                            paper_id=paper_id,
                            source=f"section_{si}",
                            heading=heading,
                            index=pi,
                            text=txt,
                            reason="Caption start pattern (paragraph)",
                            action="removed"
                        ))
                    continue

                # Keep
                new_paras.append(txt)
                new_sents.append(sents)

            # update
            sec["paragraphs"] = new_paras
            sec["sentences"] = new_sents

            # Keep section if it still has content OR meaningful heading
            if new_paras or heading:
                new_sections.append(sec)

    doc["sections"] = new_sections

    return doc, removed, methodlike


# =============================================================================
# Writers
# =============================================================================
def write_jsonl(path: Path, rows: List[Dict[str, Any]]):
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_removed_jsonl(path: Path, items: List[RemovedItem]):
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it.__dict__, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            # ensure all keys exist
            out = {k: r.get(k, "") for k in fieldnames}
            w.writerow(out)


# =============================================================================
# Main
# =============================================================================
def process_file():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Input JSONL not found: {INPUT_FILE}")

    kept_docs: List[Dict[str, Any]] = []
    removed_items: List[RemovedItem] = []
    methodlike_items: List[RemovedItem] = []

    kept_flat_rows: List[Dict[str, Any]] = []
    removed_flat_rows: List[Dict[str, Any]] = []
    methodlike_flat_rows: List[Dict[str, Any]] = []

    total_lines = 0
    invalid_lines = 0

    with INPUT_FILE.open("r", encoding="utf-8") as f:
        for li, line in enumerate(f):
            total_lines += 1
            line = line.strip()
            if not line:
                continue

            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue

            if not isinstance(doc, dict):
                invalid_lines += 1
                continue

            paper_id = safe_get_paper_id(doc, li)

            cleaned, removed, methodlike = process_document(doc, li)
            kept_docs.append(cleaned)
            removed_items.extend(removed)
            methodlike_items.extend(methodlike)

            # Flatten kept content for CSV (abstract + section paragraphs)
            # Abstract
            abs_list = cleaned.get("abstract_paragraphs", [])
            if isinstance(abs_list, list):
                for ai, ptxt in enumerate(abs_list):
                    kept_flat_rows.append({
                        "paper_id": paper_id,
                        "source": "abstract",
                        "heading": "",
                        "index": ai,
                        "text": normalize_text(str(ptxt)),
                    })

            # Sections
            sec_list = cleaned.get("sections", [])
            if isinstance(sec_list, list):
                for si, sec in enumerate(sec_list):
                    if not isinstance(sec, dict):
                        continue
                    heading = normalize_text(sec.get("heading", ""))
                    paras = sec.get("paragraphs", [])
                    if not isinstance(paras, list):
                        continue
                    for pi, ptxt in enumerate(paras):
                        kept_flat_rows.append({
                            "paper_id": paper_id,
                            "source": f"section_{si}",
                            "heading": heading,
                            "index": pi,
                            "text": normalize_text(str(ptxt)),
                        })

            # Flatten removed content
            for it in removed:
                removed_flat_rows.append({
                    "paper_id": it.paper_id,
                    "source": it.source,
                    "heading": it.heading,
                    "index": it.index,
                    "text": it.text,
                    "reason": it.reason,
                    "action": it.action,
                })

            for it in methodlike:
                methodlike_flat_rows.append({
                    "paper_id": it.paper_id,
                    "source": it.source,
                    "heading": it.heading,
                    "index": it.index,
                    "text": it.text,
                    "reason": it.reason,
                    "action": it.action,
                })

    # Write outputs
    write_jsonl(KEPT_JSONL, kept_docs)
    write_removed_jsonl(REMOVED_JSONL, removed_items)
    write_csv(KEPT_CSV, kept_flat_rows, ["paper_id", "source", "heading", "index", "text"])
    write_csv(REMOVED_CSV, removed_flat_rows, ["paper_id", "source", "heading", "index", "text", "reason", "action"])

    if SAVE_METHODLIKE:
        # methodlike as JSONL/CSV
        write_removed_jsonl(METHODLIKE_JSONL, methodlike_items)
        write_csv(METHODLIKE_CSV, methodlike_flat_rows, ["paper_id", "source", "heading", "index", "text", "reason", "action"])

    # Summary
    print("=" * 72)
    print("Caption Cleaning Done")
    print(f"Input: {INPUT_FILE}")
    print(f"Kept JSONL: {KEPT_JSONL}")
    print(f"Removed JSONL: {REMOVED_JSONL}")
    print(f"Kept CSV: {KEPT_CSV}")
    print(f"Removed CSV: {REMOVED_CSV}")
    if SAVE_METHODLIKE:
        print(f"Method-like JSONL: {METHODLIKE_JSONL}")
        print(f"Method-like CSV: {METHODLIKE_CSV}")
    print("-" * 72)
    print(f"Total lines read: {total_lines}")
    print(f"Invalid JSON lines: {invalid_lines}")
    print(f"Kept documents: {len(kept_docs)}")
    print(f"Removed items: {len(removed_items)}")
    print(f"Method-like items preserved: {len(methodlike_items)}")
    print("=" * 72)


if __name__ == "__main__":
    process_file()
