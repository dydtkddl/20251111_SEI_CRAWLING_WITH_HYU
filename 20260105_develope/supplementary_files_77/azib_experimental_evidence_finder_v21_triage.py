# -*- coding: utf-8 -*-
"""
AZIB Paper-Level Triage System v21.1 (Commercial-grade + 2-Stage Filter)
========================================================================

What this is
------------
A complete redesign from sentence-level tagging to PAPER-LEVEL TRIAGE for
rapid screening of 10k+ papers on aqueous zinc-ion battery ex-situ protective layers.

Key Changes from v21.0
----------------------
1. Fixed ELECTROLYTE_RE (removed mm - dimension misclassification)
2. Enhanced literature comparison table detection
3. Two-stage filtering: Rule-based pre-filter + LLM judgment
4. Improved block scoring with refined query weights
5. Stronger LLM prompt for ex-situ vs in-situ distinction
6. Heading-aware block extraction (prioritize Methods/Experimental)

Key Features
------------
1. Block-level scoring (not per-sentence)
2. Top-K block selection using query-based ranking
3. Two-stage filter: Rule-based heuristic → LLM judgment
4. Paper-level keep/drop/unsure judgment with LLM
5. Structured JSON output with evidence snippets

Output (per paper)
------------------
{
    "file": "path/to/paper.md",
    "triage": "keep|drop|unsure",
    "is_aqueous_zinc_battery": "yes|no|unsure",
    "has_zn_metal_anode": "yes|no|unsure",
    "ex_situ_protective_layer": "yes|no|unsure",
    "in_situ_only": "yes|no|unsure",
    "lab_scale_data": "yes|no|unsure",
    "confidence": 0.85,
    "summary": "One-line summary",
    "evidence": [
        {"heading": "Section Name", "snippet": "Key evidence text"}
    ],
    "notes": "Uncertain points",
    "rule_triage": "keep|drop|unsure",
    "rule_reason": "Why rule-based filter decided"
}

Usage
-----
python azib_experimental_evidence_finder_v21_triage.py ^
    --input_dir 02_supplementary_md ^
    --output_json out_v21_triage.json ^
    --top_k 5 ^
    --llm_backend ollama ^
    --llm_model qwen2.5:14b-instruct

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

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

import urllib.request
import urllib.error


# =============================================================================
# 0) Logging
# =============================================================================

LOGGER = logging.getLogger("azib_v21")

def setup_logging(level: str = "INFO", log_file: Optional[str] = None):
    """Configure logging for both console and optional file."""
    fmt = logging.Formatter("[%(levelname)s] %(asctime)s - %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers = []

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    if log_file:
        fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


# =============================================================================
# 1) Constants & Triage Output Schema
# =============================================================================

TRIAGE_VALUES = ("keep", "drop", "unsure")
YES_NO_UNSURE = ("yes", "no", "unsure")

@dataclass
class TriageResult:
    """Paper-level triage result."""
    file: str
    triage: str = "unsure"
    is_aqueous_zinc_battery: str = "unsure"
    has_zn_metal_anode: str = "unsure"
    ex_situ_protective_layer: str = "unsure"
    in_situ_only: str = "unsure"
    lab_scale_data: str = "unsure"
    confidence: float = 0.0
    summary: str = ""
    evidence: List[Dict[str, str]] = field(default_factory=list)
    top_blocks_used: int = 0
    notes: str = ""
    llm_calls: int = 0
    rule_triage: str = "unsure"
    rule_reason: str = ""
    warnings: List[str] = field(default_factory=list)
    block_scores: List[float] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)
    
    def to_csv_row(self) -> List[Any]:
        return [
            self.file,
            self.triage,
            self.is_aqueous_zinc_battery,
            self.has_zn_metal_anode,
            self.ex_situ_protective_layer,
            self.in_situ_only,
            self.lab_scale_data,
            f"{self.confidence:.2f}",
            self.summary,
            self.top_blocks_used,
            self.rule_triage,
            self.rule_reason,
            self.llm_calls,
            json.dumps(self.evidence, ensure_ascii=False) if self.evidence else "",
            self.notes,
            json.dumps(self.warnings, ensure_ascii=False) if self.warnings else "",
        ]


# =============================================================================
# 2) Text Preprocessing
# =============================================================================

INVISIBLE_CHARS = ["\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"]
WEIRD_SPACES = ["\u00a0", "\u202f", "\u2007", "\u2009", "\u200a"]

def normalize_invisibles(s: str) -> str:
    if not s:
        return s
    for c in INVISIBLE_CHARS:
        s = s.replace(c, "")
    for c in WEIRD_SPACES:
        s = s.replace(c, " ")
    return s

def strip_html_tags(text: str) -> str:
    """Aggressively removes HTML tags."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", text)

FENCED_CODE_RE = re.compile(r"(?s)(```.*?```|~~~.*?~~~)")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
LINK_MD_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")

def pre_sanitize_raw(raw: str) -> str:
    """STEP 1: Basic sanitation of raw MD text (preserves line structure)."""
    raw = normalize_invisibles(raw)
    text = FENCED_CODE_RE.sub("\n", raw)
    text = INLINE_CODE_RE.sub("", text)
    text = strip_html_tags(text)
    text = LINK_MD_RE.sub(r"\1", text)
    return text


# =============================================================================
# 3) Block Extraction (Heading-based + Priority Sections)
# =============================================================================

@dataclass
class TextBlock:
    """A block of text from the document."""
    heading: str
    heading_raw: str
    text: str
    start_line: int = 0
    end_line: int = 0
    word_count: int = 0
    score: float = 0.0
    section_type: str = "other"  # methods, experimental, other
    positive_hits: List[str] = field(default_factory=list)
    negative_hits: List[str] = field(default_factory=list)


# Heading detection patterns
ATX_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+)$")
S_SECTION_RE = re.compile(r"(?i)^\s*\*{0,3}\s*S\s*(\d+)\s*[.:)]*\s*(.*)$")
NUM_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[.)]\s+(.+)$")
EMPH_HEADING_RE = re.compile(r"^\s*(\*{2,3}|_{2,3})([^*_]+)\1\s*$")

# Priority section patterns (for block scoring boost)
METHODS_HEADING_RE = re.compile(
    r"(?ix)\b("
    r"method(s|ology)?|experimental|procedure|preparation|fabrication|"
    r"materials?\s+and\s+method|synthesis|electrode\s+preparation|"
    r"cell\s+assembly|electrochemical\s+(test|measurement)"
    r")\b"
)
CHARACTERIZATION_HEADING_RE = re.compile(
    r"(?ix)\b("
    r"characterization|structural\s+analysis|morpholog(y|ical)?\s+analysis|"
    r"xrd|xps|sem|tem|raman|ftir|afm"
    r")\b"
)


def extract_heading_from_line(line: str) -> Optional[Tuple[str, str, str]]:
    """Extract heading (norm, raw, kind) from line. Returns None if not a heading."""
    s = line.strip()
    if not s or len(s) < 3 or len(s) > 250:
        return None
    
    # ATX heading: # Title
    m = ATX_HEADING_RE.match(line)
    if m:
        return (m.group(2).strip(), s, "ATX")
    
    # S-section: S1, S 2, etc.
    m = S_SECTION_RE.match(s)
    if m:
        norm = f"S{m.group(1)} {m.group(2)}".strip()
        return (norm, s, "S")
    
    # Numeric heading: 1. Title, 2.1 Title
    m = NUM_HEADING_RE.match(s)
    if m:
        return (m.group(2).strip(), s, "NUM")
    
    # Emphasis heading: **Title**
    m = EMPH_HEADING_RE.match(s)
    if m:
        return (m.group(2).strip(), s, "EMPH")
    
    return None


def classify_section_type(heading: str) -> str:
    """Classify heading into section type for priority scoring."""
    if METHODS_HEADING_RE.search(heading):
        return "methods"
    if CHARACTERIZATION_HEADING_RE.search(heading):
        return "characterization"
    return "other"


def extract_blocks(lines: List[str], min_words: int = 30, max_words: int = 1200) -> List[TextBlock]:
    """Extract text blocks from lines, using headings as delimiters."""
    blocks: List[TextBlock] = []
    
    # Find all headings
    heading_indices: List[Tuple[int, str, str, str]] = []
    for i, ln in enumerate(lines):
        h = extract_heading_from_line(ln)
        if h:
            norm, raw, kind = h
            heading_indices.append((i, norm, raw, kind))
    
    if not heading_indices:
        # No headings - treat entire document as one block
        full_text = "\n".join(lines)
        wc = len(full_text.split())
        if wc >= min_words:
            blocks.append(TextBlock(
                heading="(no heading)",
                heading_raw="",
                text=full_text[:10000],  # cap size
                start_line=0,
                end_line=len(lines),
                word_count=wc,
                section_type="other",
            ))
        return blocks
    
    # Extract blocks between headings
    for j, (idx, norm, raw, kind) in enumerate(heading_indices):
        # Next heading index
        if j + 1 < len(heading_indices):
            end_idx = heading_indices[j + 1][0]
        else:
            end_idx = len(lines)
        
        # Extract text (skip heading line itself)
        body_lines = lines[idx + 1:end_idx]
        text = "\n".join(body_lines).strip()
        wc = len(text.split())
        
        if wc < min_words:
            continue
        if wc > max_words:
            # Truncate to max_words (roughly)
            text = " ".join(text.split()[:max_words])
            wc = max_words
        
        section_type = classify_section_type(norm)
        
        blocks.append(TextBlock(
            heading=norm,
            heading_raw=raw,
            text=text,
            start_line=idx,
            end_line=end_idx,
            word_count=wc,
            section_type=section_type,
        ))
    
    return blocks


# =============================================================================
# 4) Block Scoring (Query-based ranking - IMPROVED)
# =============================================================================

# Positive queries: Zn anode preparation & ex-situ coating
POSITIVE_ZN_ANODE_RE = re.compile(
    r"(?ix)\b("
    r"zn\s*(foil|plate|anode|electrode|metal)|zinc\s*(foil|plate|anode)|"
    r"polished|cleaned|etched|pretreated|pre[-\s]?treated|"
    r"pristine\s+zn|bare\s+zn"
    r")\b"
)

POSITIVE_EX_SITU_RE = re.compile(
    r"(?ix)\b("
    r"immers(ed|ing)|soak(ed|ing)|dip(ped|ping)|"
    r"drop[-\s]?cast|spin[-\s]?coat|spray(ed|ing)?|doctor[-\s]?blade|"
    r"electrodeposit(ed|ion|ing)?|electroplat(ed|ing)?|"
    r"pre[-\s]?form(ed|ing)?|infiltrat(ed|ing)?|"
    r"coat(ed|ing)?\s+(with|on|onto)|protect(ed|ive)?\s+layer|"
    r"artificial\s+sei|artificial\s+interface|"
    r"mof\s+coat|zif\s+coat|polymer\s+coat"
    r")\b"
)

POSITIVE_LAB_SCALE_RE = re.compile(
    r"(?ix)\b("
    r"coin\s*cell|cr2032|cr2016|swagelok|pouch\s*cell|"
    r"symmetric\s*cell|full\s*cell|half\s*cell|"
    r"galvanostatic|plating[-/\s]?stripping|"
    r"coulombic\s+efficiency|ce\s+of|cycling|"
    r"current\s+density|"
    r"areal\s+capacity"
    r")\b"
)

# Current density / capacity patterns (careful not to match dimensions)
CURRENT_DENSITY_RE = re.compile(
    r"(?ix)\b(\d+(?:\.\d+)?)\s*(mA\s*cm[−-]?\s*[2²]|mA/cm[2²]|A\s*g[−-]?\s*1|mAh\s*g[−-]?\s*1|mAh/g)\b"
)

# FIXED: ELECTROLYTE_RE - removed mm (dimension misclassification)
ELECTROLYTE_RE = re.compile(
    r"(?ix)\b("
    r"electrolyte|aqueous\s+solution|additive|buffer"
    r")\b"
    r"|"
    r"\b("
    r"znso4|zn\(so4\)2|zn\(cf3so3\)2|zn\(tfs?i\)2|zncl2|zn\(no3\)2|"
    r"zinc\s+sulfate|zinc\s+triflate|zinc\s+chloride|"
    r"cf3so3|tfs?i"
    r")\b"
    r"|"
    r"\b\d+(?:\.\d+)?\s*(?:mM|M|mol\s*[lL][−-]?1|wt%|vol%)\b"
)

# Negative queries: Characterization-only blocks
NEGATIVE_CHAR_ONLY_RE = re.compile(
    r"(?ix)\b("
    r"sem|tem|xrd|xps|raman|ftir|afm|eds|eels|"
    r"morpholog(y|ical)|structur(e|al)\s+characterization|"
    r"spectroscop(y|ic)|diffraction|microscop(y|ic)"
    r")\b"
)

# Simulation/DFT only (should drop)
SIMULATION_ONLY_RE = re.compile(
    r"(?ix)\b("
    r"dft|density\s+functional|first[-\s]?principles?|"
    r"md\s+simulation|molecular\s+dynamics|comsol|"
    r"vasp|gaussian|computational"
    r")\b"
)

# Review/non-experimental indicators
REVIEW_INDICATORS_RE = re.compile(
    r"(?ix)\b("
    r"review|perspective|progress\s+report|"
    r"summarized?\s+(in|by)|reported\s+by|"
    r"previous\s+stud(y|ies)"
    r")\b"
)

# Literature comparison table patterns (drop these blocks)
LIT_TABLE_CUE_RE = re.compile(
    r"(?ix)\b("
    r"discharge\s+capacity|cycle\s+performance|capacity\s+retention|"
    r"ref\.|this\s+work|coulombic\s+efficiency|"
    r"reported\s+value|literature\s+value"
    r")\b"
)
CITATION_ONLY_LINE_RE = re.compile(r"^\s*\[\s*\d+\s*\]\s*$")
PERF_UNIT_RE = re.compile(r"(?ix)\b(mAh|A\s*g[−-]1|mA\s*cm[−-]2|cycles?|mAh\s*g[−-]1)\b")


def looks_like_literature_comparison_table(block_text: str) -> bool:
    """Detect literature comparison tables (should be dropped)."""
    lines = [ln.strip() for ln in block_text.splitlines() if ln.strip()]
    if len(lines) < 8:
        return False
    
    cue = sum(1 for ln in lines if LIT_TABLE_CUE_RE.search(ln))
    cite_only = sum(1 for ln in lines if CITATION_ONLY_LINE_RE.match(ln))
    perf = sum(1 for ln in lines if PERF_UNIT_RE.search(ln))
    short = sum(1 for ln in lines if len(ln) <= 50)
    
    # Vertical table characteristics: many short lines, performance units/Ref repeated
    if cue >= 2 and (perf >= 4 or cite_only >= 2) and (short / len(lines) >= 0.50):
        return True
    
    # Also check for excessive performance units without recipe context
    if perf >= 10 and not POSITIVE_EX_SITU_RE.search(block_text):
        return True
    
    return False


def score_block(block: TextBlock) -> float:
    """Score a block based on query matches. Higher = more relevant."""
    text = block.text
    heading = block.heading
    score = 0.0
    pos_hits = []
    neg_hits = []
    
    # Section type boost
    if block.section_type == "methods":
        score += 3.0
        pos_hits.append("section:methods")
    elif block.section_type == "characterization":
        score -= 1.0
        neg_hits.append("section:char")
    
    # Positive signals
    zn_hits = POSITIVE_ZN_ANODE_RE.findall(text)
    if zn_hits:
        score += 2.5 * min(len(zn_hits), 3)
        pos_hits.append(f"Zn_anode:{len(zn_hits)}")
    
    ex_situ_hits = POSITIVE_EX_SITU_RE.findall(text)
    if ex_situ_hits:
        score += 4.0 * min(len(ex_situ_hits), 4)  # High weight for ex-situ
        pos_hits.append(f"ex_situ:{len(ex_situ_hits)}")
    
    lab_hits = POSITIVE_LAB_SCALE_RE.findall(text)
    if lab_hits:
        score += 2.0 * min(len(lab_hits), 4)
        pos_hits.append(f"lab_scale:{len(lab_hits)}")
    
    # Current density / capacity (good signal if with Zn context)
    current_hits = CURRENT_DENSITY_RE.findall(text)
    if current_hits and zn_hits:
        score += 1.5 * min(len(current_hits), 3)
        pos_hits.append(f"current_density:{len(current_hits)}")
    
    # Electrolyte mentions (good signal for AZIB)
    electrolyte_hits = ELECTROLYTE_RE.findall(text)
    if electrolyte_hits:
        score += 1.0 * min(len(electrolyte_hits), 2)
        pos_hits.append(f"electrolyte:{len(electrolyte_hits)}")
    
    # Negative signals (reduce score)
    char_hits = NEGATIVE_CHAR_ONLY_RE.findall(text)
    if char_hits:
        # Only penalize if characterization dominates
        if len(char_hits) >= 5 and not (zn_hits or ex_situ_hits):
            score -= 5.0
            neg_hits.append(f"char_only:{len(char_hits)}")
        elif len(char_hits) >= 3:
            score -= 2.0
            neg_hits.append(f"char:{len(char_hits)}")
    
    # Simulation/DFT only -> heavy penalty
    sim_hits = SIMULATION_ONLY_RE.findall(text)
    if sim_hits and not lab_hits:
        score -= 4.0
        neg_hits.append(f"sim_only:{len(sim_hits)}")
    
    # Review indicators
    review_hits = REVIEW_INDICATORS_RE.findall(text)
    if review_hits and len(review_hits) >= 3:
        score -= 3.0
        neg_hits.append(f"review:{len(review_hits)}")
    
    # Literature table -> heavy penalty
    if looks_like_literature_comparison_table(text):
        score -= 12.0
        neg_hits.append("lit_table")
    
    block.score = score
    block.positive_hits = pos_hits
    block.negative_hits = neg_hits
    
    return score


def select_top_k_blocks(blocks: List[TextBlock], k: int = 5) -> List[TextBlock]:
    """Select top K blocks by score."""
    for b in blocks:
        score_block(b)
    
    # Filter out very negative scores
    candidates = [b for b in blocks if b.score > -5.0]
    
    # Sort by score descending
    candidates.sort(key=lambda b: b.score, reverse=True)
    
    return candidates[:k]


# =============================================================================
# 5) Title & Abstract Extraction
# =============================================================================

def extract_title_and_abstract(lines: List[str]) -> Tuple[str, str]:
    """Extract title and abstract from document."""
    title = ""
    abstract = ""
    
    # Title is often the first non-empty line (or after ATX heading)
    for i, ln in enumerate(lines[:30]):
        s = ln.strip()
        if s and len(s) > 10:
            # Skip if it's a heading marker only
            if re.match(r"^#{1,6}\s*$", s):
                continue
            # Remove markdown ATX
            if s.startswith("#"):
                s = re.sub(r"^#+\s*", "", s)
            # Remove markdown emphasis
            s = re.sub(r"[*_]+", "", s).strip()
            if 15 < len(s) < 350:
                title = s
                break
    
    # Abstract
    abstract_start = -1
    for i, ln in enumerate(lines[:100]):
        if re.match(r"(?i)^\s*(#{1,3}\s*)?abstract\b", ln):
            abstract_start = i + 1
            break
    
    if abstract_start > 0:
        abstract_lines = []
        for ln in lines[abstract_start:abstract_start + 25]:
            s = ln.strip()
            if not s:
                if abstract_lines:
                    break
                continue
            if re.match(r"(?i)^\s*(#{1,3}\s*)?(keywords?|introduction|1\s*\.|\*\*1\.)", s):
                break
            abstract_lines.append(s)
        abstract = " ".join(abstract_lines)[:2500]
    
    return title, abstract


# =============================================================================
# 6) Rule-Based Pre-Filter (Stage 1)
# =============================================================================

# Quick AZIB detection
AZIB_QUICK_RE = re.compile(
    r"(?ix)\b("
    r"aqueous\s+zinc|zn\s*-?\s*ion\s+battery|"
    r"zinc\s*-?\s*ion\s+battery|azib|"
    r"zn\s+anode|zinc\s+anode|"
    r"zn\s+metal\s+(anode|battery)|"
    r"rechargeable\s+zn|rechargeable\s+zinc"
    r")\b"
)

# Non-aqueous / wrong battery type (drop signals)
WRONG_BATTERY_RE = re.compile(
    r"(?ix)\b("
    r"lithium|li\s*-?\s*ion|sodium|na\s*-?\s*ion|potassium|k\s*-?\s*ion|"
    r"solid\s+electrolyte|all\s*-?\s*solid|"
    r"non\s*-?\s*aqueous|organic\s+electrolyte|"
    r"aprotic\s+electrolyte"
    r")\b"
)

# Ex-situ layer quick detection
EX_SITU_QUICK_RE = re.compile(
    r"(?ix)\b("
    r"protect(ed|ive|ion)?\s+(layer|coating|film)|"
    r"artificial\s+(sei|interface|layer)|"
    r"coat(ed|ing)?\s+(zn|zinc)|"
    r"(zn|zinc)\s+coat(ed|ing)|"
    r"surface\s+(modif|treat|coat)|"
    r"pre[-\s]?treat(ed|ment)?|"
    r"deposit(ed|ion)?\s+(on|onto)\s+(zn|zinc)"
    r")\b"
)

# In-situ only indicators
IN_SITU_ONLY_RE = re.compile(
    r"(?ix)\b("
    r"in[-\s]?situ\s+(form|generat|creat)|"
    r"self[-\s]?heal|spontaneous(ly)?\s+(form|generat)|"
    r"natural(ly)?\s+(form|generat)|"
    r"during\s+cycling|upon\s+cycling"
    r")\b"
)


def rule_based_triage(
    title: str, 
    abstract: str, 
    blocks: List[TextBlock],
    top_blocks: List[TextBlock]
) -> Tuple[str, str, float]:
    """
    Stage 1: Rule-based pre-filter before LLM.
    Returns: (triage, reason, confidence)
    """
    all_text = f"{title}\n{abstract}\n" + "\n".join(b.text for b in blocks[:10])
    
    # Quick checks
    azib_match = AZIB_QUICK_RE.search(all_text)
    wrong_battery_match = WRONG_BATTERY_RE.search(all_text)
    ex_situ_match = EX_SITU_QUICK_RE.search(all_text)
    in_situ_only_match = IN_SITU_ONLY_RE.search(all_text)
    
    # Total positive score from top blocks
    total_score = sum(b.score for b in top_blocks)
    
    # FAST DROP cases
    if not azib_match:
        # Likely not AZIB at all
        return ("drop", "no_azib_keywords", 0.7)
    
    if wrong_battery_match and not azib_match:
        return ("drop", "wrong_battery_type", 0.8)
    
    # Check if simulation/review only (no experimental at all)
    sim_count = len(SIMULATION_ONLY_RE.findall(all_text))
    lab_count = len(POSITIVE_LAB_SCALE_RE.findall(all_text))
    if sim_count >= 5 and lab_count < 2:
        return ("drop", "simulation_only", 0.65)
    
    # FAST KEEP cases
    if azib_match and ex_situ_match and total_score >= 12.0:
        return ("keep", f"strong_ex_situ_signal(score={total_score:.1f})", 0.75)
    
    # In-situ only check
    if in_situ_only_match and not ex_situ_match:
        return ("drop", "in_situ_only_no_ex_situ", 0.6)
    
    # Low score -> drop
    if total_score < 2.0:
        return ("drop", f"low_relevance_score({total_score:.1f})", 0.6)
    
    # Medium signals -> unsure (needs LLM)
    return ("unsure", f"mixed_signals(score={total_score:.1f})", 0.5)


# =============================================================================
# 7) LLM Client (from v20)
# =============================================================================

@dataclass
class LLMConfig:
    backend: str = "none"
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen2.5:14b-instruct"
    timeout_s: int = 180
    temperature: float = 0.0
    top_p: float = 0.9
    num_ctx: int = 12000
    seed: int = 42
    retry: int = 2
    retry_backoff_s: float = 1.5


class OllamaClient:
    """Minimal Ollama HTTP client."""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.base = cfg.ollama_url.rstrip("/")

    def _post_json(self, path: str, payload: Dict[str, Any]) -> str:
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
                return resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama request failed: {e}")

    def chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.cfg.temperature,
                "top_p": self.cfg.top_p,
                "num_ctx": self.cfg.num_ctx,
                "seed": self.cfg.seed,
            },
        }
        raw = self._post_json("/api/chat", payload)
        obj = json.loads(raw)
        return obj.get("message", {}).get("content", "")


# =============================================================================
# 8) LLM Triage Prompt (IMPROVED - stronger ex-situ vs in-situ distinction)
# =============================================================================

LLM_SYSTEM_TRIAGE = """You are a scientific paper screening expert for aqueous zinc-ion battery (AZIB) research.
Your task: Determine if a paper is relevant for extracting Zn anode ex-situ protective layer fabrication RECIPES.

CRITICAL DEFINITIONS (READ CAREFULLY):

1. "ex-situ protective layer" (WHAT WE WANT):
   - A layer/coating/film applied to Zn anode BEFORE electrochemical cycling begins
   - Fabrication methods: dip-coating, drop-casting, electrodeposition, spin-coating, spray-coating, doctor-blade, immersion, etc.
   - The layer is PRE-FORMED before the cell is assembled and cycled
   - Examples: MOF coating, polymer coating, ZnO layer, artificial SEI, etc.
   - RECIPE indicators: temperature, time, concentration, drying conditions, etc.

2. "in-situ layer" (NOT our target):
   - A layer formed DURING electrochemical cycling (after cell assembly)
   - Terms like: "upon cycling", "during cycling", "self-forming", "spontaneously formed"
   - These papers describe layers that form naturally during battery operation
   - EVEN IF they mention "SEI" or "protective layer", if it's formed in-situ, it's NOT our target

3. "lab-scale experimental data" (REQUIRED):
   - Actual electrochemical experiments: cycling tests, CE measurements, EIS, plating/stripping
   - Cell types: coin cell, CR2032, Swagelok, symmetric cell, full cell
   - NOT just simulations, DFT calculations, or literature reviews

TRIAGE DECISION RULES:

KEEP if ALL of the following are true:
  - Aqueous zinc battery (uses water-based electrolyte)
  - Zn metal anode (not Zn oxide cathode)
  - Contains ex-situ protective layer fabrication RECIPE (dip time, temperature, concentration, etc.)
  - Has lab-scale electrochemical data

DROP if ANY of the following are true:
  - Not aqueous zinc battery (Li-ion, Na-ion, solid-state, non-aqueous)
  - Only describes in-situ layer formation (no pre-fabrication recipe)
  - Only simulation/DFT/computational study
  - Review paper without original experimental data
  - Zn is cathode material, not anode

UNSURE if:
  - Mixed signals or incomplete information
  - Partially relevant but unclear on ex-situ vs in-situ

OUTPUT STRICT JSON ONLY (no markdown fences, no explanation outside JSON):
{
  "triage": "keep|drop|unsure",
  "is_aqueous_zinc_battery": "yes|no|unsure",
  "has_zn_metal_anode": "yes|no|unsure",
  "ex_situ_protective_layer": "yes|no|unsure",
  "in_situ_only": "yes|no|unsure",
  "lab_scale_data": "yes|no|unsure",
  "confidence": 0.0-1.0,
  "summary": "one line summary of paper relevance",
  "evidence": [
    {"heading": "section name", "snippet": "key quote showing recipe or electrochemistry (short)"}
  ],
  "notes": "any uncertainty or mixed signals"
}"""


def build_triage_prompt(title: str, abstract: str, blocks: List[TextBlock]) -> str:
    """Build the user prompt for triage."""
    parts = []
    parts.append(f"TITLE: {title or '(not found)'}")
    parts.append(f"\nABSTRACT:\n{abstract[:2000] if abstract else '(not found)'}")
    
    parts.append("\n\n=== TOP EVIDENCE BLOCKS (from Methods/Experimental sections) ===")
    for i, b in enumerate(blocks):
        parts.append(f"\n[Block {i+1}] Heading: {b.heading} | Score: {b.score:.1f} | Type: {b.section_type}")
        if b.positive_hits:
            parts.append(f"  Positive signals: {', '.join(b.positive_hits)}")
        # Truncate block text
        text = b.text[:2500] if len(b.text) > 2500 else b.text
        parts.append(text)
    
    parts.append("\n\n=== END BLOCKS ===")
    parts.append("\nBased on the above information, provide your triage judgment as JSON.")
    parts.append("Remember: We need EX-SITU (pre-formed before cycling) protective layer RECIPES, not in-situ formation.")
    
    return "\n".join(parts)


def parse_triage_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse triage JSON from LLM output."""
    text = text.strip()
    # Remove markdown fences if present
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    text = text.strip()
    
    # Find JSON
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


# =============================================================================
# 9) Main Analyzer
# =============================================================================

@dataclass
class AnalyzerConfig:
    top_k: int = 5
    min_block_words: int = 30
    max_block_words: int = 1200
    max_chars_per_doc: int = 500_000
    skip_llm_for_confident_rule: bool = True  # Skip LLM if rule-based is confident
    rule_confidence_threshold: float = 0.70     # Skip LLM if rule confidence >= this


class TriageAnalyzer:
    """Paper-level triage analyzer with 2-stage filtering."""
    
    def __init__(self, acfg: AnalyzerConfig, lcfg: LLMConfig):
        self.acfg = acfg
        self.lcfg = lcfg
        self.client = OllamaClient(lcfg) if lcfg.backend != "none" else None
        self.llm_calls = 0
    
    def _read_md(self, path: Path) -> Optional[str]:
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            LOGGER.error(f"Failed to read {path}: {e}")
            return None
        if raw and len(raw) > self.acfg.max_chars_per_doc:
            raw = raw[:self.acfg.max_chars_per_doc]
        return raw
    
    def analyze(self, md_path: Path, base_dir: Path) -> TriageResult:
        """Analyze a single paper and return triage result."""
        rel = str(md_path.relative_to(base_dir))
        result = TriageResult(file=rel)
        
        raw = self._read_md(md_path)
        if raw is None:
            result.warnings.append("read_error")
            result.triage = "drop"
            result.rule_triage = "drop"
            result.rule_reason = "read_error"
            return result
        
        # Preprocess
        text = pre_sanitize_raw(raw)
        lines = text.splitlines()
        
        # Extract title and abstract
        title, abstract = extract_title_and_abstract(lines)
        
        # Extract blocks
        blocks = extract_blocks(
            lines,
            min_words=self.acfg.min_block_words,
            max_words=self.acfg.max_block_words
        )
        
        if not blocks:
            result.warnings.append("no_blocks")
            result.triage = "unsure"
            result.rule_triage = "unsure"
            result.rule_reason = "no_blocks_extracted"
            result.notes = "Could not extract any blocks from document"
            return result
        
        # Score and select top-K
        top_blocks = select_top_k_blocks(blocks, k=self.acfg.top_k)
        result.top_blocks_used = len(top_blocks)
        result.block_scores = [b.score for b in top_blocks]
        
        if not top_blocks:
            result.warnings.append("no_relevant_blocks")
            result.triage = "drop"
            result.rule_triage = "drop"
            result.rule_reason = "no_relevant_blocks"
            result.notes = "No relevant blocks found"
            return result
        
        # Stage 1: Rule-based pre-filter
        rule_triage, rule_reason, rule_conf = rule_based_triage(title, abstract, blocks, top_blocks)
        result.rule_triage = rule_triage
        result.rule_reason = rule_reason
        
        # Decide if we need LLM
        if self.acfg.skip_llm_for_confident_rule and rule_conf >= self.acfg.rule_confidence_threshold:
            # Trust rule-based decision
            result.triage = rule_triage
            result.confidence = rule_conf
            result.notes = f"Rule-based decision: {rule_reason}"
            return result
        
        # Stage 2: LLM triage (for unsure cases or when rule confidence is low)
        if self.client and rule_triage == "unsure":
            prompt = build_triage_prompt(title, abstract, top_blocks)
            
            for attempt in range(self.lcfg.retry + 1):
                try:
                    self.llm_calls += 1
                    response = self.client.chat(LLM_SYSTEM_TRIAGE, prompt)
                    parsed = parse_triage_json(response)
                    
                    if parsed:
                        result.triage = parsed.get("triage", "unsure")
                        result.is_aqueous_zinc_battery = parsed.get("is_aqueous_zinc_battery", "unsure")
                        result.has_zn_metal_anode = parsed.get("has_zn_metal_anode", "unsure")
                        result.ex_situ_protective_layer = parsed.get("ex_situ_protective_layer", "unsure")
                        result.in_situ_only = parsed.get("in_situ_only", "unsure")
                        result.lab_scale_data = parsed.get("lab_scale_data", "unsure")
                        result.confidence = float(parsed.get("confidence", 0.5))
                        result.summary = parsed.get("summary", "")
                        result.evidence = parsed.get("evidence", [])
                        result.notes = parsed.get("notes", "")
                        result.llm_calls = 1
                        break
                    else:
                        LOGGER.warning(f"Failed to parse LLM response for {rel}")
                        
                except Exception as e:
                    LOGGER.error(f"LLM error for {rel}: {e}")
                    if attempt < self.lcfg.retry:
                        time.sleep(self.lcfg.retry_backoff_s * (attempt + 1))
                        continue
                    result.warnings.append("llm_error")
            
            # If LLM failed, fall back to rule-based
            if result.llm_calls == 0:
                result.triage = rule_triage
                result.confidence = rule_conf
        else:
            # No LLM available or rule already decided
            result.triage = rule_triage
            result.confidence = rule_conf
            if not result.notes:
                result.notes = f"Rule-based decision: {rule_reason}"
        
        return result


# =============================================================================
# 10) Pipeline
# =============================================================================

def run_pipeline(
    input_dir: Path,
    output_json: Path,
    output_csv: Optional[Path],
    acfg: AnalyzerConfig,
    lcfg: LLMConfig,
    sample_n: int = 0,
):
    """Run triage pipeline on all markdown files."""
    md_files = sorted(input_dir.rglob("*.md"))
    
    if sample_n > 0 and len(md_files) > sample_n:
        random.seed(42)
        md_files = random.sample(md_files, sample_n)
    
    LOGGER.info(f"Processing {len(md_files)} files from {input_dir}")
    LOGGER.info(f"LLM backend: {lcfg.backend} | Model: {lcfg.model}")
    LOGGER.info(f"Top-K blocks: {acfg.top_k} | Skip LLM for confident rules: {acfg.skip_llm_for_confident_rule}")
    
    analyzer = TriageAnalyzer(acfg, lcfg)
    results: List[TriageResult] = []
    
    iterator = tqdm(md_files, desc="Triage") if tqdm else md_files
    
    for md_path in iterator:
        result = analyzer.analyze(md_path, input_dir)
        results.append(result)
        
        tag = result.triage.upper()
        rule_tag = f"[R:{result.rule_triage}]" if result.rule_triage else ""
        LOGGER.debug(f"[{tag}]{rule_tag} {result.file} | conf={result.confidence:.2f} | {result.summary[:50] if result.summary else ''}")
    
    # Write JSON output
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in results], f, ensure_ascii=False, indent=2)
    LOGGER.info(f"Wrote {output_json}")
    
    # Write CSV output
    if output_csv:
        with open(output_csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "File", "Triage", "AqueousZn", "ZnMetalAnode", "ExSituLayer",
                "InSituOnly", "LabScaleData", "Confidence", "Summary",
                "TopBlocks", "RuleTriage", "RuleReason", "LLMCalls", "Evidence", "Notes", "Warnings"
            ])
            for r in results:
                writer.writerow(r.to_csv_row())
        LOGGER.info(f"Wrote {output_csv}")
    
    # Summary
    keep_count = sum(1 for r in results if r.triage == "keep")
    drop_count = sum(1 for r in results if r.triage == "drop")
    unsure_count = sum(1 for r in results if r.triage == "unsure")
    
    LOGGER.info(f"=== TRIAGE SUMMARY ===")
    LOGGER.info(f"KEEP: {keep_count} | DROP: {drop_count} | UNSURE: {unsure_count}")
    LOGGER.info(f"Total LLM calls: {analyzer.llm_calls}")
    
    # Rule-based stats
    rule_keep = sum(1 for r in results if r.rule_triage == "keep")
    rule_drop = sum(1 for r in results if r.rule_triage == "drop")
    rule_unsure = sum(1 for r in results if r.rule_triage == "unsure")
    LOGGER.info(f"Rule-based: KEEP={rule_keep} | DROP={rule_drop} | UNSURE={rule_unsure}")


# =============================================================================
# 11) CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="AZIB Paper-Level Triage v21.1 - Two-stage filter for ex-situ Zn anode protection papers",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    # I/O
    parser.add_argument("--input_dir", type=str, required=True, help="Input directory with .md files")
    parser.add_argument("--output_json", type=str, default="out_v21_triage.json", help="Output JSON file")
    parser.add_argument("--output_csv", type=str, default="", help="Optional output CSV file")
    
    # Analysis config
    parser.add_argument("--top_k", type=int, default=5, help="Top K blocks to use for triage")
    parser.add_argument("--min_block_words", type=int, default=30, help="Minimum words per block")
    parser.add_argument("--max_block_words", type=int, default=1200, help="Maximum words per block")
    parser.add_argument("--sample_n", type=int, default=0, help="Process random sample of N files (0=all)")
    parser.add_argument("--skip_llm_for_confident_rule", action="store_true", default=True, 
                        help="Skip LLM if rule-based filter is confident")
    parser.add_argument("--rule_confidence_threshold", type=float, default=0.70,
                        help="Confidence threshold to skip LLM")
    
    # LLM config
    parser.add_argument("--llm_backend", type=str, default="none", choices=["none", "ollama"], help="LLM backend")
    parser.add_argument("--ollama_url", type=str, default="http://localhost:11434", help="Ollama URL")
    parser.add_argument("--llm_model", type=str, default="qwen2.5:14b-instruct", help="LLM model")
    parser.add_argument("--llm_timeout", type=int, default=180, help="LLM timeout seconds")
    parser.add_argument("--llm_temperature", type=float, default=0.0, help="LLM temperature")
    parser.add_argument("--llm_num_ctx", type=int, default=12000, help="LLM context window")
    
    # Logging
    parser.add_argument("--log_level", type=str, default="INFO", help="Logging level")
    parser.add_argument("--log_file", type=str, default="", help="Optional log file")
    
    args = parser.parse_args()
    
    setup_logging(args.log_level, args.log_file or None)
    
    acfg = AnalyzerConfig(
        top_k=args.top_k,
        min_block_words=args.min_block_words,
        max_block_words=args.max_block_words,
        skip_llm_for_confident_rule=args.skip_llm_for_confident_rule,
        rule_confidence_threshold=args.rule_confidence_threshold,
    )
    
    lcfg = LLMConfig(
        backend=args.llm_backend,
        ollama_url=args.ollama_url,
        model=args.llm_model,
        timeout_s=args.llm_timeout,
        temperature=args.llm_temperature,
        num_ctx=args.llm_num_ctx,
    )
    
    run_pipeline(
        input_dir=Path(args.input_dir),
        output_json=Path(args.output_json),
        output_csv=Path(args.output_csv) if args.output_csv else None,
        acfg=acfg,
        lcfg=lcfg,
        sample_n=args.sample_n,
    )


if __name__ == "__main__":
    main()
