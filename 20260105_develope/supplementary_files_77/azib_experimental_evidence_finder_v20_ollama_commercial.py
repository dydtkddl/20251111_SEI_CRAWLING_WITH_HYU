# -*- coding: utf-8 -*-
"""
AZIB Experimental Evidence Finder v20.0 (Commercial-grade + Ollama-assisted + Robust Cleanup)
===============================================================================================

What this is
------------
A "recipe-first" extractor for aqueous zinc-ion battery (AZIB) papers in Markdown form
(e.g., PDF-to-MD outputs). It extracts procedure evidence as *sentences* grouped by tags,
with a strong preference for reproducible recipes rather than terminology (in situ/ex situ
words are not decisive).

v20.0 upgrades v17/v18/v19.1 with additional real-world pain points:
- **Hierarchical heading boundaries** (e.g., "2. Experimental" must include "2.1 Materials", ...)
- **Plain heading detection** (standalone lines like "Experimental Section", "METHODS")
- **Numbered headings without dot** ("2 Experimental section")
- **Appendix / Supporting Information pointers** (stub "see SI" blocks)
- **Stronger frontmatter/author-affiliation removal** (author lists + institutions)
- **Stronger caption removal** (e.g., "Fig. S4 ..." / "Table S1 ...")
- **Regex safety fix**: avoid `re.VERBOSE` with literal `#` (prevents PatternError)
- Stronger sectionless fallback for papers with *no headings at all*
- More guardrails, warnings, and deterministic behavior

Tags (taxonomy)
---------------
- PROC_ZN            : Zn foil/anode surface treatment recipe (dip/coat/wash/dry/anneal/plate/...)
- PROC_COAT_MAT      : coating material/precursor synthesis or solution preparation recipe
- ELECTROLYTE        : electrolyte composition / additives / concentration / pH recipe
- ASSEMBLY           : cell assembly steps (coin/swagelok/pouch, separator, stacking)
- FORMATION_IN_SITU  : formation/activation/conditioning protocol to form SEI/layer during cycling
- CHAR               : characterization protocol (SEM/XRD/XPS/Raman/etc.) -> dropped
- ECHEM_TEST         : electrochemical test settings (CV/EIS/GCD etc.) -> dropped
- RESULT             : results/discussion claims -> dropped
- OTHER              : none of the above

Kept tags (for downstream classifier input)
-------------------------------------------
PROC_ZN, PROC_COAT_MAT, ELECTROLYTE, ASSEMBLY, FORMATION_IN_SITU

Outputs
-------
CSV columns (default, compatible with v17):
- File
- Found
- HitCount                      # number of extracted candidate blocks (not sentences)
- EvidenceJSON
- EvidenceJSON_Cleaned
- TaggedSentencesJSON
- CleanedChunksByTagJSON
- ProcZnCount
- FallbackUsed
- LLMCalls
- WarningsJSON

Optional (enable via --extended_csv):
- HeadingsJSON                  # all detected headings with levels and indices
- QAStatsJSON                   # counts for sanity check / QA

LLM (optional)
--------------
Supports local Ollama server via HTTP (/api/chat).
Two optional LLM features:
- --llm_refine_tags   : refine ambiguous sentence tags only (budgeted)
- --llm_recipe_miner  : mine recipe sentences from mixed paragraphs (budgeted)

Determinism notes
-----------------
- Default LLM temperature is 0.0
- Strict JSON validation is ON by default for LLM outputs
- On-disk cache is available to avoid repeat calls

Example (Windows PowerShell)
----------------------------
python azib_experimental_evidence_finder_v18_ollama_commercial.py ^
  --input_dir ..\pdfs_marker_output ^
  --output_csv out_v18.csv ^
  --only_true ^
  --min_proc_zn 2 ^
  --keep_context 2

Example (Ollama high precision)
-------------------------------
python azib_experimental_evidence_finder_v18_ollama_commercial.py ^
  --input_dir ..\pdfs_marker_output ^
  --output_csv out_v18_ollama.csv ^
  --only_true ^
  --min_proc_zn 3 ^
  --keep_context 2 ^
  --llm_backend ollama ^
  --ollama_url http://localhost:11434 ^
  --llm_model qwen2.5:14b-instruct ^
  --llm_refine_tags ^
  --llm_recipe_miner ^
  --llm_timeout 180 ^
  --llm_max_calls_per_doc 14 ^
  --llm_cache_dir .\.ollama_cache ^
  --extended_csv

Author: Anyongsan (AI Agent)
Date: 2026-01-06
License: Internal research/industrial use (no warranty).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# tqdm is optional; script must run without it.
try:
    from tqdm import tqdm  # type: ignore
except Exception:
    tqdm = None  # type: ignore

# urllib is used for Ollama HTTP calls (no external internet required).
import urllib.request
import urllib.error


# =============================================================================
# 0) Logging
# =============================================================================

LOGGER = logging.getLogger("azib_v18")

def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """
    Configure logging for both console and optional file.
    """
    LOGGER.setLevel(getattr(logging, level.upper(), logging.INFO))
    LOGGER.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    LOGGER.addHandler(sh)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        LOGGER.addHandler(fh)


# =============================================================================
# 1) Taxonomy / constants (v21: Enhanced with signals and provenance)
# =============================================================================

TAG_TAXONOMY: Tuple[str, ...] = (
    "PROC_ZN",
    "PROC_ZN_WEAK",  # v21: Zn processing with weak evidence (salt-only or no verb)
    "PROC_COAT_MAT",
    "ELECTROLYTE",
    "ASSEMBLY",
    "FORMATION_IN_SITU",
    "CHAR",
    "ECHEM_TEST",
    "RESULT",
    "SIMULATION",
    "OTHER",
)

TAG_KEEP: Tuple[str, ...] = (
    "PROC_ZN",
    "PROC_ZN_WEAK",  # Keep but flag for LLM to judge
    "PROC_COAT_MAT",
    "ELECTROLYTE",
    "ASSEMBLY",
    "FORMATION_IN_SITU",
)

TAG_SET = set(TAG_TAXONOMY)
KEEP_SET = set(TAG_KEEP)

# v21: Provenance types for trust scoring
PROVENANCE_TYPES = (
    "PRIMARY",           # From detected Methods/Experimental section
    "FALLBACK_HEADING",  # From heading-based fallback mining
    "FALLBACK_SENTENCE", # From sentence-level fallback mining
    "SECTIONLESS",       # From document with no headings
)

PROMPT_VERSION = "v21.0.0"

# =============================================================================
# v21: Signal patterns for LLM judgment support
# =============================================================================

# Zn METAL anchors (actual metal substrate)
ZN_METAL_ANCHOR_RE = re.compile(
    r"(?ix)\b("
    r"zn\s*(foil|plate|sheet|metal|anode|electrode)|"
    r"zinc\s*(foil|plate|sheet|metal|anode|electrode)|"
    r"bare\s+zn|pristine\s+zn|polished\s+zn|treated\s+zn|"
    r"modified\s+zn|coated\s+zn"
    r")\b"
)

# Zn SALT anchors (electrolyte salts - should NOT be PROC_ZN)
ZN_SALT_ANCHOR_RE = re.compile(
    r"(?ix)\b("
    r"znso4|zn\(so4\)|zn\(otf\)|zn\(tfsi\)|zn\(cf3so3\)|"
    r"zncl2|zn\(no3\)|zinc\s+sulfate|zinc\s+triflate|zinc\s+chloride|"
    r"zn2\+|zn\s*ions|zn\s+salt|zinc\s+salt"
    r")\b"
)

# v21: Time-axis signals (ex-situ vs in-situ distinction)
TIME_AXIS_PRE_RE = re.compile(
    r"(?ix)\b("
    r"before\s+cycling|prior\s+to\s+cycling|pre[-\s]?cycling|"
    r"before\s+assembly|prior\s+to\s+assembly|before\s+cell\s+assembly|"
    r"pretreated|pre[-\s]?treated|precoated|pre[-\s]?coated|"
    r"before\s+electrochemical|before\s+battery"
    r")\b"
)

TIME_AXIS_DURING_RE = re.compile(
    r"(?ix)\b("
    r"during\s+cycling|in[-\s]?situ\s+form|formed\s+during\s+cycling|"
    r"electrochemically\s+form|in\s+first\s+cycles?|"
    r"plating/stripping|built\s+during"
    r")\b"
)

# v21: Aqueous signals
AQUEOUS_SIGNAL_RE = re.compile(
    r"(?ix)\b("
    r"aqueous|water[-\s]?based|h2o|in\s+water|"
    r"aqueous\s+electrolyte|aqueous\s+solution|aqueous\s+znso4|"
    r"mild\s+aqueous|ph\s*[=\s]?\d"
    r")\b"
)

# v21: Lab-scale experiment signals (vs review/simulation)
LAB_SCALE_SIGNAL_RE = re.compile(
    r"(?ix)\b("
    r"coin\s*cell|cr2032|cr2016|swagelok|pouch\s*cell|"
    r"current\s+density|ma\s*cm|ma/cm|ma\s*g|mah\s*g|mah/g|"
    r"areal\s+capacity|mass\s+loading|areal\s+loading|"
    r"coulombic\s+efficiency|ce\s+of|cycling\s+stability|cycle\s+life|"
    r"charge[-\s]?discharge|galvanostatic"
    r")\b"
)

# =============================================================================
# 2) Text normalization and cleanup
# =============================================================================

INVISIBLE_CHARS = ["\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"]
WEIRD_SPACES = ["\u00a0", "\u202f", "\u2007", "\u2009", "\u200a"]

def normalize_invisibles(s: str) -> str:
    if not s:
        return s
    for ch in INVISIBLE_CHARS:
        s = s.replace(ch, "")
    for sp in WEIRD_SPACES:
        s = s.replace(sp, " ")
    return s

def strip_html_tags(text: str) -> str:
    """
    Aggressively removes HTML tags.
    """
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text)

def strip_markdown_emphasis(text: str) -> str:
    """
    Removes markdown emphasis markers (*, **, _, __).
    """
    if not text:
        return ""
    return re.sub(r"[*_]+", "", text)

# fenced and inline code removal (reduces noise)
FENCED_CODE_RE = re.compile(r"(?s)(```.*?```|~~~.*?~~~)")
INLINE_CODE_RE = re.compile(r"`[^`]*`")

# markdown links: [text](url) -> text
LINK_MD_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")

# table-ish HTML artifacts
HTML_TABLEISH_RE = re.compile(
    r"</?(table|thead|tbody|tr|td|th|colgroup|col)\b|class\s*=\s*\"(odd|even|header)\"",
    re.IGNORECASE,
)

def is_tableish_or_html_noise(line: str) -> bool:
    if not line:
        return False
    if HTML_TABLEISH_RE.search(line):
        return True
    if line.strip().startswith("|"):
        return True
    low = line.lower()
    if "td>" in low or "tr>" in low or "th>" in low or "colgroup" in low:
        return True
    return False

def pre_sanitize_raw(raw: str) -> str:
    """
    STEP 1 of preprocessing: Basic sanitation of raw MD text.
    - Normalize invisible characters
    - Remove code blocks
    - Strip HTML tags
    - Simplify markdown links
    
    NOTE: Does NOT merge lines. Line structure is preserved for downstream
    line-based cleanups (caption/table/frontmatter removal).
    """
    raw = normalize_invisibles(raw)

    # remove code blocks and inline code (rare but noisy in converted MD)
    text = FENCED_CODE_RE.sub("\n", raw)
    text = INLINE_CODE_RE.sub("", text)

    # strip HTML tags aggressively (battery recipes usually survive fine without <sub>/<sup>)
    text = strip_html_tags(text)

    # simplify markdown links early: [text](url) -> text
    text = LINK_MD_RE.sub(r"\1", text)

    return text

# Backward compatibility alias
def preprocess_text(raw: str) -> str:
    """Alias for pre_sanitize_raw for backward compatibility."""
    return pre_sanitize_raw(raw)



# =============================================================================
# 3) Front-matter / metadata removal (authors, affiliations, etc.)
# =============================================================================

# Stop markers: once we reach "real content", we stop aggressive front-matter stripping.
# NOTE: Do NOT use inline `(?x)` (VERBOSE) when the pattern contains a literal '#'.
# In Python, `re.VERBOSE` treats `#` as the start of a comment unless escaped.
# That breaks patterns like `(?:#+\s*)?` and `(?:#{1,6}\s*)?`.
# We therefore use only case-insensitive mode here.
FRONTMATTER_STOP_RE = re.compile(
    r"(?i)^\s*(?:#{1,6}\s*)?(?:abstract|introduction|highlights|graphical\s+abstract|keywords)\b|^\s*1\s*[\.)]\s*introduction\b"
)

# Likely affiliation / correspondence tokens
AFFIL_TOKEN_RE = re.compile(
    r"(?ix)\b(department|faculty|school|institute|university|laborator(y|ies)|centre|center|academy|college|hospital|research\s+center|key\s+laboratory)\b"
)
CORRESP_TOKEN_RE = re.compile(
    r"(?ix)\b(corresponding\s+author|e-?mail|email|tel\.?|fax\.?|orcid)\b"
)
ADDRESS_TOKEN_RE = re.compile(
    r"(?ix)\b(avenue|street|road|district|province|state|city|postal|postcode|zip|p\.?o\.?\s*box|"
    r"china|thailand|korea|japan|vietnam|singapore|malaysia|india|australia|canada|"
    r"germany|france|italy|spain|sweden|norway|finland|denmark|switzerland|netherlands|belgium|austria|"
    r"united\s+states|usa|united\s+kingdom|uk)\b"
)

# Heuristic: author list lines are often long, comma-separated name blocks with optional numeric markers.
AUTHOR_NAME_TOKEN_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:[-'][A-Za-z]+)?\b")
AUTHOR_BLOCK_LIKELY_RE = re.compile(
    r"(?x)"
    r"(?:[A-Z][a-z]{2,}(?:[-'][A-Za-z]+)?\s+){1,3}[A-Z][a-z]{2,}(?:[-'][A-Za-z]+)?\d{0,2}"
)

KEYWORDS_LINE_RE = re.compile(r"(?ix)^\s*(keywords?|index\s+terms?)\s*[:：]")

PAGE_FOOTER_RE = re.compile(r"(?ix)^\s*(page\s*\d+\s*(?:of\s*\d+)?|\d+\s*/\s*\d+)\s*$")
PUBLISHER_NOISE_RE = re.compile(
    r"(?ix)\b(doi\s*:\s*10\.|elsevier|wiley|springer|royal\s+society|acs\s+publications|copyright|all\s+rights\s+reserved|preprint)\b"
)

def _comma_count(s: str) -> int:
    return s.count(",")

def is_likely_author_line(line: str) -> bool:
    s = normalize_invisibles(line).strip()
    if not s:
        return False
    # very long single lines with many commas and many capitalized tokens
    commas = _comma_count(s)
    name_tokens = len(AUTHOR_NAME_TOKEN_RE.findall(s))
    if commas >= 4 and name_tokens >= 8 and len(s) <= 600:
        return True
    # compact author list (few commas) but clearly "Name Name1 , Name Name2 , ..."
    if commas >= 2 and AUTHOR_BLOCK_LIKELY_RE.search(s) and any(ch.isdigit() for ch in s):
        return True
    return False

def is_likely_affiliation_line(line: str) -> bool:
    s = normalize_invisibles(line).strip()
    if not s:
        return False
    if KEYWORDS_LINE_RE.match(s):
        return True
    # Strong affiliation/correspondence/address cues
    if AFFIL_TOKEN_RE.search(s) or CORRESP_TOKEN_RE.search(s) or ADDRESS_TOKEN_RE.search(s):
        # avoid killing true content: require either punctuation density or typical address patterns
        if any(ch.isdigit() for ch in s) or ";" in s or "," in s or "@" in s:
            return True
        if len(s) <= 120:
            return True
    return False

def remove_front_matter_blocks(lines: List[str], max_scan_lines: int = 260) -> List[str]:
    """
    Removes author list / affiliations / correspondence blocks near the start of a document.

    Works best on:
    - main paper MD
    - supporting info MD (often starts with authors + affiliations again)
    """
    out: List[str] = []
    i, n = 0, len(lines)
    scan_limit = min(max_scan_lines, n)

    while i < n:
        raw = normalize_invisibles(lines[i])
        s = raw.strip()

        if i >= scan_limit:
            out.extend(lines[i:])
            break

        # Once we see real content headings, stop aggressive stripping.
        if FRONTMATTER_STOP_RE.match(s):
            out.append(lines[i])
            i += 1
            continue

        if is_likely_author_line(s) or is_likely_affiliation_line(s):
            # drop a contiguous block until a blank gap or a clear content stop marker
            blank_run = 0
            i += 1
            while i < n and i < scan_limit:
                cur = normalize_invisibles(lines[i]).strip()
                if FRONTMATTER_STOP_RE.match(cur):
                    break
                if cur == "":
                    blank_run += 1
                else:
                    blank_run = 0

                # continue dropping while it still looks like metadata/affiliation-ish
                if blank_run >= 1:
                    i += 1
                    break
                if is_likely_author_line(cur) or is_likely_affiliation_line(cur):
                    i += 1
                    continue
                # lines with publisher noise (doi/copyright) are also dropped in this front-matter region
                if PUBLISHER_NOISE_RE.search(cur):
                    i += 1
                    continue
                # if it doesn't look like metadata anymore, stop dropping
                break
            continue

        # also drop very obvious single-line metadata in the front-matter region
        if PAGE_FOOTER_RE.match(s):
            i += 1
            continue
        if PUBLISHER_NOISE_RE.search(s) and len(s) <= 200:
            i += 1
            continue

        out.append(lines[i])
        i += 1

    return out

def remove_noise_lines(lines: List[str]) -> List[str]:
    """
    Removes obvious page headers/footers and ultra-noisy standalone lines across the whole document.
    Keep this conservative to avoid deleting real methods content.
    """
    out: List[str] = []
    for ln in lines:
        s = normalize_invisibles(ln).strip()
        if not s:
            out.append(ln)
            continue
        if PAGE_FOOTER_RE.match(s):
            continue
        if PUBLISHER_NOISE_RE.search(s) and len(s) <= 160:
            continue
        if s in {",", " ,", ".,", ";", ":", "-----", "----", "---"}:
            continue
        out.append(ln)
    return out

# =============================================================================
# 4) Caption removal (figure/table captions etc.)
# =============================================================================


CAPTION_PREFIXES = ("figure", "fig", "fig.", "scheme", "table", "caption")

# Caption lines usually start with: "Fig. S4.", "Figure 2:", "Table S1", "Scheme 3" ...
# We keep it slightly conservative: do NOT treat "Fig. 1 shows ..." as caption when it looks like a
# narrative reference line (common after PDF hard-wrapping).
CAPTION_HEAD_RE = re.compile(
    # NOTE: do NOT use inline `(?x)` (VERBOSE) here because the pattern contains a
    # literal '#'. In VERBOSE mode, '#' starts a comment unless escaped.
    r"(?i)^\s*(?:#{1,6}\s*)?(?:\*{1,3}|_{1,3})?\s*"
    r"(?:(?:fig(?:ure)?|table|scheme)\.?|caption)\s*"
    r"(?:s?\s*\d{1,4}(?:[a-z])?(?:\s*[-–]\s*\d{1,4})?(?:\.\d{1,3})?)\b"
)


# Supplementary caption head (e.g., "Fig. S4 ...", "Table S1 ..."):
# In SI, captions often start with "Fig. S#" and include verbs like "shows".
# We treat them as captions (to be removed) rather than narrative figure references.
SUPP_CAPTION_HEAD_RE = re.compile(
    r"(?i)^\s*(?:#{1,6}\s*)?(?:\*{1,3}|_{1,3})?\s*(?:\(?\s*)?"
    r"(?:fig(?:ure)?|table|scheme)\.?\s*s\s*\d{1,4}(?:\s*[\(\[]?[a-z][\)\]]?)?\b"
)
CAPTION_REF_VERB_RE = re.compile(
    r"(?ix)\b(shows?|showing|illustrat(?:e|es|ed|ing)|depict(?:s|ed|ing)|present(?:s|ed|ing)|"
    r"compare(?:s|d|ing)|indicat(?:e|es|ed|ing)|summariz(?:e|es|ed|ing))\b"
)

PANEL_MARKER_RE = re.compile(r"(?i)(?:\([a-z]\)|\b(?:a|b|c|d)\)|\bpanel\s*[a-z]\b)")

CAPTION_START_RE = re.compile(
    # Accept "Fig. S4...", "Fig S4...", "Figure 2...", "Table S1..." etc.
    # IMPORTANT: avoid `(?x)` for the same reason as CAPTION_HEAD_RE.
    # Also, do NOT use a trailing \b here because prefixes like "fig." end with a
    # non-word character; instead use a lookahead.
    r"(?i)^\s*(?:#{1,6}\s*)?(?:\*{1,3}|_{1,3})?\s*(?:"
    + "|".join(re.escape(x) for x in CAPTION_PREFIXES)
    + r")(?=\s|\d|\(|[:.\-–—]|$)"
)

MAX_CAPTION_LINES = 220

def _looks_like_narrative_figure_reference(line: str) -> bool:
    """Heuristic: keep 'Fig. 1 shows ...' lines (often hard-wrapped) as narrative, not caption."""
    s = normalize_invisibles(line).strip()
    if not s:
        return False
    if not CAPTION_HEAD_RE.search(s):
        return False
    # Special-case: Supplementary captions (e.g., "Fig. S4 shows ...") often include verbs;
    # treat them as captions to be removed, not narrative figure references to keep.
    if SUPP_CAPTION_HEAD_RE.search(s):
        return False

    # If it contains panel markers, it's almost surely a caption.
    if PANEL_MARKER_RE.search(s):
        return False
    # If it clearly uses a 'shows/depicts/...' verb, treat as narrative reference.
    if CAPTION_REF_VERB_RE.search(s):
        return True
    return False

def remove_caption_blocks(lines: List[str]) -> List[str]:
    out: List[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = normalize_invisibles(lines[i])

        if CAPTION_START_RE.match(line.strip()) and CAPTION_HEAD_RE.search(line.strip()):
            # Avoid deleting narrative reference lines (rare but happens in hard-wrapped PDFs)
            if _looks_like_narrative_figure_reference(line):
                out.append(lines[i])
                i += 1
                continue

            blank_run, skipped = 0, 0
            i += 1
            while i < n and skipped < MAX_CAPTION_LINES:
                cur = normalize_invisibles(lines[i])
                if cur.strip() == "":
                    blank_run += 1
                else:
                    blank_run = 0
                if blank_run >= 2:
                    i += 1
                    break
                # stop if a new section heading begins
                if HeadingExtractor.extract_heading(cur) is not None:
                    break
                i += 1
                skipped += 1
            continue

        out.append(lines[i])
        i += 1

    return out


# =============================================================================
# 4) Block cleanup and paragraph repair
# =============================================================================

def cleanup_block_text(block_text: str) -> str:
    """
    Aggressive cleanup for markdown blocks:
    - remove html tags
    - remove markdown images
    - remove table-ish lines
    - normalize blank lines
    """
    t = strip_html_tags(normalize_invisibles(block_text))
    t = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", t)

    cleaned_lines: List[str] = []
    for ln in t.splitlines():
        ln = normalize_invisibles(ln).strip()
        if not ln:
            cleaned_lines.append("")
            continue
        if ln.startswith("|"):
            continue
        if is_tableish_or_html_noise(ln):
            continue
        if ln in {",", " ,", ".,", ";", ":", "-----", "----"}:
            continue
        cleaned_lines.append(ln)

    t = "\n".join(cleaned_lines)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t

# =============================================================================
# Helper Regex & Functions (New)
# =============================================================================

REMOVE_BRACKET_CITATIONS_RE = re.compile(r"\[\s*(?:S?\d+|SI|See|Ref)(?:\s*,\s*(?:S?\d+|SI|See|Ref))*\s*\]", re.IGNORECASE)
BIB_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

def remove_bracket_citations(text: str) -> str:
    """
    Removes [1], [2,3], [S1], [See SI] type patterns.
    """
    return REMOVE_BRACKET_CITATIONS_RE.sub(" ", text)

def drop_reference_section(text: str) -> str:
    """
    Truncates text if a clear Reference section header is found to prevent
    fallback miners from reading bibliography.
    """
    # Look for a line that is solely "References" or "Reference" (case insensitive, strict)
    # or "6. References" etc.
    lines = text.splitlines()
    out = []
    # Identify likely reference header
    ref_header_re = re.compile(r"^(#+)?\s*(\d+\.?\s*)?references?\s*$", re.IGNORECASE)
    
    for ln in lines:
        if ref_header_re.match(ln.strip()):
            # stop reading here
            break
        out.append(ln)
    return "\n".join(out)

def is_bibliography_like_sentence(s: str) -> bool:
    """
    Returns True if sentence looks like a bibliography entry.
    """
    # 1. Starts with bracketed number [1] or just 1.
    # 2. Contains a year (19xx or 20xx)
    # 3. Contains typical journal keywords or volume indicators like "Vol.", "Pp."
    
    s_strip = s.strip()
    if not s_strip:
        return False

    # Check for leading number/citation
    # e.g. "[1] ...", "1. ..."
    if re.match(r"^\[\d+\]", s_strip) or re.match(r"^\d+\.\s", s_strip):
        if BIB_YEAR_RE.search(s_strip):
            return True
        if "Journal" in s or "Chem." in s or "Phys." in s or "Mater." in s:
            return True
    
    # Check for DOI-like pattern in strictly plain text if not caught above
    if "doi.org" in s.lower():
        return True

    return False

def needs_noise_gate(block_text: str) -> bool:
    """
    Heuristic to decide if we should run the LLM noise gate.
    If the block looks like a table, caption, or huge list of numbers, return True.
    """
    # 1. Table rows: many pipes or tabs or aligned numbers
    if block_text.count("|") > 4:
        return True
    
    # 2. Caption-like: starts with Fig. or Table
    if re.match(r"^(Fig(ure)?\.?|Table)\s*\d", block_text, re.IGNORECASE):
        return True
        
    # 3. Dense numbers (e.g. data dump)
    # Ratio of digits to chars
    digit_count = sum(c.isdigit() for c in block_text)
    if len(block_text) > 50 and (digit_count / len(block_text) > 0.3):
        return True

    return False

# =============================================================================
# Table Dump Detection (Strict)
# =============================================================================

NUM_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
UNIT_PARENS_RE = re.compile(r"\([^)]+\)")
PM_RE = re.compile(r"±")

# HTML table structure detection (for early table removal)
HTML_TABLEISH_RE = re.compile(r'<\s*(table|tr|td|th)\b', re.IGNORECASE)

# "레시피 문장"이면 숫자 많아도 살려야 하므로, 동사/공정 신호도 같이 봄
# NOTE: Exclude "as-prepared" via negative lookbehind to prevent table sample names from bypassing filter
PROC_VERB_HINT_RE = re.compile(
    r"(?ix)(?<!as[-\s])\b(prepared?|dissolv(ed|ing)?|mix(ed|ing)?|stir(red|ring)?|added?|"
    r"washed?|rinsed?|dried?|anneal(ed|ing)?|soak(ed|ing)?|immers(ed|ing)?|"
    r"coated?|dip(ped|ping)?|filtered?|centrifug(ed|ing)?|heated?|cured?|"
    r"electrodeposit(ed|ion|ing)?|plated?|spray(ed|ing)?|drop[-\s]?cast)\b"
)

def looks_like_table_line(line: str) -> bool:
    s = normalize_invisibles(line).strip()
    if not s:
        return False

    # 1. HTML table structure -> immediate True
    if HTML_TABLEISH_RE.search(s):
        return True

    # 2. 너무 긴 단일 라인은 표 덤프일 확률 높음
    if len(s) >= 250 and not PROC_VERB_HINT_RE.search(s):
        # 숫자 토큰이 매우 많으면 거의 확정
        if len(NUM_TOKEN_RE.findall(s)) >= 12:
            return True

    n_nums = len(NUM_TOKEN_RE.findall(s))
    n_units = len(UNIT_PARENS_RE.findall(s))
    has_pm = bool(PM_RE.search(s))

    # 3. 레시피 동사가 없고 숫자/유닛이 빽빽하면 표로 간주
    if not PROC_VERB_HINT_RE.search(s):
        if (n_nums >= 8 and (n_units >= 2 or has_pm)):
            return True

    # 4. 숫자 비율 기반(보수적으로)
    digit_ratio = (sum(c.isdigit() for c in s) / max(1, len(s)))
    if digit_ratio > 0.35 and n_nums >= 10 and not PROC_VERB_HINT_RE.search(s):
        return True

    return False


def remove_table_like_blocks(lines: List[str]) -> List[str]:
    """
    연속된 table-like 라인을 블록으로 보고 통째로 제거.
    종료 조건: 빈 줄 1~2개 또는 heading 등장.
    """
    out: List[str] = []
    in_table = False
    blank_run = 0

    for ln in lines:
        cur = normalize_invisibles(ln)

        # heading을 만나면 table block 종료
        if HeadingExtractor.extract_heading(cur) is not None:
            in_table = False
            blank_run = 0
            out.append(ln)
            continue

        if looks_like_table_line(cur):
            in_table = True
            blank_run = 0
            continue

        if in_table:
            if cur.strip() == "":
                blank_run += 1
                # 빈 줄 1개는 구조 유지용으로 남겨도 됨(선택)
                if blank_run >= 1:
                    in_table = False
                    out.append("")  # 문단 경계 유지
                continue
            else:
                # table block 계속 제거
                continue

        out.append(ln)

    return out


def is_table_dump_sentence(sent: str) -> bool:
    # split_sentences 이후 “문장”이 되어버린 표 덤프 2차 차단
    s = sent.strip()
    if len(s) < 80:
        return False
    if PROC_VERB_HINT_RE.search(s):
        return False
    n_nums = len(NUM_TOKEN_RE.findall(s))
    if n_nums >= 15:
        return True
    if (sum(c.isdigit() for c in s) / max(1, len(s))) > 0.35 and n_nums >= 12:
        return True
    if "elemental percentage" in s.lower() and n_nums >= 10:
        return True
    return False


# =============================================================================
# Literature Comparison Table Detection (v21 feature backported)
# =============================================================================

LIT_TABLE_CUE_RE = re.compile(r"(?ix)\b(discharge\s+capacity|cycle\s+performance|capacity\s+retention|ref\.|this\s+work|coulombic\s+efficiency)\b")
CITATION_ONLY_LINE_RE = re.compile(r"^\s*\[\s*\d+\s*\]\s*$")
PERF_UNIT_RE = re.compile(r"(?ix)\b(mAh|A\s*g−1|A\s*g-1|mA\s*cm−2|mA\s*cm-2|cycles?|mAh\s*g−1|mAh\s*g-1)\b")

def looks_like_literature_comparison_table(block_text: str) -> bool:
    """
    Detect literature comparison tables (Ref., This work, performance metrics).
    These should be dropped from evidence blocks.
    """
    lines = [ln.strip() for ln in block_text.splitlines() if ln.strip()]
    if len(lines) < 12:
        return False

    cue = sum(1 for ln in lines if LIT_TABLE_CUE_RE.search(ln))
    cite_only = sum(1 for ln in lines if CITATION_ONLY_LINE_RE.match(ln))
    perf = sum(1 for ln in lines if PERF_UNIT_RE.search(ln))
    short = sum(1 for ln in lines if len(ln) <= 45)

    # Vertical table pattern: many short lines, performance units/Ref repeated
    if cue >= 2 and (perf >= 6 or cite_only >= 2) and (short / len(lines) >= 0.55):
        return True
    return False



# =============================================================================
# Caption Cleaning (Strict)
# =============================================================================

CAPTION_START_RE = re.compile(
    r'^\s*(?:\*\*|\*)?\s*(fig(?:ure)?|table|scheme|chart)\.?\s*(?:S)?\s*\d+[A-Za-z0-9\-]*\s*',
    re.IGNORECASE
)
CAPTION_PANEL_RE = re.compile(r'^\s*\([a-zA-Z0-9]+\)\s+', re.IGNORECASE)
CAPTION_TAIL_RE  = re.compile(r'^\s*(inset|scale bar|error bar|comparison of)\b', re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')

def strip_html(s: str) -> str:
    return HTML_TAG_RE.sub('', s)

def strip_emph(s: str) -> str:
    return re.sub(r'(\*\*|\*)', '', s)

def remove_captions_early(lines: List[str]) -> List[str]:
    """
    Strictly remove caption blocks including follow-up panel/tail lines.
    Using state machine:
       - if CAPTION_START_RE match -> in_caption=True
       - while in_caption:
           if next line looks like panel/tail -> drop
           else -> in_caption=False
    """
    out = []
    in_caption = False

    for line in lines:
        # Check plain version for triggers
        plain = strip_emph(strip_html(line)).strip()

        # 1. Caption start?
        if CAPTION_START_RE.match(plain):
            in_caption = True
            continue 

        # 2. Inside caption block?
        if in_caption:
            if plain == "":
                # clear blank line ends caption
                in_caption = False
                out.append(line) # preserve structure blank
                continue
            
            # Check for continuations
            if CAPTION_PANEL_RE.match(plain) or CAPTION_TAIL_RE.match(plain):
                continue
            
            # Not a continuation -> caption ended
            in_caption = False
        
        out.append(line)

    return out

BULLET_LINE_RE = re.compile(r"^\s*([-*+]\s+|\d{1,3}[.)]\s+)\S")
HEADING_LINE_QUICK_RE = re.compile(r"^\s*(#{1,6}\s+|\*{2,3}[^*]+\*{2,3}|S\s*\d+[.:\s]|\d+\.\s+[A-Z])")

def _is_heading_or_structure_line(line: str) -> bool:
    """
    Quick check if line is a heading/bullet/structure that should NOT be merged.
    """
    s = line.strip()
    if not s:
        return True  # blank lines are structure
    if BULLET_LINE_RE.match(line):
        return True
    if HEADING_LINE_QUICK_RE.match(s):
        return True
    # Check with full heading extractor if available (expensive but accurate)
    try:
        h = HeadingExtractor.extract_heading(line)
        if h is not None:
            return True
    except:
        pass
    return False

def preprocess_repair(text: str) -> str:
    """
    STEP 3 of preprocessing: Repair hard-wrapped paragraphs.
    
    IMPORTANT: Run this AFTER line-based cleanups (caption/table/frontmatter removal).
    This function:
    - Merges hard-wrapped lines into proper paragraphs
    - PRESERVES heading/bullet/blank lines as boundaries (never merged)
    - Fixes hyphenation (electro- lyte -> electrolyte)
    
    NOTE: This is run ONCE per document, not per block.
    """
    # Remove bracket citations early to avoid them gluing words
    text = remove_bracket_citations(text)
    text = normalize_invisibles(text)

    lines = text.splitlines()
    out_lines: List[str] = []
    buf: List[str] = []

    def flush_buf() -> None:
        nonlocal buf
        if not buf:
            return
        # Join with hyphenation fix
        result_parts = []
        for i, part in enumerate(buf):
            part = part.strip()
            if not part:
                continue
            # Fix hyphenation: if line ends with '-' and next starts with lowercase, merge
            if result_parts and result_parts[-1].endswith('-'):
                prev = result_parts[-1]
                if part and part[0].islower():
                    # Hyphenated word break: remove hyphen and merge directly
                    result_parts[-1] = prev[:-1] + part
                    continue
            result_parts.append(part)
        
        joined = " ".join(result_parts)
        joined = re.sub(r"\s{2,}", " ", joined).strip()
        if joined:
            out_lines.append(joined)
        buf = []

    for ln in lines:
        raw = ln.rstrip()
        
        # Structure lines are boundaries - never merge
        if _is_heading_or_structure_line(raw):
            flush_buf()
            if raw.strip():  # Non-blank structure
                out_lines.append(raw.strip())
            else:  # Blank line
                out_lines.append("")
            continue
        
        # Regular text line - add to buffer for merging
        buf.append(raw.strip())

    flush_buf()
    repaired = "\n".join(out_lines)
    repaired = re.sub(r"\n{3,}", "\n\n", repaired).strip()
    return repaired


def repair_paragraphs(text: str) -> str:
    """
    Alias for preprocess_repair for backward compatibility.
    Repairs single-newline hard wraps common in PDF->MD outputs.
    """
    return preprocess_repair(text)



# =============================================================================
# 5) Heading extraction with hierarchical levels (v18 core)
# =============================================================================

@dataclass(frozen=True)
class Heading:
    idx: int                # line index in lines[]
    level: int              # logical level: lower means higher priority (like markdown #)
    norm: str               # normalized heading text
    raw: str                # raw extracted heading text (best effort)
    kind: str               # ATX / NUM / S / EMPH / INLINE / PLAIN / APPENDIX

class HeadingExtractor:
    """
    Robust detection of heading-like lines in messy Markdown converted from PDF.

    v18 additions:
    - Plain heading detection (standalone lines): "Experimental Section", "METHODS"
    - Numbered headings without dot: "2 Experimental section"
    - Appendix headings: "Appendix B"
    - Hierarchical level inference for numbered headings:
        2. -> level=2, 2.1 -> level=3, 2.1.1 -> level=4
        S1 -> level=2, S1.1 -> level=3, ...
      (base=2 aligns with typical ## for main sections)

    The extractor returns Heading(idx, level, norm, raw, kind) or None.
    """

    # markdown-style headings
    ATX_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")

    # emphasis-only headings: *Heading* / **Heading** / ___Heading___
    EMPH_LINE_RE = re.compile(r"^\s*(\*{1,3}|_{1,3})\s*(.+?)\s*\1\s*$")

    # inline-bold "header-ish": **Experimental:** details...
    INLINE_HEADER_RE = re.compile(r"^\s*((?:\*{2,3}.+?\*{2,3}\s*)+)[:.-]?\s+")

    BOLD_SEG_RE = re.compile(r"(\*{2,3}|_{2,3})(.+?)\1")
    LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d{1,3}[.)]\s+)(.+?)\s*$")

    # S sections like: S1 Experimental Section, S 1.2 ...
    S_SECTION_RE = re.compile(
        r"^\s*(?:Text\s*)?S\s*[-:.\s]*\s*(\d{1,3}(?:\s*[\.\-]\s*\d{1,3})*)\s*[\.\)]?\s+(.+?)\s*$",
        re.IGNORECASE,
    )

    # Numbered: 2. Experimental, 2.1 Materials, 4) Methods, 3.1.2 ...
    NUM_SECTION_RE = re.compile(
        r"^\s*(\d{1,3}(?:\.\d{1,3})*)\s*[\.\)]\s+(.+?)\s*$",
        re.IGNORECASE,
    )

    # Numbered WITHOUT dot: "2 Experimental section"
    NUM_SECTION_NODOT_RE = re.compile(
        r"^\s*(\d{1,3}(?:\.\d{1,3})*)\s+(.+?)\s*$",
        re.IGNORECASE,
    )

    # Appendix: "Appendix B", "Appendix A. Experimental", "Appendix: Methods"
    APPENDIX_RE = re.compile(
        r"^\s*(appendix|appendices)\s*([A-Z]|\d+)?\s*[:.\-]?\s*(.*)$",
        re.IGNORECASE,
    )

    # Plain headings: standalone lines, no punctuation end, short enough
    PLAIN_MAX_CHARS = 120
    PLAIN_MAX_WORDS = 15
    PLAIN_BAD_END_RE = re.compile(r"[.;:]\s*$")

    # Keywords that often appear as standalone headings
    PLAIN_HEADING_KEYWORDS_RE = re.compile(
        r"(?ix)\b("
        r"experimental(\s+section|\s+details|\s+procedures?)?|"
        r"experiment(\s+section)?|"
        r"materials?\s+and\s+methods?|"
        r"material\s+and\s+methods?|"
        r"methods?|methodology|"
        r"experimental\s+part|experimental\s+details|experimental\s+procedures?|"
        r"electrode\s+preparation|anode\s+preparation|"
        r"electrolyte(\s+preparation)?|"
        r"cell\s+assembly|battery\s+assembly|device\s+assembly|"
        r"materials\s+preparation|"
        r"supporting\s+information|supplementary(\s+information)?"
        r")\b"
    )

    MD_MARKUP_RE = re.compile(r"[*_]{1,3}")
    REMOVE_OTHER_TAGS_RE = re.compile(r"(?is)</?(?!sub\b|/sub\b|sup\b|/sup\b)[a-z0-9]+\b[^>]*>")

    @staticmethod
    def normalize_heading_text(s: str) -> str:
        s = normalize_invisibles(s)
        s = HeadingExtractor.REMOVE_OTHER_TAGS_RE.sub("", s)
        s = LINK_MD_RE.sub(r"\1", s)
        s = s.replace("\u00a0", " ")
        s = re.sub(r"\s+", " ", s).strip()
        s = HeadingExtractor.MD_MARKUP_RE.sub("", s).strip()
        s = re.sub(r"[\s:.\-–—]+$", "", s).strip()
        return s

    @staticmethod
    def _mixed_emphasis_heading_heuristic(line: str) -> bool:
        s = normalize_invisibles(line).strip()
        if not s or s.endswith((".", ";")):
            return False
        if s.startswith("|"):
            return False
        segs = list(HeadingExtractor.BOLD_SEG_RE.finditer(s))
        if not segs:
            return False
        total_len = len(re.sub(r"\s+", "", s))
        bold_len = sum(len(re.sub(r"\s+", "", m.group(2))) for m in segs)
        coverage = (bold_len / total_len) if total_len else 0.0
        starts_bold = bool(re.match(r"^\s*(\*{2,3}|_{2,3})", s))
        if not (starts_bold or coverage >= 0.30):
            return False
        norm = HeadingExtractor.normalize_heading_text(s)
        if not norm or not (1 <= len(norm.split()) <= 30) or len(norm) > 180:
            return False
        return True

    @staticmethod
    def _numeric_depth(num: str) -> int:
        """
        "2" -> 1, "2.1" -> 2, "2.1.3" -> 3
        """
        num = num.strip()
        if not num:
            return 1
        return num.count(".") + 1

    @staticmethod
    def _s_depth(snum: str) -> int:
        """
        "1" -> 1, "1.2" -> 2, "1-2-3" -> 3 (best effort)
        """
        snum = snum.strip()
        if not snum:
            return 1
        # normalize separators
        snum2 = re.sub(r"\s+", "", snum)
        # treat both '.' and '-' as depth separators
        depth = 1 + len(re.findall(r"[.\-]", snum2))
        return max(1, depth)

    @staticmethod
    def _level_for_depth(depth: int, base: int = 2) -> int:
        """
        Map section depth to markdown-like levels.
        depth=1 -> level=2 (like ##)
        depth=2 -> level=3 (like ###)
        """
        depth = max(1, int(depth))
        return base + (depth - 1)

    @staticmethod
    def _is_plain_heading(line: str) -> bool:
        s = normalize_invisibles(line).strip()
        if not s:
            return False
        if len(s) > HeadingExtractor.PLAIN_MAX_CHARS:
            return False
        if len(s.split()) > HeadingExtractor.PLAIN_MAX_WORDS:
            return False
        if HeadingExtractor.PLAIN_BAD_END_RE.search(s):
            return False
        if s.startswith("|"):
            return False
        if is_tableish_or_html_noise(s):
            return False
        # avoid sentences that start with lowercase letter and are long-ish
        if len(s) > 40 and re.match(r"^[a-z]", s):
            return False
        # must contain known section keyword OR be all-caps METHODS style
        if HeadingExtractor.PLAIN_HEADING_KEYWORDS_RE.search(s):
            return True
        if s.isupper() and len(s) <= 30 and re.search(r"\bMETHODS?\b", s):
            return True
        return False

    @staticmethod
    def extract_heading(line: str, idx: int = -1) -> Optional[Heading]:
        """
        Returns Heading or None.
        """
        if not line or not line.strip():
            return None
        if is_tableish_or_html_noise(line):
            return None

        s = normalize_invisibles(line).rstrip("\n").strip()

        # list prefix stripping (common in MD conversions)
        mlist = HeadingExtractor.LIST_PREFIX_RE.match(s)
        if mlist:
            s = normalize_invisibles(mlist.group(1)).strip()

        # ATX headings
        m = HeadingExtractor.ATX_HEADING_RE.match(s)
        if m:
            level = len(m.group(1))
            raw = m.group(2)
            norm = HeadingExtractor.normalize_heading_text(raw)
            return Heading(idx=idx, level=level, norm=norm, raw=raw, kind="ATX")

        # S sections
        ms = HeadingExtractor.S_SECTION_RE.match(s)
        if ms:
            snum = ms.group(1)
            title = ms.group(2)
            depth = HeadingExtractor._s_depth(snum)
            level = HeadingExtractor._level_for_depth(depth, base=2)
            raw = s
            norm = HeadingExtractor.normalize_heading_text(raw)
            return Heading(idx=idx, level=level, norm=norm, raw=raw, kind="S")

        # Numeric sections with dot/paren
        mn = HeadingExtractor.NUM_SECTION_RE.match(s)
        if mn:
            num = mn.group(1)
            title = mn.group(2)
            depth = HeadingExtractor._numeric_depth(num)
            level = HeadingExtractor._level_for_depth(depth, base=2)
            raw = s
            norm = HeadingExtractor.normalize_heading_text(raw)
            return Heading(idx=idx, level=level, norm=norm, raw=raw, kind="NUM")

        # Emphasis-only headings
        me = HeadingExtractor.EMPH_LINE_RE.match(s)
        if me:
            raw = me.group(2)
            norm = HeadingExtractor.normalize_heading_text(raw)
            return Heading(idx=idx, level=2, norm=norm, raw=raw, kind="EMPH")

        # Inline bold header
        mi = HeadingExtractor.INLINE_HEADER_RE.match(s)
        if mi:
            raw_heading_part = mi.group(1)
            norm = HeadingExtractor.normalize_heading_text(raw_heading_part)
            return Heading(idx=idx, level=3, norm=norm, raw=raw_heading_part, kind="INLINE")

        # Appendix
        ma = HeadingExtractor.APPENDIX_RE.match(s)
        if ma:
            # keep whole line; appendix often contains methods
            raw = s
            norm = HeadingExtractor.normalize_heading_text(raw)
            return Heading(idx=idx, level=2, norm=norm, raw=raw, kind="APPENDIX")

        # Numbered without dot (guard: only accept if it looks like a section header)
        mnd = HeadingExtractor.NUM_SECTION_NODOT_RE.match(s)
        if mnd:
            num = mnd.group(1)
            rest = mnd.group(2)
            # accept only if rest looks like a section name (not a normal sentence)
            rest_norm = HeadingExtractor.normalize_heading_text(rest)
            if HeadingExtractor.PLAIN_HEADING_KEYWORDS_RE.search(rest_norm) or re.search(r"\b(methods?|experimental)\b", rest_norm, re.I):
                depth = HeadingExtractor._numeric_depth(num)
                level = HeadingExtractor._level_for_depth(depth, base=2)
                raw = s
                norm = HeadingExtractor.normalize_heading_text(raw)
                return Heading(idx=idx, level=level, norm=norm, raw=raw, kind="NUM_NODOT")

        # Mixed emphasis heuristic
        if HeadingExtractor._mixed_emphasis_heading_heuristic(s):
            raw = s
            norm = HeadingExtractor.normalize_heading_text(raw)
            return Heading(idx=idx, level=2, norm=norm, raw=raw, kind="MIXED_EMPH")

        # Plain headings (v18)
        if HeadingExtractor._is_plain_heading(s):
            raw = s
            norm = HeadingExtractor.normalize_heading_text(raw)
            # treat very short as higher-level
            lvl = 2 if len(norm.split()) <= 6 else 3
            return Heading(idx=idx, level=lvl, norm=norm, raw=raw, kind="PLAIN")

        return None


# =============================================================================
# 6) Candidate heading patterns (Methods + recipe-first)
# =============================================================================

# Candidate headings to include as method/recipe-related sections
METHOD_HEADING_PATTERNS = [
    r"\bexperimental\b",
    r"\bexperiment\s+section\b",
    r"\bexperimental\s+section\b",
    r"\bexperimental\s+details\b",
    r"\bexperimental\s+procedures?\b",
    r"\bmaterials?\s+and\s+methods\b",
    r"\bmaterial\s+and\s+methods\b",
    r"\bmethods?\b",
    r"\bmethodology\b",
    r"\bexperimental\s+part\b",
    r"\bpreparation\b",
    r"\bmaterials\s+preparation\b",
    r"\belectrode\s+preparation\b",
    r"\banode\s+preparation\b",
    r"\belectrolyte\b",
    r"\bcell\s+assembly\b",
    r"\bbattery\s+assembly\b",
    r"\bdevice\s+assembly\b",
    r"\bappendix\b",
    # v20 extended: synthesis/coating/assembly patterns for SI
    r"\bsynthesis\b",
    r"\bfabrication\b",
    r"\bcoating\b",
    r"\bassembly\s+of\b",
    r"\belectrodeposition\b",
]
METHOD_HEADING_RE = re.compile("|".join(f"(?:{p})" for p in METHOD_HEADING_PATTERNS), re.IGNORECASE)

# Strong noise headings to exclude
HEADER_EXCLUDE_PATTERNS = [
    r"\bcharacteri[sz]ations?\b",
    r"\bcharacteri[sz]ation\b",
    r"\bresults?\b",
    r"\bdiscussion\b",
    r"\bconclusion\b",
    r"\bmechanism\b",
    r"\btheoretical\b",
    r"\bcalculation\b",
    r"\bcomputational\b",
    r"\bsimulation\b",
    r"\bmodel(l)?ing\b",
    r"\breferences?\b",
    r"\backnowledg(e)?ments\b",
    r"\bfigures?\b",
    r"\bmodel(l)?ing\b",
    r"\breferences?\b",
    r"\backnowledg(e)?ments\b",
    r"\bfigures?\b",
    r"\btables?\b",
    # NOTE: do NOT exclude "supporting information" here; methods can live in SI.
]
HEADER_EXCLUDE_RE = re.compile("|".join(f"(?:{p})" for p in HEADER_EXCLUDE_PATTERNS), re.IGNORECASE)

# Simulation headings to strictly exclude (block-level cut)
SIM_HEADING_RE = re.compile(
    r'\b('
    r'finite\s*element|fem|fea|comsol|multiphysics|ansys|fluent|abaqus|openfoam|cfd|'
    r'simulation|simulations|modeling|modelling|numerical|calculation|calculations|'
    r'density\s*functional\s*theory|dft|first[-\s]*principles|ab\s*initio|'
    r'molecular\s*dynamics|md|monte\s*carlo|phase[-\s]*field|'
    r'computational|theoretical'
    r')\b',
    re.IGNORECASE
)

# Recipe headings for permissive fallback (heading-based miner)
RECIPE_HEADING_PATTERNS = [
    r"\bexperimental\b",
    r"\bmethods?\b",
    r"\bmethodology\b",
    r"\bpreparation\b",
    r"\bfabrication\b",
    r"\bsynthesis\b",
    r"\bcoating\b",
    r"\bmodification\b",
    r"\bsurface\s+treatment\b",
    r"\belectrode\b.*\bprepar",
    r"\banode\b.*\bprepar",
    r"\bmodified\b.*\bzn",
    r"\bpre[-\s]?treatment\b",
    r"\bpost[-\s]?treatment\b",
    r"\belectrolyte\b",
    r"\bcell\s+assembly\b",
    r"\bappendix\b",
    r"\bsupporting\s+information\b",
    r"\bsupplementary(\s+information)?\b",
]
RECIPE_HEADING_RE = re.compile("|".join(f"(?:{p})" for p in RECIPE_HEADING_PATTERNS), re.IGNORECASE)

# Stub "see SI" detection: some Experimental sections are just a pointer
METHODS_POINTER_RE = re.compile(
    r"(?ix)\b("
    r"supporting\s+information|supplementary(\s+information)?|"
    r"see\s+(the\s+)?supporting\s+information|see\s+(the\s+)?supplementary|"
    r"provided\s+in\s+the\s+supporting\s+information|"
    r"described\s+in\s+the\s+supporting\s+information|"
    r"given\s+in\s+the\s+supporting\s+information|"
    r"available\s+in\s+the\s+supporting\s+information|"
    r"refer\s+to\s+(the\s+)?supporting\s+information|"
    r"supporting\s+information\s*[:\-]"
    r")\b"
)

# If block is this short AND contains pointer language, treat as stub and force fallback
METHODS_POINTER_MAX_WORDS = 60
METHODS_POINTER_MAX_CHARS = 450


# =============================================================================
# 7) Sentence splitting (hardened)
# =============================================================================

ABBR_RE = re.compile(r"\b(e\.g|i\.e|vs|Fig|Figs|Eq|Eqs|Ref|Refs|No|Dr|Prof|ca|et al|wt|vol|approx|incl|excl|min|max|avg|temp|conc|soln|aq)\.?$", re.IGNORECASE)
DECIMAL_RE = re.compile(r"\b\d+\.\d+\b")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)

# Scientific notation / ranges protection
SCI_NOTATION_RE = re.compile(r"\d+(\.\d+)?[×x]\s*10\^?[-−]?\d+", re.IGNORECASE)
RANGE_RE = re.compile(r"\d+(\.\d+)?\s*[-–—]\s*\d+(\.\d+)?")

def _protect_special_tokens(text: str) -> Tuple[str, Dict[str, str]]:
    """
    Protect decimals, DOIs, scientific notation, and ranges so naive splitting doesn't break them.
    """
    mapping: Dict[str, str] = {}

    def repl_generic(m: re.Match, prefix: str) -> str:
        tok = m.group(0)
        key = f"__{prefix}_{len(mapping)}__"
        mapping[key] = tok
        return key

    # Order matters: more specific patterns first
    text2 = DOI_RE.sub(lambda m: repl_generic(m, "DOI"), text)
    text2 = SCI_NOTATION_RE.sub(lambda m: repl_generic(m, "SCI"), text2)
    text2 = RANGE_RE.sub(lambda m: repl_generic(m, "RNG"), text2)
    text2 = DECIMAL_RE.sub(lambda m: repl_generic(m, "DEC"), text2)
    return text2, mapping

def _restore_special_tokens(text: str, mapping: Dict[str, str]) -> str:
    for k, v in mapping.items():
        text = text.replace(k, v)
    return text

def _split_line_into_sentences(line: str, mapping: Dict[str, str]) -> List[str]:
    """
    Split a single line (no newlines) into sentences.
    Returns list of sentence fragments.
    """
    parts: List[str] = []
    buf: List[str] = []
    
    for ch in line:
        buf.append(ch)
        if ch in ".!?":
            cur = "".join(buf).strip()
            # Check if this is a real sentence end
            if ABBR_RE.search(cur):
                continue  # Abbreviation, keep collecting
            if len(cur) >= 5:
                parts.append(cur)
                buf = []
    
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    
    return parts

def split_sentences(text: str, already_repaired: bool = False) -> List[str]:
    """
    Improved sentence splitter that:
    1. Respects newlines as potential sentence boundaries (bullets, headings)
    2. Protects decimals, DOIs, scientific notation, ranges
    3. Handles abbreviations better
    4. Splits long sentences on ';'
    
    Args:
        text: Text to split
        already_repaired: If True, skip repair_paragraphs call
    """
    if not already_repaired:
        text = repair_paragraphs(text)
    text = normalize_invisibles(text)
    text = remove_bracket_citations(text)  # Clean citations once
    
    # Collapse multiple spaces but preserve newlines
    text = re.sub(r"[ \t]+", " ", text)
    
    protected, mapping = _protect_special_tokens(text)
    
    # STEP 1: Split by newlines first (respects structure)
    lines = protected.split("\n")
    
    all_sentences: List[str] = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue  # Skip blank lines
        
        # STEP 2: Split each line into sentences
        line_sents = _split_line_into_sentences(line, mapping)
        
        for s in line_sents:
            s = _restore_special_tokens(s, mapping).strip()
            if not s or len(s) < 3:
                continue
            
            # STEP 3: Split very long sentences on ';'
            if len(s) > 320 and ";" in s:
                subs = [x.strip() for x in s.split(";") if x.strip() and len(x.strip()) >= 3]
                all_sentences.extend(subs)
            else:
                all_sentences.append(s)
    
    return all_sentences


def split_sentences_fast(text: str) -> List[str]:
    """
    Fast version of split_sentences that skips repair_paragraphs.
    Use when text is already repaired at document level.
    """
    return split_sentences(text, already_repaired=True)



# =============================================================================
# 8) Heuristic tagger (stage 1)
# =============================================================================

# Zn surface anchors (avoid electrolyte salts only)
ZN_SURFACE_ANCHOR_RE = re.compile(
    r"(?ix)"
    r"\b(zn\s*(foil|anode|electrode|plate|metal))\b|"
    r"\b(zinc\s*(foil|anode|electrode|plate|metal))\b|"
    r"\b(bare\s+zn|pristine\s+zn)\b|"
    r"(@zn\b)|(\bzn@)",
)

ELECTROLYTE_RE = re.compile(
    r"(?ix)\b(electrolyte|aqueous\s+solution|additive|ph|buffer)\b|"
    r"\b(znso4|zn\(so4\)2|zn\(cf3so3\)2|zn\(tfs?i\)2|zncl2|zn\(no3\)2|"
    r"zinc\s+sulfate|zinc\s+triflate|zinc\s+chloride|cf3so3|tfs?i)\b|"
    r"\b\d+(?:\.\d+)?\s*(?:mM|M|mol\s*l-?1|mol\s*l−?1|wt%|vol%)\b"
)

ZN_SURFACE_VERB_RE = re.compile(
    r"(?ix)\b("
    r"coat(ed|ing)?|coated\s+with|"
    r"electrodeposit(ed|ion|ing)?|electroplate(d|ing)?|plate(d|ing)?|plating|"
    r"dip(ped|ping)?|immerse(d|s|ing)?|soak(ed|ing)?|drench(ed|ing)?|"
    r"etch(ed|ing)?|corrod(ed|ing)?|"
    r"wash(ed|ing)?|rinse(d|s|ing)?|clean(ed|ing)?|"
    r"dry(ing|ed)?|vacuum\s*dried?|freeze[-\s]?dried?|"
    r"anneal(ed|ing)?|heat(ed|ing)?|cure(d|ing)?|calcine(d|ation)?|"
    r"polish(ed|ing)?|scratch(ed|ing)?|roll(ed|ing)?|press(ed|ing)?|"
    r"treat(ed|ment|ing)?|modify(ing|ied)?|functionaliz(ed|ation|ing)?|"
    r"spin[-\s]?coat(ed|ing)?|spray(ed|ing)?|drop[-\s]?cast(ed|ing)?|doctor[-\s]?blade(d|ing)?"
    r")\b"
)

SOLUTION_VERB_RE = re.compile(
    r"(?ix)\b("
    r"dissolv(ed|ing)?|add(ed|ing)?|mix(ed|ing)?|stir(red|ring)?|sonicat(ed|ion|ing)?|"
    r"centrifug(ed|ing)?|filter(ed|ing)?|react(ed|ion|ing)?|synthesi[sz](ed|s|ing)?|"
    r"prepared?|was\s+synthesi[sz]ed|was\s+prepared|obtained|"
    r"polymeri[sz](ed|ation|ing)?"
    r")\b"
)

FORMATION_RE = re.compile(
    r"(?ix)\b("
    r"formation\s+cycle|activation|conditioning|pre[-\s]?cycling|"
    r"to\s+form\s+(a\s+)?(sei|protective\s+layer|interface)|"
    r"in[-\s]?situ\s+(sei|layer|film)\s+formation|"
    r"during\s+(cycling|charge|discharge)|"
    r"plating/stripping\s+to\s+form|"
    r"built\s+(a\s+)?(sei|layer)\s+(during|in)\s+"
    r")\b"
)

CHAR_RE = re.compile(r"(?ix)\b(sem|tem|xrd|xps|ftir|raman|afm|nmr|uv-?vis|eds|edx|icp)\b")
ECHEM_TEST_RE = re.compile(r"(?ix)\b(cv|lsv|eis|gcd|galvanostatic|charge/discharge|rate\s+performance)\b")

RESULT_RE = re.compile(
    r"(?ix)\b("
    r"as\s+shown\s+in|fig\.?|scheme|table\s+\d|"
    r"demonstrat(es|ed)|reveal(s|ed)|achiev(ed|es)|"
    r"significant(ly)?|superior|enhanc(ed|ement)|"
    r"outperform(ed|s)|higher\s+capacity|lower\s+overpotential"
    r")\b"
)

# Simulation/Modeling sentences
SIM_SENT_RE = re.compile(
    r'\b('
    r'comsol|multiphysics|ansys|abaqus|openfoam|cfd|finite\s*element|mesh|grid|cad|'
    r'nernst[-\s]*planck|butler[-\s]*volmer|poisson|navier[-\s]*stokes|'
    r'exchange\s*current\s*density|current\s*distribution|electric\s*field|'
    r'dft|density\s*functional|ab\s*initio|first[-\s]*principles|'
    r'molecular\s*dynamics|monte\s*carlo|simulated|simulation|model was built'
    r')\b',
    re.IGNORECASE
)

COND_RE = re.compile(
    r"(?ix)\b("
    r"\d+(\.\d+)?\s*(h|hr|hrs|hours|min|mins|s|sec|secs|days)\b|"
    r"\d+(\.\d+)?\s*°\s*c\b|"
    r"\d+(\.\d+)?\s*(m|mm|mM|mol\s*l-?1|mol\s*l−?1|wt%|mg\s*mL-?1|mg\s*mL−?1)\b|"
    r"\d+(\.\d+)?\s*(mA|A)\s*cm[-−]2\b|"
    r"\d+(\.\d+)?\s*(rpm)\b"
    r")"
)

ASSEMBLY_RE = re.compile(
    r"(?ix)\b("
    r"assembled|coin\s*cell|cr2032|swagelok|pouch\s*cell|"
    r"separator|glass\s*fiber|celgard|"
    r"cathode|anode|electrode|"
    r"cell\s+was\s+assembled|battery\s+was\s+assembled|"
    r"stacked|laminated"
    r")\b"
)

# Simulation/Modeling sentences
SIM_SENT_RE = re.compile(
    r"(?ix)\b(simulation|modeling|density\s*functional\s*theory|dft|first[-\s]?principles|finite\s*element\s*method|ab\s*initio|molecular\s*dynamics|computational|theoretical)\b"
)

def heuristic_tag_sentence_v21(sentence: str) -> Tuple[str, float, List[str], Dict[str, Any]]:
    """
    v21: Returns (tag, confidence, reasons, features).
    features: dict of extracted signal flags for LLM support.
    """
    s = sentence.strip()
    if not s:
        return "OTHER", 0.0, ["empty"], {}

    reasons: List[str] = []
    features: Dict[str, Any] = {
        "has_zn_metal": bool(ZN_METAL_ANCHOR_RE.search(s)),
        "has_zn_salt": bool(ZN_SALT_ANCHOR_RE.search(s)),
        "has_time_pre": bool(TIME_AXIS_PRE_RE.search(s)),
        "has_time_during": bool(TIME_AXIS_DURING_RE.search(s)),
        "has_aqueous": bool(AQUEOUS_SIGNAL_RE.search(s)),
        "has_lab_scale": bool(LAB_SCALE_SIGNAL_RE.search(s)),
    }

    # 0. Simulation/Modeling check (Strict priority)
    if SIM_SENT_RE.search(s):
        return "SIMULATION", 0.95, ["SIMULATION:keyword"], features

    # Strong characterization noise
    if CHAR_RE.search(s):
        return "CHAR", 0.95, ["CHAR:instrument"], features

    # v21: Formation with battery context check
    formation_match = FORMATION_RE.search(s)
    if formation_match:
        reasons.append("FORMATION:keyword")
        conf = 0.80
        
        # v21: "activation" alone is ambiguous - need battery context
        matched_text = formation_match.group(0).lower()
        if "activation" in matched_text:
            battery_context = any(k in s.lower() for k in [
                "cycle", "cycling", "cell", "electrochemical", "electrode",
                "formation", "conditioning", "sei", "anode", "cathode"
            ])
            if not battery_context:
                reasons.append("activation_ambiguous")
                conf = 0.55  # Lower confidence, LLM will judge
        
        if RESULT_RE.search(s):
            conf -= 0.15
            reasons.append("RESULT:cooccur")
        return "FORMATION_IN_SITU", max(0.0, min(1.0, conf)), reasons, features

    # Electrolyte recipe
    if ELECTROLYTE_RE.search(s):
        reasons.append("ELECTROLYTE:pattern")
        
        # v21: Override to PROC_ZN ONLY if Zn METAL anchor + verb (not salt)
        if features["has_zn_metal"] and ZN_SURFACE_VERB_RE.search(s):
            reasons.append("PROC_ZN:override_electrolyte")
            return "PROC_ZN", 0.85, reasons, features
        
        conf = 0.75
        if "electrolyte" in s.lower() or "additive" in s.lower():
            conf += 0.10
        return "ELECTROLYTE", min(1.0, conf), reasons, features

    # Assembly
    if ASSEMBLY_RE.search(s):
        reasons.append("ASSEMBLY:keyword")
        conf = 0.70
        if "assembled" in s.lower() or "cr2032" in s.lower():
            conf += 0.10
        if RESULT_RE.search(s):
            conf -= 0.10
            reasons.append("RESULT:cooccur")
        return "ASSEMBLY", max(0.0, min(1.0, conf)), reasons, features

    # v21: Zn anode processing - METAL anchor required for full PROC_ZN
    has_verb = bool(ZN_SURFACE_VERB_RE.search(s))
    has_cond = bool(COND_RE.search(s))
    
    if features["has_zn_metal"]:
        # Clear Zn metal context - strong PROC_ZN
        if has_verb or has_cond:
            reasons.append("PROC_ZN:zn_metal_anchor")
            if has_verb:
                reasons.append("PROC_ZN:surface_verb")
            if has_cond:
                reasons.append("PROC_ZN:conditions")
            if features["has_time_pre"]:
                reasons.append("time_axis:pre_cycling")
            conf = 0.85
            if RESULT_RE.search(s):
                conf -= 0.15
                reasons.append("RESULT:cooccur")
            return "PROC_ZN", max(0.0, min(1.0, conf)), reasons, features
    
    # v21: Zn salt without metal context - PROC_ZN_WEAK (let LLM decide)
    if features["has_zn_salt"] and not features["has_zn_metal"]:
        if has_verb or has_cond:
            reasons.append("PROC_ZN_WEAK:salt_only")
            reasons.append("no_metal_context")
            # Default to ELECTROLYTE with flag, unless clear surface verb
            if has_verb and any(v in s.lower() for v in ["coated", "immersed", "dipped", "soaked"]):
                return "PROC_ZN_WEAK", 0.55, reasons, features
            return "ELECTROLYTE", 0.65, reasons, features

    # v21: Legacy ZN_SURFACE_ANCHOR_RE (includes @Zn patterns)
    zn_anchor_match = ZN_SURFACE_ANCHOR_RE.search(s)
    if zn_anchor_match:
        matched_text = zn_anchor_match.group(0).lower()
        is_weak_anchor = '@zn' in matched_text or 'zn@' in matched_text
        
        if is_weak_anchor:
            if has_verb:
                reasons.append("PROC_ZN:weak_anchor+verb")
                conf = 0.70
                if RESULT_RE.search(s):
                    conf -= 0.15
                    reasons.append("RESULT:cooccur")
                return "PROC_ZN", max(0.0, min(1.0, conf)), reasons, features
        else:
            if has_verb or has_cond:
                reasons.append("PROC_ZN:zn_anchor")
                if has_verb:
                    reasons.append("PROC_ZN:surface_verb")
                if has_cond:
                    reasons.append("PROC_ZN:conditions")
                conf = 0.80
                if RESULT_RE.search(s):
                    conf -= 0.15
                    reasons.append("RESULT:cooccur")
                return "PROC_ZN", max(0.0, min(1.0, conf)), reasons, features

    # Coating material prep
    if SOLUTION_VERB_RE.search(s) and (COND_RE.search(s) or "prepared" in s.lower() or "synth" in s.lower()):
        reasons.append("PROC_COAT_MAT:solution_verb")
        if COND_RE.search(s):
            reasons.append("PROC_COAT_MAT:conditions")
        conf = 0.70
        if "was synthesized" in s.lower() or "was prepared" in s.lower():
            conf += 0.10
        if RESULT_RE.search(s):
            conf -= 0.10
        return "PROC_COAT_MAT", max(0.0, min(1.0, conf)), reasons, features

    # Electrochemical tests
    if ECHEM_TEST_RE.search(s):
        reasons.append("ECHEM_TEST:keyword")
        conf = 0.55
        if RESULT_RE.search(s):
            conf -= 0.05
        return "ECHEM_TEST", max(0.0, min(1.0, conf)), reasons, features

    # Result-ish
    if RESULT_RE.search(s):
        return "RESULT", 0.70, ["RESULT:keyword"], features

    return "OTHER", 0.35, ["no_match"], features


# Backward compatibility wrapper
def heuristic_tag_sentence(sentence: str) -> Tuple[str, float, List[str]]:
    tag, conf, reasons, _ = heuristic_tag_sentence_v21(sentence)
    return tag, conf, reasons


def is_ambiguous(tag: str, confidence: float, sentence: str = "") -> bool:
    """
    Decide whether to send to LLM for refinement.
    Stricter version: only send if there are procedural signals worth refining.
    """
    # Never refine SIMULATION, CHAR, or high-confidence tags
    if tag in {"SIMULATION", "CHAR"} or confidence >= 0.85:
        return False
    
    # For OTHER: only refine if it has some procedural signal
    if tag == "OTHER":
        s = sentence.lower()
        # Must have at least one procedural hint to be worth refining
        has_hint = bool(
            ZN_SURFACE_ANCHOR_RE.search(s) or
            ZN_SURFACE_VERB_RE.search(s) or
            SOLUTION_VERB_RE.search(s) or
            COND_RE.search(s) or
            "electrolyte" in s or
            "assembly" in s
        )
        return has_hint
    
    if tag in {"RESULT", "ECHEM_TEST"} and confidence < 0.75:
        return True
    if tag in {"ASSEMBLY", "ELECTROLYTE"} and confidence < 0.70:
        return True
    if tag == "PROC_COAT_MAT" and confidence < 0.70:
        return True
    return False


# =============================================================================
# 9) Fallback miners (sentence + sectionless paragraph miner)
# =============================================================================

def mine_fallback_sentences(full_text: str, keep_context: int = 1) -> List[str]:
    """
    Sentence-based fallback:
      - collect sentences with Zn surface anchor + surface verbs/conditions
      - include minimal context (+/- keep_context sentences) around hits
    """
    sents = split_sentences(full_text)
    hits: List[int] = []
    for i, s in enumerate(sents):
        # Skip table dump sentences in fallback
        if is_table_dump_sentence(s):
            continue
        # Skip bibliography-like sentences
        if is_bibliography_like_sentence(s):
            continue
        if ZN_SURFACE_ANCHOR_RE.search(s) and (ZN_SURFACE_VERB_RE.search(s) or COND_RE.search(s)):
            hits.append(i)

    if not hits:
        return []

    keep_idx = set()
    for i in hits:
        for j in range(max(0, i - keep_context), min(len(sents), i + keep_context + 1)):
            # Also skip table/bib in context
            if not is_table_dump_sentence(sents[j]) and not is_bibliography_like_sentence(sents[j]):
                keep_idx.add(j)

    out = [sents[i] for i in sorted(keep_idx)]
    return out

def paragraphize(text: str) -> List[str]:
    """
    Split text into paragraphs after repair_paragraphs.
    """
    repaired = repair_paragraphs(text)
    # paragraphs separated by blank lines
    paras = [p.strip() for p in repaired.split("\n\n") if p.strip()]
    # further split huge paras
    out: List[str] = []
    for p in paras:
        if len(p) > 2500:
            # chunk by sentences
            sents = split_sentences(p)
            buf = []
            buf_len = 0
            for s in sents:
                if buf_len + len(s) > 2000 and buf:
                    out.append(" ".join(buf))
                    buf = []
                    buf_len = 0
                buf.append(s)
                buf_len += len(s) + 1
            if buf:
                out.append(" ".join(buf))
        else:
            out.append(p)
    return out

def paragraph_recipe_score(p: str) -> int:
    """
    Heuristic score to select promising paragraphs when there are no headings.
    Higher = more likely to contain recipe.
    """
    score = 0
    if ZN_SURFACE_ANCHOR_RE.search(p):
        score += 4
    if ZN_SURFACE_VERB_RE.search(p):
        score += 3
    if COND_RE.search(p):
        score += 2
    if ELECTROLYTE_RE.search(p):
        score += 2
    if SOLUTION_VERB_RE.search(p):
        score += 1
    if RESULT_RE.search(p):
        score -= 1
    if CHAR_RE.search(p):
        score -= 2
    return score

def select_top_paragraphs(full_text: str, top_k: int = 25, min_score: int = 3) -> List[str]:
    paras = paragraphize(cleanup_block_text(full_text))
    scored = [(paragraph_recipe_score(p), i, p) for i, p in enumerate(paras)]
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = [p for sc, _, p in scored if sc >= min_score][:top_k]
    return out


# =============================================================================
# 10) Ollama client + caching + strict JSON parsing
# =============================================================================

class OllamaError(RuntimeError):
    pass

@dataclass
class LLMConfig:
    backend: str = "none"              # "none" or "ollama"
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen2.5:14b-instruct"
    timeout_s: int = 120
    temperature: float = 0.0
    top_p: float = 0.9
    num_ctx: int = 4096
    max_calls_per_doc: int = 12
    cache_dir: Optional[str] = None
    seed: int = 42

    # feature toggles
    refine_tags: bool = False          # stage-2 tagging for ambiguous sentences
    recipe_miner: bool = False         # extract recipe sentences from paragraphs
    noise_gate: bool = False           # NEW: filter noise (caps/refs/tables) with small LLM
    gate_model: str = "llama3.2:3b"    # NEW: smaller model for gating
    gate_max_calls_per_doc: int = 20   # NEW: budget for gate

    # strictness
    strict_json: bool = True
    retry: int = 2
    retry_backoff_s: float = 1.2

class OllamaClient:
    """
    Minimal Ollama HTTP client.
    """
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.base = cfg.ollama_url.rstrip("/")
        random.seed(cfg.seed)

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            raise OllamaError(f"Ollama HTTPError {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise OllamaError(f"Ollama URLError: {e}") from e
        except Exception as e:
            raise OllamaError(f"Ollama request failed: {e}") from e

    def chat(self, system: str, user: str, model_override: str = "") -> str:
        payload = {
            "model": model_override if model_override else self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "temperature": float(self.cfg.temperature),
                "top_p": float(self.cfg.top_p),
                "num_ctx": int(self.cfg.num_ctx),
            },
            "stream": False,
        }
        resp = self._post_json("/api/chat", payload)
        msg = resp.get("message", {})
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        return content.strip()

class JSONLCache:
    """
    Simple append-only JSONL cache.
    """
    def __init__(self, cache_path: Path):
        self.path = cache_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        if not self.path.exists():
            self._loaded = True
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = json.loads(ln)
                        k = obj.get("key")
                        v = obj.get("value")
                        if isinstance(k, str):
                            self._data[k] = v
                    except Exception:
                        continue
        except Exception as e:
            LOGGER.warning(f"Cache load failed: {self.path} ({e})")
        self._loaded = True

    def get(self, key: str) -> Optional[Any]:
        self.load()
        return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        self.load()
        if key in self._data:
            return
        self._data[key] = value
        rec = {"key": key, "value": value, "ts": time.time()}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            LOGGER.warning(f"Cache write failed: {e}")

def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

def parse_strict_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Parse JSON from model output.
    If extra text exists, extract first {...}.
    """
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# =============================================================================
# 11) LLM prompts + refiner
# =============================================================================

LLM_SYSTEM_TAGGER = (
    "You are a scientific text classifier for battery experimental procedures. "
    "You must follow the tag taxonomy and output STRICT JSON only."
)

def build_tagger_user_prompt(sentence: str, prev_sentence: str = "", next_sentence: str = "") -> str:
    return (
        "Classify the TARGET sentence into exactly one tag from this list:\n"
        f"{', '.join(list(TAG_TAXONOMY) + ['SIMULATION'])}\n\n"
        "Tag definitions (short):\n"
        "- SIMULATION: computational/modeling/theoretical work (DFT, MD, FEM, COMSOL)\n"
        "- PROC_ZN: Zn anode/foil surface treatment/coating/dipping/washing/drying/annealing/electrodeposition recipe\n"
        "- PROC_COAT_MAT: synthesis/preparation of coating material or precursor solution recipe\n"
        "- ELECTROLYTE: electrolyte composition/additives/concentration/pH recipe\n"
        "- ASSEMBLY: cell assembly (coin/swagelok/pouch, separator, assembly steps)\n"
        "- FORMATION_IN_SITU: formation/activation/conditioning protocol to build SEI/layer during cycling\n"
        "- CHAR: characterization/instrument protocol (SEM/XRD/XPS/Raman/etc.)\n"
        "- ECHEM_TEST: electrochemical test conditions (CV/EIS/GCD etc.) without formation purpose\n"
        "- RESULT: results/discussion/mechanism claims (as shown in Fig..., reveals..., performance...)\n"
        "- OTHER: none of the above\n\n"
        "Return STRICT JSON only:\n"
        '{"tag":"<ONE_TAG>","rationale":"<=15 words"}\n\n'
        f"PREV: {prev_sentence}\n"
        f"TARGET: {sentence}\n"
        f"NEXT: {next_sentence}\n"
    )

LLM_SYSTEM_RECIPE_MINER = (
    "You extract ONLY reproducible procedure sentences (recipes) from scientific text. "
    "Never include results, interpretation, or characterization. Output STRICT JSON only."
)

def build_recipe_miner_prompt(paragraph: str) -> str:
    return (
        "From the PARAGRAPH, extract ONLY procedure sentences that describe 'how it was made/treated/assembled'.\n"
        "Rules:\n"
        "- Keep ONLY recipe sentences. Drop results/claims (e.g., 'shows', 'reveals', 'improves').\n"
        "- If a sentence contains both recipe and characterization, keep only the recipe clause.\n"
        "- Prefer Zn anode/foil recipes.\n"
        "Return STRICT JSON only:\n"
        f"PARAGRAPH:\n{paragraph}\n"
    )

LLM_SYSTEM_NOISE_GATE = (
    "You are a filter. Decide if the text is meaningful procedural content or noise (Title, Table row, Reference, Caption). "
    "Output STRICT JSON only: {\"keep\": true/false, \"kind\": \"...\"}"
)

def build_noise_gate_prompt(text: str) -> str:
    return (
        "Classify the TEXT.\n"
        "KEEP if: It is a sentence about materials, methods, synthesis, assembly, or electrochemical testing.\n"
        "DISCARD if: It is a Table row, Figure caption, Reference, Journal header, Title, or Garbage.\n"
        "Return STRICT JSON only: {\"keep\": true/false, \"kind\": \"PROC/TABLE/CAPTION/REF/OTHER\"}\n\n"
        f"TEXT: {text}\n"
    )

@dataclass
class LLMStats:
    calls_total: int = 0
    calls_cached: int = 0
    calls_failed: int = 0

class LLMRefiner:
    """
    Stage-2 refiner: wraps OllamaClient with caching and strict JSON validation.
    """
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.stats = LLMStats()
        self.client: Optional[OllamaClient] = None
        self.cache: Optional[JSONLCache] = None

        if cfg.backend == "ollama":
            self.client = OllamaClient(cfg)

        if cfg.cache_dir:
            cache_dir = Path(cfg.cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            self.cache = JSONLCache(cache_dir / f"azib_v18_cache_{cfg.model.replace(':','_')}.jsonl")

    def _cache_key(self, kind: str, payload: str) -> str:
        base = f"{PROMPT_VERSION}|{self.cfg.model}|{kind}|{payload}"
        return stable_hash(base)

    def refine_tag(self, sentence: str, prev_sentence: str = "", next_sentence: str = "") -> Optional[Tuple[str, str]]:
        if not self.client:
            return None

        user_prompt = build_tagger_user_prompt(sentence, prev_sentence, next_sentence)
        key = self._cache_key("tagger", user_prompt)
        if self.cache:
            cached = self.cache.get(key)
            if cached:
                self.stats.calls_cached += 1
                tag = cached.get("tag")
                rat = cached.get("rationale", "")
                if isinstance(tag, str) and tag in TAG_SET:
                    return tag, str(rat)

        for attempt in range(self.cfg.retry + 1):
            try:
                self.stats.calls_total += 1
                out = self.client.chat(system=LLM_SYSTEM_TAGGER, user=user_prompt)
                obj = parse_strict_json(out) if self.cfg.strict_json else (parse_strict_json(out) or {})
                if not obj:
                    raise ValueError("No JSON parsed")
                tag = obj.get("tag")
                rationale = obj.get("rationale", "")
                if not isinstance(tag, str) or tag not in TAG_SET:
                    raise ValueError(f"Invalid tag: {tag}")
                if self.cache:
                    self.cache.set(key, {"tag": tag, "rationale": str(rationale)})
                return tag, str(rationale)
            except Exception:
                self.stats.calls_failed += 1
                if attempt < self.cfg.retry:
                    time.sleep(self.cfg.retry_backoff_s * (attempt + 1))
                    continue
                return None
        return None

    def mine_recipes(self, paragraph: str) -> Optional[List[str]]:
        if not self.client:
            return None
        paragraph = paragraph.strip()
        if not paragraph:
            return None
        # keep small to reduce cost
        if len(paragraph) > 2400:
            paragraph = paragraph[:2400]

        user_prompt = build_recipe_miner_prompt(paragraph)
        key = self._cache_key("recipe_miner", user_prompt)

        if self.cache:
            cached = self.cache.get(key)
            if cached and isinstance(cached, dict) and "recipes" in cached:
                self.stats.calls_cached += 1
                recs = cached.get("recipes", [])
                if isinstance(recs, list):
                    return [str(x).strip() for x in recs if str(x).strip()]

        for attempt in range(self.cfg.retry + 1):
            try:
                self.stats.calls_total += 1
                out = self.client.chat(system=LLM_SYSTEM_RECIPE_MINER, user=user_prompt)
                obj = parse_strict_json(out) if self.cfg.strict_json else (parse_strict_json(out) or {})
                if not obj:
                    raise ValueError("No JSON parsed")
                recs = obj.get("recipes", [])
                if not isinstance(recs, list):
                    raise ValueError("recipes is not a list")
                cleaned = [str(x).strip() for x in recs if str(x).strip()]
                cleaned2: List[str] = []
                for s in cleaned:
                    # drop result-only; keep if it's clearly a Zn procedure
                    if RESULT_RE.search(s) and not (ZN_SURFACE_ANCHOR_RE.search(s) and ZN_SURFACE_VERB_RE.search(s)):
                        continue
                    cleaned2.append(s)
                if self.cache:
                    self.cache.set(key, {"recipes": cleaned2, "notes": str(obj.get("notes", ""))})
                return cleaned2
            except Exception:
                self.stats.calls_failed += 1
                if attempt < self.cfg.retry:
                    time.sleep(self.cfg.retry_backoff_s * (attempt + 1))
                    continue
                return None
        return None

    def noise_gate_keep(self, text: str) -> bool:
        """
        Returns True if we should keep this text (procedural), False if noise.
        Uses the smaller gate model.
        """
        if not self.client:
            return True # default keep if no client

        user_prompt = build_noise_gate_prompt(text)
        key = self._cache_key(f"gate_{self.cfg.gate_model}", user_prompt)
        
        if self.cache:
            cached = self.cache.get(key)
            if cached and isinstance(cached, dict) and "keep" in cached:
                self.stats.calls_cached += 1
                return bool(cached.get("keep"))

        for attempt in range(self.cfg.retry + 1):
            try:
                self.stats.calls_total += 1
                out = self.client.chat(
                    system=LLM_SYSTEM_NOISE_GATE, 
                    user=user_prompt, 
                    model_override=self.cfg.gate_model
                )
                obj = parse_strict_json(out) if self.cfg.strict_json else (parse_strict_json(out) or {})
                if not obj:
                    raise ValueError("No JSON parsed")
                keep = bool(obj.get("keep", True))
                kind = str(obj.get("kind", "UNKNOWN"))
                
                if self.cache:
                    self.cache.set(key, {"keep": keep, "kind": kind})
                return keep
            except Exception:
                self.stats.calls_failed += 1
                if attempt < self.cfg.retry:
                    time.sleep(self.cfg.retry_backoff_s * (attempt + 1))
                    continue
                return True # fail-safe keep
        return True


# =============================================================================
# 12) Data structures
# =============================================================================

@dataclass
class EvidenceBlock:
    heading_norm: str
    heading_raw: str
    block_text: str
    start_line: int = -1
    end_line: int = -1
    level: int = 0
    kind: str = ""

@dataclass
class TaggedSentence:
    tag: str
    sentence: str
    source_heading: str
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    llm_used: bool = False
    llm_rationale: str = ""

@dataclass
class DocWarnings:
    read_error: bool = False
    no_headings: bool = False
    missing_methods: bool = False
    low_proc_zn: bool = False
    fallback_used: bool = False
    sectionless_used: bool = False
    methods_pointer_stub: bool = False
    no_keep_sentences: bool = False
    llm_budget_exhausted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

@dataclass
class QAStats:
    headings_total: int = 0
    candidate_blocks: int = 0
    sentences_tagged: int = 0
    kept_sentences: int = 0
    kept_proc_zn: int = 0
    fallback_sentences: int = 0
    sectionless_paras_used: int = 0
    
    # v21: Preprocessing quality metrics (detect "망한" preprocessing)
    sent_with_newline_count: int = 0     # Sentences containing '\n' (should be ~0)
    avg_sentence_len: float = 0.0        # Average sentence length
    p95_sentence_len: int = 0            # 95th percentile sentence length
    frontmatter_hit_count: int = 0       # Sentences with University/Authors/ORCID (should be ~0)
    caption_head_count: int = 0          # Sentences starting with Fig/Table (should be ~0)
    hyphen_break_count: int = 0          # '-\\s+[a-z]' matches (should be ~0)
    digit_heavy_sent_count: int = 0      # Sentences with >35% digits and no verbs (table leakage)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


# QA metric helpers
FRONTMATTER_LEAK_RE = re.compile(r"(?ix)\b(corresponding\s+author|orcid|university|department|faculty|institute|authors?)\b")
CAPTION_HEAD_QA_RE = re.compile(r"(?i)^\s*(fig(ure)?|table|scheme)\b")
HYPHEN_BREAK_QA_RE = re.compile(r"-\s+[a-z]")

def compute_qa_preprocessing_metrics(tagged_sentences: List[TaggedSentence], qa: QAStats) -> None:
    """
    Compute preprocessing quality metrics and update QAStats.
    Call this after all tagging is done.
    """
    if not tagged_sentences:
        return
    
    lens = []
    for ts in tagged_sentences:
        s = ts.sentence
        lens.append(len(s))
        
        # Sentences with newlines (should be ~0 after proper processing)
        if "\n" in s:
            qa.sent_with_newline_count += 1
        
        # Frontmatter leakage
        if FRONTMATTER_LEAK_RE.search(s):
            qa.frontmatter_hit_count += 1
        
        # Caption head leakage
        if CAPTION_HEAD_QA_RE.match(s):
            qa.caption_head_count += 1
        
        # Hyphen break leakage
        qa.hyphen_break_count += len(HYPHEN_BREAK_QA_RE.findall(s))
        
        # Digit-heavy sentences (table leakage)
        if len(s) > 50:
            digit_ratio = sum(c.isdigit() for c in s) / len(s)
            # Check if has any verb (simple heuristic)
            has_verb = bool(re.search(r"\b(is|was|were|are|be|been|being|have|has|had|do|does|did|will|would|could|should|can|may|might|shall)\b", s, re.IGNORECASE))
            if not has_verb:
                has_verb = bool(re.search(r"\b\w+ed\b|\b\w+ing\b", s))  # past/present participle
            if digit_ratio > 0.35 and not has_verb:
                qa.digit_heavy_sent_count += 1
    
    # Compute statistics
    if lens:
        qa.avg_sentence_len = sum(lens) / len(lens)
        sorted_lens = sorted(lens)
        p95_idx = int(len(sorted_lens) * 0.95)
        qa.p95_sentence_len = sorted_lens[min(p95_idx, len(sorted_lens) - 1)]

@dataclass
class DocResult:
    file: str
    found: bool
    hit_count: int
    evidence: List[EvidenceBlock]
    evidence_cleaned: List[Dict[str, str]]
    tagged: List[TaggedSentence]
    chunks_by_tag: Dict[str, List[str]]
    proc_zn_count: int
    fallback_used: bool
    llm_calls: int
    warnings: DocWarnings
    headings: List[Heading] = field(default_factory=list)
    qa_stats: QAStats = field(default_factory=QAStats)

    def to_csv_row(self, extended: bool = False) -> List[Any]:
        row = [
            self.file,
            self.found,
            self.hit_count,
            json.dumps([dataclasses.asdict(e) for e in self.evidence], ensure_ascii=False),
            json.dumps(self.evidence_cleaned, ensure_ascii=False),
            json.dumps([dataclasses.asdict(t) for t in self.tagged], ensure_ascii=False),
            json.dumps(self.chunks_by_tag, ensure_ascii=False),
            self.proc_zn_count,
            self.fallback_used,
            self.llm_calls,
            json.dumps(self.warnings.to_dict(), ensure_ascii=False),
        ]
        if extended:
            row.append(json.dumps([dataclasses.asdict(h) for h in self.headings], ensure_ascii=False))
            row.append(json.dumps(self.qa_stats.to_dict(), ensure_ascii=False))
        return row

    def to_json(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "found": self.found,
            "hit_count": self.hit_count,
            "evidence": [dataclasses.asdict(e) for e in self.evidence],
            "evidence_cleaned": self.evidence_cleaned,
            "tagged": [dataclasses.asdict(t) for t in self.tagged],
            "chunks_by_tag": self.chunks_by_tag,
            "proc_zn_count": self.proc_zn_count,
            "fallback_used": self.fallback_used,
            "llm_calls": self.llm_calls,
            "warnings": self.warnings.to_dict(),
            "headings": [dataclasses.asdict(h) for h in self.headings],
            "qa_stats": self.qa_stats.to_dict(),
        }


# =============================================================================
# 13) Analyzer
# =============================================================================

@dataclass
class AnalyzerConfig:
    # thresholds
    min_proc_zn: int = 1
    keep_context: int = 1
    min_block_chars: int = 60
    min_block_words: int = 8

    # behavior
    include_heading_line_in_block: bool = False
    extended_csv: bool = False

    # performance / guards
    max_sentences_per_doc: int = 6000
    max_chars_per_doc: int = 5_000_000
    sectionless_top_k_paras: int = 25
    sectionless_min_score: int = 3

class AZIBAnalyzer:
    def __init__(self, acfg: AnalyzerConfig, lcfg: LLMConfig):
        self.acfg = acfg
        self.lcfg = lcfg
        self.refiner = LLMRefiner(lcfg) if lcfg.backend != "none" else None

    def _read_md(self, path: Path) -> Optional[str]:
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            LOGGER.error(f"Failed to read {path}: {e}")
            return None
        if raw and len(raw) > self.acfg.max_chars_per_doc:
            raw = raw[: self.acfg.max_chars_per_doc]
        return raw

    def _collect_headings(self, lines: List[str]) -> List[Heading]:
        headings: List[Heading] = []
        for idx, ln in enumerate(lines):
            h = HeadingExtractor.extract_heading(ln, idx=idx)
            if h is None:
                continue
            if h.norm.strip():
                headings.append(h)
        return headings

    def _section_bounds(self, headings: List[Heading], j: int, n_lines: int) -> Tuple[int, int]:
        """
        Hierarchical boundary with special rule:
        - For NON-S headings (ATX/NUM/PLAIN...), do NOT let an S-heading close the section
          (because in SI, '## Experimental Procedures' often contains 'S1, S2, ...').
        - For S headings, keep the original rule (S1 ends at S2, etc.).
        This makes "## Experimental Procedures" include "S1, S2, ..." subsections.
        """
        start = headings[j].idx
        cur = headings[j]
        cur_level = cur.level
        end = n_lines

        for k in range(j + 1, len(headings)):
            nxt = headings[k]
            if nxt.level <= cur_level:
                # Key fix: non-S parent should not be closed by S-heading child
                if cur.kind != "S" and nxt.kind == "S":
                    continue
                end = nxt.idx
                break

        return start, end

    def _is_methods_pointer_stub(self, heading_norm: str, block_text: str) -> bool:
        if not METHODS_POINTER_RE.search(block_text):
            return False
        if len(block_text) <= METHODS_POINTER_MAX_CHARS:
            return True
        if len(block_text.split()) <= METHODS_POINTER_MAX_WORDS:
            return True
        # extra guard: if block has almost no verbs/conditions, it's likely a pointer stub
        if not (ZN_SURFACE_VERB_RE.search(block_text) or SOLUTION_VERB_RE.search(block_text) or COND_RE.search(block_text)):
            return True
        return False

    def _extract_candidate_blocks(self, lines: List[str], headings: List[Heading], warnings: DocWarnings, qa: QAStats) -> List[EvidenceBlock]:
        if not headings:
            return []

        evidence: List[EvidenceBlock] = []
        n_lines = len(lines)

        for j, h in enumerate(headings):
            # Candidate heading?
            is_candidate = bool(METHOD_HEADING_RE.search(h.norm))
            
            # NEW: S-heading is also candidate if it matches RECIPE_HEADING_RE
            if (not is_candidate) and (h.kind == "S") and RECIPE_HEADING_RE.search(h.norm):
                is_candidate = True

            if not is_candidate:
                continue
            if HEADER_EXCLUDE_RE.search(h.norm):
                continue
            # Simulation exclusion (Heading-level)
            if SIM_HEADING_RE.search(h.norm):
                continue

            start, end = self._section_bounds(headings, j, n_lines)
            block_lines = lines[start:end]

            if self.acfg.include_heading_line_in_block:
                body_lines = block_lines
            else:
                body_lines = block_lines[1:] if len(block_lines) > 1 else []

            block_text = cleanup_block_text("\n".join(body_lines).strip())

            # NEW: Literature comparison table drop (v21 backport)
            if looks_like_literature_comparison_table(block_text):
                continue

            # Block-level content simulation check (ratio-based to avoid killing mixed blocks)
            if SIM_SENT_RE.search(block_text):
                sim_hits = len(SIM_SENT_RE.findall(block_text))
                sents_tmp = split_sentences_fast(block_text)  # P0: Use fast version
                sim_ratio = sim_hits / max(1, len(sents_tmp))
                # Only cut if many sim hits AND high ratio (majority is sim)
                if sim_hits >= 6 and sim_ratio >= 0.35:
                    continue

            if len(block_text) < self.acfg.min_block_chars:
                continue
            if len(block_text.split()) < self.acfg.min_block_words:
                continue

            # stub detection: Experimental section that only points to SI
            if self._is_methods_pointer_stub(h.norm, block_text):
                warnings.methods_pointer_stub = True
                # skip this stub block; force fallback later
                continue

            evidence.append(EvidenceBlock(
                heading_norm=h.norm,
                heading_raw=h.raw,
                block_text=block_text,
                start_line=start,
                end_line=end,
                level=h.level,
                kind=h.kind,
            ))

        qa.candidate_blocks = len(evidence)
        return evidence

    def _extract_recipe_blocks_fallback(self, lines: List[str], headings: List[Heading]) -> List[EvidenceBlock]:
        """
        Permissive heading-based fallback: scan whole doc for recipe-like headings,
        and extract hierarchical blocks.
        """
        if not headings:
            return []
        out: List[EvidenceBlock] = []
        n_lines = len(lines)

        for j, h in enumerate(headings):
            if not RECIPE_HEADING_RE.search(h.norm):
                continue
            if HEADER_EXCLUDE_RE.search(h.norm):
                continue
            if SIM_HEADING_RE.search(h.norm):
                continue

            start, end = self._section_bounds(headings, j, n_lines)
            block_lines = lines[start:end]
            body_lines = block_lines[1:] if len(block_lines) > 1 else []
            block_text = cleanup_block_text("\n".join(body_lines).strip())
            
            # NEW: Literature comparison table drop (v21 backport)
            if looks_like_literature_comparison_table(block_text):
                continue

            # Block-level content sim check (ratio-based)
            sim_hits = len(SIM_SENT_RE.findall(block_text))
            sents_tmp = split_sentences_fast(block_text)  # P0: Use fast version
            sim_ratio = sim_hits / max(1, len(sents_tmp))
            if sim_hits >= 6 and sim_ratio >= 0.35:
                continue

            if len(block_text) < self.acfg.min_block_chars:
                continue
            if len(block_text.split()) < self.acfg.min_block_words:
                continue

            out.append(EvidenceBlock(
                heading_norm=h.norm,
                heading_raw=h.raw,
                block_text=block_text,
                start_line=start,
                end_line=end,
                level=h.level,
                kind=h.kind,
            ))
        return out

    def _dedup_preserve(self, seq: List[str]) -> List[str]:
        seen = set()
        out: List[str] = []
        for x in seq:
            k = x.strip()
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(k)
        return out

    def _tag_sentences(
        self,
        text: str,
        source_heading: str,
        llm_budget_left: int,
        gate_budget_left: int,
        tagged_out: List[TaggedSentence],
        tag_cache: Optional[Dict[str, Tuple[str, float, List[str]]]] = None,
    ) -> Tuple[int, int]:
        """
        Tag sentences from text, append to tagged_out. Return (main_budget, gate_budget).
        P0 OPTIMIZATION: Use split_sentences_fast since text is already repaired at doc level.
        P0 OPTIMIZATION: Use tag_cache to avoid re-tagging same sentences.
        """
        sents = split_sentences_fast(text)
        if len(sents) > self.acfg.max_sentences_per_doc:
            sents = sents[: self.acfg.max_sentences_per_doc]

        for i, sent in enumerate(sents):
            # 0. Table dump sentence check (2차 방어)
            if is_table_dump_sentence(sent):
                continue

            # 1. Skip bibliography-like sentences explicitly
            if is_bibliography_like_sentence(sent):
                continue
                
            # 2. Heuristic Noise Gate (Optional LLM check)
            if self.refiner and self.lcfg.noise_gate and gate_budget_left > 0:
                if needs_noise_gate(sent):
                    # trigger gate
                    keep = self.refiner.noise_gate_keep(sent)
                    gate_budget_left -= 1
                    if not keep:
                        continue # Dropped by gate

            # P0 OPTIMIZATION: Check cache first
            cache_key = sent.strip()
            if tag_cache is not None and cache_key in tag_cache:
                tag, conf, reasons = tag_cache[cache_key]
                reasons = list(reasons) + ["cached"]  # Copy to avoid mutation
            else:
                tag, conf, reasons = heuristic_tag_sentence(sent)
                if tag_cache is not None:
                    tag_cache[cache_key] = (tag, conf, list(reasons))
            ts = TaggedSentence(
                tag=tag,
                sentence=sent,
                source_heading=source_heading,
                confidence=conf,
                reasons=reasons,
                llm_used=False,
                llm_rationale="",
            )

            if self.refiner and self.lcfg.refine_tags and llm_budget_left > 0:
                if is_ambiguous(tag, conf, sent):
                    prev_s = sents[i - 1] if i - 1 >= 0 else ""
                    next_s = sents[i + 1] if i + 1 < len(sents) else ""
                    refined = self.refiner.refine_tag(sent, prev_s, next_s)
                    if refined:
                        new_tag, rationale = refined
                        
                        # NEW GUARD: Prevent PROC_ZN promotion without Zn-metal context (v21 backport)
                        if new_tag == "PROC_ZN":
                            has_zn_anchor = ZN_SURFACE_ANCHOR_RE.search(sent)
                            has_zn_verb = ZN_SURFACE_VERB_RE.search(sent)
                            if not has_zn_anchor:
                                # Reject PROC_ZN override if no Zn anchor
                                new_tag = tag
                                rationale = f"rejected PROC_ZN (no Zn-metal context). orig={tag}"
                                ts.reasons.append(f"LLM:rejected_PROC_ZN(no Zn anchor)")
                            elif has_zn_anchor and not has_zn_verb:
                                # weak anchor without verb - also reject
                                new_tag = tag
                                rationale = f"rejected PROC_ZN (Zn anchor but no verb). orig={tag}"
                                ts.reasons.append(f"LLM:rejected_PROC_ZN(no verb)")
                        
                        if new_tag != tag:
                            ts.reasons.append(f"LLM:override({tag}->{new_tag})")
                        ts.tag = new_tag
                        ts.llm_used = True
                        ts.llm_rationale = rationale
                        ts.confidence = max(ts.confidence, 0.80)
                    else:
                        ts.reasons.append("LLM:failed")
                    llm_budget_left -= 1

            tagged_out.append(ts)

        return llm_budget_left, gate_budget_left

    def _apply_recipe_miner(
        self,
        paragraphs: List[str],
        source_heading: str,
        llm_budget_left: int,
        tagged_out: List[TaggedSentence],
        qa: QAStats,
    ) -> int:
        """
        Optional LLM recipe miner: extract recipe sentences from paragraphs and tag them.
        """
        if not (self.refiner and self.lcfg.recipe_miner and llm_budget_left > 0):
            return llm_budget_left

        for para in paragraphs:
            if llm_budget_left <= 0:
                break

            # only mine from paragraphs that look promising
            if paragraph_recipe_score(para) < 3:
                continue

            recs = self.refiner.mine_recipes(para)
            llm_budget_left -= 1
            if not recs:
                continue

            qa.sectionless_paras_used += 1 if source_heading == "__SECTIONLESS__" else 0

            for r in recs:
                tag, conf, reasons = heuristic_tag_sentence(r)
                ts = TaggedSentence(
                    tag=tag,
                    sentence=r,
                    source_heading=source_heading,
                    confidence=max(conf, 0.75),
                    reasons=reasons + ["LLM:recipe_miner"],
                    llm_used=True,
                    llm_rationale="recipe_miner",
                )
                tagged_out.append(ts)

        return llm_budget_left

    def analyze_file(self, md_path: Path, base_dir: Path) -> DocResult:
        rel = str(md_path.relative_to(base_dir))
        warnings = DocWarnings()
        qa = QAStats()

        raw = self._read_md(md_path)
        if raw is None:
            warnings.read_error = True
            return DocResult(
                file=rel,
                found=False,
                hit_count=0,
                evidence=[],
                evidence_cleaned=[],
                tagged=[],
                chunks_by_tag={},
                proc_zn_count=0,
                fallback_used=False,
                llm_calls=0,
                warnings=warnings,
                headings=[],
                qa_stats=qa,
            )

        text_full = preprocess_text(raw)
        lines = text_full.splitlines()

        # Early document-level cleanup BEFORE heading detection:
        
        # 1. Strict caption removal (NEW)
        lines = remove_captions_early(lines)

        # 2. NEW: 표/데이터 덤프 제거 (가장 중요)
        lines = remove_table_like_blocks(lines)

        # 3. Drop reference section explicitly
        text_full_temp = "\n".join(lines)
        text_full_temp = drop_reference_section(text_full_temp)
        lines = text_full_temp.splitlines()

        # 4. Existing cleanups (front matter, noise)
        lines = remove_front_matter_blocks(lines)
        # remove_caption_blocks is now redundant but safe to keep as backup
        lines = remove_caption_blocks(lines)
        lines = remove_noise_lines(lines)

        # Use cleaned text downstream (fallback sentence mining, etc.)
        text = "\n".join(lines)
        
        # P0 OPTIMIZATION: Repair paragraphs ONCE at document level
        # All downstream split_sentences calls should use already_repaired=True
        text_repaired = repair_paragraphs(text)

        headings = self._collect_headings(lines)
        qa.headings_total = len(headings)
        if not headings:
            warnings.no_headings = True

        # Primary extraction: methods-ish headings with hierarchical bounds
        evidence_blocks: List[EvidenceBlock] = []
        if headings:
            evidence_blocks = self._extract_candidate_blocks(lines, headings, warnings, qa)

        if not evidence_blocks:
            warnings.missing_methods = True

        tagged_all: List[TaggedSentence] = []
        chunks_by_tag: Dict[str, List[str]] = {k: [] for k in TAG_KEEP}
        proc_zn_count = 0

        llm_budget = self.lcfg.max_calls_per_doc if self.refiner else 0
        gate_budget = self.lcfg.gate_max_calls_per_doc if self.refiner else 0
        llm_calls_start = self.refiner.stats.calls_total if self.refiner else 0

        # P0 OPTIMIZATION: Document-level sentence tag cache to avoid re-tagging same sentences
        tag_cache: Dict[str, Tuple[str, float, List[str]]] = {}

        # Tag evidence blocks
        for eb in evidence_blocks:
            llm_budget, gate_budget = self._tag_sentences(eb.block_text, eb.heading_norm, llm_budget, gate_budget, tagged_all, tag_cache)

            # paragraph miner within blocks (optional)
            if self.refiner and self.lcfg.recipe_miner and llm_budget > 0:
                paras = [p.strip() for p in repair_paragraphs(eb.block_text).split("\n") if p.strip()]
                llm_budget = self._apply_recipe_miner(paras, eb.heading_norm, llm_budget, tagged_all, qa)

        # Populate kept chunks
        for ts in tagged_all:
            if ts.tag in KEEP_SET:
                chunks_by_tag[ts.tag].append(ts.sentence)
            if ts.tag == "PROC_ZN":
                proc_zn_count += 1

        # Deduplicate
        chunks_by_tag = {k: self._dedup_preserve(v) for k, v in chunks_by_tag.items()}
        chunks_by_tag = {k: v for k, v in chunks_by_tag.items() if v}

        if proc_zn_count < self.acfg.min_proc_zn:
            warnings.low_proc_zn = True

        fallback_used = False
        recipe_blocks_used: List[EvidenceBlock] = []  # Track fallback blocks for hit_count

        # Fallback triggers:
        # - no evidence blocks
        # - PROC_ZN below threshold
        # - methods pointer stub detected
        if (not evidence_blocks) or (proc_zn_count < self.acfg.min_proc_zn) or warnings.methods_pointer_stub:
            fallback_used = True
            warnings.fallback_used = True

            # 1) Heading-based fallback (permissive recipe headings)
            if headings:
                recipe_blocks = self._extract_recipe_blocks_fallback(lines, headings)
                recipe_blocks_used = recipe_blocks  # Store for hit_count
                for rb in recipe_blocks:
                    llm_budget, gate_budget = self._tag_sentences(rb.block_text, rb.heading_norm, llm_budget, gate_budget, tagged_all, tag_cache)
                    if self.refiner and self.lcfg.recipe_miner and llm_budget > 0:
                        paras = [p.strip() for p in repair_paragraphs(rb.block_text).split("\n") if p.strip()]
                        llm_budget = self._apply_recipe_miner(paras, rb.heading_norm, llm_budget, tagged_all, qa)

            # 2) Sentence-based fallback (Zn-surface anchor)
            fb_sents = mine_fallback_sentences(cleanup_block_text(text), keep_context=self.acfg.keep_context)
            qa.fallback_sentences = len(fb_sents)
            for s in fb_sents:
                # P0 OPTIMIZATION: Use cache for fallback sentences too
                cache_key = s.strip()
                if cache_key in tag_cache:
                    tag, conf, reasons = tag_cache[cache_key]
                    reasons = list(reasons) + ["cached", "fallback_sentence"]
                else:
                    tag, conf, reasons = heuristic_tag_sentence(s)
                    tag_cache[cache_key] = (tag, conf, list(reasons))
                    reasons = reasons + ["fallback_sentence"]
                tagged_all.append(
                    TaggedSentence(
                        tag=tag,
                        sentence=s,
                        source_heading="__FALLBACK_SENTENCE__",
                        confidence=conf,
                        reasons=reasons,
                        llm_used=False,
                        llm_rationale="",
                    )
                )

            # 3) Sectionless paragraph miner (only when no headings or still low PROC_ZN)
            #    This addresses papers with no clear section delimiters.
            if warnings.no_headings or (proc_zn_count < self.acfg.min_proc_zn):
                warnings.sectionless_used = True
                paras = select_top_paragraphs(
                    full_text=text,
                    top_k=self.acfg.sectionless_top_k_paras,
                    min_score=self.acfg.sectionless_min_score,
                )
                qa.sectionless_paras_used = len(paras)
                # Mine recipes via LLM if enabled
                if self.refiner and self.lcfg.recipe_miner and llm_budget > 0 and paras:
                    llm_budget = self._apply_recipe_miner(paras, "__SECTIONLESS__", llm_budget, tagged_all, qa)
                else:
                    # heuristic-only: tag sentences from selected paragraphs
                    for p in paras:
                        llm_budget, gate_budget = self._tag_sentences(p, "__SECTIONLESS__", llm_budget, gate_budget, tagged_all, tag_cache)

            # rebuild chunks_by_tag after fallback
            chunks2: Dict[str, List[str]] = {k: [] for k in TAG_KEEP}
            proc2 = 0
            for ts in tagged_all:
                if ts.tag in KEEP_SET:
                    chunks2[ts.tag].append(ts.sentence)
                if ts.tag == "PROC_ZN":
                    proc2 += 1
            chunks2 = {k: self._dedup_preserve(v) for k, v in chunks2.items()}
            chunks2 = {k: v for k, v in chunks2.items() if v}
            chunks_by_tag = chunks2
            proc_zn_count = proc2

        qa.sentences_tagged = len(tagged_all)
        qa.kept_sentences = sum(len(v) for v in chunks_by_tag.values())
        qa.kept_proc_zn = proc_zn_count

        # v21: Compute preprocessing quality metrics
        compute_qa_preprocessing_metrics(tagged_all, qa)

        found = any(len(v) > 0 for v in chunks_by_tag.values())
        if not found:
            warnings.no_keep_sentences = True

        if self.refiner and (llm_budget <= 0) and (self.refiner.stats.calls_total > llm_calls_start):
            warnings.llm_budget_exhausted = True

        # Evidence cleaned output
        evidence_cleaned: List[Dict[str, str]] = []
        for eb in evidence_blocks:
            evidence_cleaned.append({
                "heading_norm": strip_markdown_emphasis(eb.heading_norm),
                "heading_raw": eb.heading_raw,
                "block_text": strip_markdown_emphasis(eb.block_text),
                "start_line": eb.start_line,
                "end_line": eb.end_line,
                "level": eb.level,
                "kind": eb.kind,
            })

        # per-doc llm calls delta
        llm_calls = 0
        if self.refiner:
            llm_calls = max(0, self.refiner.stats.calls_total - llm_calls_start)

        # Compute total hit_count: primary + fallback blocks
        total_hit_count = len(evidence_blocks) + len(recipe_blocks_used)

        return DocResult(
            file=rel,
            found=found,
            hit_count=total_hit_count,
            evidence=evidence_blocks,
            evidence_cleaned=evidence_cleaned,
            tagged=tagged_all,
            chunks_by_tag=chunks_by_tag,
            proc_zn_count=proc_zn_count,
            fallback_used=fallback_used,
            llm_calls=llm_calls,
            warnings=warnings,
            headings=headings,
            qa_stats=qa,
        )


# =============================================================================
# 14) Runner / IO
# =============================================================================

CSV_HEADER_BASE = [
    "File",
    "Found",
    "HitCount",
    "EvidenceJSON",
    "EvidenceJSON_Cleaned",
    "TaggedSentencesJSON",
    "CleanedChunksByTagJSON",
    "ProcZnCount",
    "FallbackUsed",
    "LLMCalls",
    "WarningsJSON",
]

CSV_HEADER_EXT = CSV_HEADER_BASE + ["HeadingsJSON", "QAStatsJSON"]

def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

def write_json(path: Path, obj: Any) -> None:
    ensure_parent_dir(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def append_jsonl(path: Path, obj: Any) -> None:
    ensure_parent_dir(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def iter_md_files(input_dir: Path) -> List[Path]:
    return sorted(list(input_dir.rglob("*.md")))

def run_pipeline(
    input_dir: Path,
    output_csv: Path,
    out_json: Optional[Path],
    out_jsonl: Optional[Path],
    analyzer: AZIBAnalyzer,
    only_true: bool,
    extended_csv: bool,
    sample_n: int = 0,
) -> None:
    md_files = iter_md_files(input_dir)
    LOGGER.info(f"Found {len(md_files)} markdown files under {input_dir}")

    if sample_n > 0 and len(md_files) > sample_n:
        random.shuffle(md_files)
        md_files = md_files[:sample_n]
        LOGGER.info(f"Randomly sampled {len(md_files)} files.")

    ensure_parent_dir(output_csv)
    results_json: List[Dict[str, Any]] = []

    iterator = md_files
    if tqdm is not None:
        iterator = tqdm(md_files, desc="Processing", unit="file")  # type: ignore

    with output_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER_EXT if extended_csv else CSV_HEADER_BASE)

        for md in iterator:
            res = analyzer.analyze_file(md, input_dir)

            if not res.found:
                LOGGER.info(
                    f"[MISS] {res.file}: blocks={res.hit_count} proc_zn={res.proc_zn_count} "
                    f"fallback={res.fallback_used} llm_calls={res.llm_calls} "
                    f"warnings={res.warnings.to_dict()}"
                )

            # if only_true and not res.found:
            #     continue

            if res.found:
                LOGGER.info(
                    f"[HIT] {res.file}: blocks={res.hit_count} proc_zn={res.proc_zn_count} "
                    f"fallback={res.fallback_used} llm_calls={res.llm_calls} "
                    f"headings={len(res.headings)}"
                )

            writer.writerow(res.to_csv_row(extended=extended_csv))

            if out_json is not None:
                results_json.append(res.to_json())
            if out_jsonl is not None:
                append_jsonl(out_jsonl, res.to_json())

    if out_json is not None:
        write_json(out_json, results_json)

    LOGGER.info(f"Done. Saved CSV to: {output_csv}")
    if out_json:
        LOGGER.info(f"Saved JSON to: {out_json}")
    if out_jsonl:
        LOGGER.info(f"Saved JSONL to: {out_jsonl}")

    if analyzer.refiner:
        LOGGER.info(
            f"LLM stats: total={analyzer.refiner.stats.calls_total} "
            f"cached={analyzer.refiner.stats.calls_cached} "
            f"failed={analyzer.refiner.stats.calls_failed}"
        )


# =============================================================================
# 15) CLI
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AZIB Experimental Evidence Finder v18 (Commercial + Ollama-assisted + Hierarchical headings)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # IO
    p.add_argument("--input_dir", required=True, help="Directory containing Markdown files")
    p.add_argument("--output_csv", required=True, help="Output CSV path")
    p.add_argument("--out_json", default="", help="Optional output JSON path (empty disables)")
    p.add_argument("--out_jsonl", default="", help="Optional output JSONL path (empty disables)")
    p.add_argument("--only_true", action="store_true", help="Only write rows where Found=True")
    p.add_argument("--extended_csv", action="store_true", help="Append HeadingsJSON + QAStatsJSON columns")

    # thresholds
    p.add_argument("--min_proc_zn", type=int, default=1, help="Fallback trigger: PROC_ZN count < min_proc_zn")
    p.add_argument("--keep_context", type=int, default=1, help="Fallback context (+/- N sentences)")
    p.add_argument("--min_block_chars", type=int, default=60, help="Minimum chars for a candidate block")
    p.add_argument("--min_block_words", type=int, default=8, help="Minimum words for a candidate block")

    # sectionless
    p.add_argument("--sectionless_top_k_paras", type=int, default=25, help="Top-K paragraphs for sectionless miner")
    p.add_argument("--sectionless_min_score", type=int, default=3, help="Min paragraph score for sectionless miner")

    # LLM
    p.add_argument("--llm_backend", default="none", choices=["none", "ollama"], help="LLM backend")
    p.add_argument("--ollama_url", default="http://localhost:11434", help="Ollama base URL")
    p.add_argument("--llm_model", default="qwen2.5:14b-instruct", help="Ollama model name")
    p.add_argument("--llm_timeout", type=int, default=120, help="LLM timeout seconds")
    p.add_argument("--llm_temperature", type=float, default=0.0, help="LLM temperature (0 for determinism)")
    p.add_argument("--llm_top_p", type=float, default=0.9, help="LLM top_p")
    p.add_argument("--llm_num_ctx", type=int, default=4096, help="LLM context window (if supported)")
    p.add_argument("--llm_max_calls_per_doc", type=int, default=12, help="Maximum LLM calls per document")
    p.add_argument("--llm_cache_dir", default="", help="Cache directory (empty disables)")
    p.add_argument("--llm_refine_tags", action="store_true", help="Use LLM to refine ambiguous sentence tags")
    p.add_argument("--llm_recipe_miner", action="store_true", help="Use LLM to mine recipe sentences from paragraphs")
    p.add_argument("--llm_noise_gate", action="store_true", help="Use small LLM to filter noise (refs/tables/captions)")
    p.add_argument("--llm_gate_model", default="llama3.2:3b", help="Model for noise gate")
    p.add_argument("--llm_retry", type=int, default=2, help="LLM retries on failure")
    p.add_argument("--llm_retry_backoff", type=float, default=1.2, help="LLM retry backoff multiplier")
    p.add_argument("--llm_no_strict_json", action="store_true", help="Disable strict JSON validation (not recommended)")

    # logging
    p.add_argument("--log_level", default="INFO", help="Logging level (DEBUG/INFO/WARNING/ERROR)")
    p.add_argument("--log_file", default="", help="Optional log file path")
    
    # limits
    p.add_argument("--sample_n", type=int, default=0, help="Process a random sample of N files (0=all)")

    return p

def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    setup_logging(args.log_level, args.log_file if args.log_file else None)

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        LOGGER.error(f"Input dir not found: {input_dir}")
        sys.exit(2)

    output_csv = Path(args.output_csv)
    out_json = Path(args.out_json) if args.out_json.strip() else None
    out_jsonl = Path(args.out_jsonl) if args.out_jsonl.strip() else None

    acfg = AnalyzerConfig(
        min_proc_zn=int(args.min_proc_zn),
        keep_context=int(args.keep_context),
        min_block_chars=int(args.min_block_chars),
        min_block_words=int(args.min_block_words),
        extended_csv=bool(args.extended_csv),
        sectionless_top_k_paras=int(args.sectionless_top_k_paras),
        sectionless_min_score=int(args.sectionless_min_score),
    )

    lcfg = LLMConfig(
        backend=str(args.llm_backend),
        ollama_url=str(args.ollama_url),
        model=str(args.llm_model),
        timeout_s=int(args.llm_timeout),
        temperature=float(args.llm_temperature),
        top_p=float(args.llm_top_p),
        num_ctx=int(args.llm_num_ctx),
        max_calls_per_doc=int(args.llm_max_calls_per_doc),
        cache_dir=str(args.llm_cache_dir) if args.llm_cache_dir.strip() else None,
        refine_tags=bool(args.llm_refine_tags),
        recipe_miner=bool(args.llm_recipe_miner),
        noise_gate=bool(args.llm_noise_gate),
        gate_model=str(args.llm_gate_model),
        strict_json=not bool(args.llm_no_strict_json),
        retry=int(args.llm_retry),
        retry_backoff_s=float(args.llm_retry_backoff),
    )

    if lcfg.backend == "none":
        lcfg.refine_tags = False
        lcfg.recipe_miner = False

    analyzer = AZIBAnalyzer(acfg, lcfg)

    # Sanity: if LLM enabled, ensure Ollama is reachable early
    if lcfg.backend == "ollama":
        try:
            client = OllamaClient(lcfg)
            ping = client.chat("You are a system.", "Reply with JSON: {\"ok\":true}")
            _ = parse_strict_json(ping) or {"raw": ping}
            LOGGER.info(f"Ollama reachable at {lcfg.ollama_url}, model={lcfg.model}")
        except Exception as e:
            LOGGER.error(f"Ollama is not reachable or model failed: {e}")
            LOGGER.error("Start Ollama: `ollama serve` and ensure the model is pulled.")
            sys.exit(3)

    run_pipeline(
        input_dir=input_dir,
        output_csv=output_csv,
        out_json=out_json,
        out_jsonl=out_jsonl,
        analyzer=analyzer,
        only_true=bool(args.only_true),
        extended_csv=bool(args.extended_csv),
        sample_n=int(args.sample_n),
    )

if __name__ == "__main__":
    main()
