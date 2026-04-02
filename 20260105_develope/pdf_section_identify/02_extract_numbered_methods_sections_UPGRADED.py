# -*- coding: utf-8 -*-
"""
02_extract_numbered_methods_sections_UPGRADED_v8.py

AZIB Experimental Chunk Extractor (Recipe-first) — Commercial Rubric v1.0 aligned (v8)
====================================================================================

v8 핵심 철학: Recall 우선 + LLM 판정 지원
-----------------------------------------
"추출기는 최대한 안 놓치고(Recall↑), 최종 판정은 Title+Abstract+Chunks를 넣은 LLM이 하게 한다"

✅ Hard-drop은 "명백한 노이즈만" (표/캡션/수식정크)
✅ 나머지는 Soft-keep (낮은 confidence + noise_flags로 표시)
✅ LLM이 판단하기 쉬운 evidence_pack 구조 제공

v8 주요 기능
-----------
(1) Zn-metal vs Zn-salt 컨텍스트 분리: flags로 구분, conf 조절
(2) RECIPE_VERBS 2단계: 강한 동사 vs 약한 동사 (weak_verb flag)
(3) 하이브리드 헤딩: "Synthesis and characterization" → synth로 포함 + hybrid flag
(4) Formation purpose 배터리 컨텍스트 조건부 (activation_ambiguous flag)
(5) Time-axis signals: pre_cycling_flag, pre_assembly_flag
(6) Aqueous signals: aqueous_flag + evidence
(7) Dropped 문장 side-channel: dropped_result_snippets, dropped_echem_snippets
(8) Interlayer/Cathode evidence top-k 보존
(9) evidence_pack 구조화: 태그별 top-k + counts + flags
(10) Sentence features 저장: has_zn_metal, has_zn_salt, weak_verb 등
(11) Provenance 필드: method_section vs fallback 구분
(12) Lab-scale signals: coin cell, current density 등

Requirements
------------
pip install requests tqdm
"""

from __future__ import annotations

from tqdm import tqdm
import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Set

# Optional dependency (only needed when llm_backend=ollama)
try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore

# =============================================================================
# Version & Logging
# =============================================================================
EXTRACTOR_VERSION = "v9.0.0"  # v9: 12-issue fix based on result analysis
LOGGER = logging.getLogger("azib_chunk_extractor_v9")


def setup_logging(level: str = "INFO") -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# =============================================================================
# Rubric keyword sets
# =============================================================================

METHOD_MAIN_KEYWORDS = [
    "experimental", "experiment", "experiments",
    "materials and methods", "material and methods",
    "methods", "methodology",
    "experimental section", "experimental details",
    "experimental procedures", "experimental procedure",
]

SYNTH_MANUF_KEYWORDS = [
    # synthesis/fabrication
    "synthes", "prepar", "fabricat", "manufactur", "process",
    "treatment", "modification", "surface treatment",
    # coating/deposition
    "coat", "spin-coat", "dip-coat", "spray-coat", "drop-cast", "drop cast", "cast",
    "doctor blade", "blade coating",
    "electrodeposit", "electroplat", "electroplating", "plating",
    "deposit", "deposition",
    "grow", "growth", "in situ growth",
    "etch", "etching",
    "polish", "polishing",
    "anneal", "annealing", "calcina", "calcination",
    "dry", "drying", "cure", "curing",
    "polymeriz", "polymerization",
    "precipitat", "precipitation",
    "mix", "mixing", "stirr", "stirring",
    "assembly", "cell assembly", "electrode preparation",
    # electrolyte/formulation
    "electrolyte", "electrolyte formulation", "electrolyte preparation",
    "additive", "additives",
    # Korean
    "합성", "제조", "제작", "도포", "코팅", "전착", "증착", "성장", "조립",
]

WEAK_MATERIAL_HEADINGS = [
    "material", "materials",
    "chemical", "chemicals",
    "reagent", "reagents",
    "materials and reagents",
    "chemicals and reagents",
    "materials and chemicals",
]

SYNTH_MANUF_EXCLUDE_TERMS = [
    "mechanism", "behavior", "behaviour",
    "performance", "property", "properties",
    "results", "discussion", "analysis",
    "electrochemical performance",
    "electrochemical test", "electrochemical testing",
    "cycling performance", "rate capability",
    "simulation", "calculation",
]

# --- v7: layer anchors (Zn anchor 없이도 "보호층 재료/필름 제조"를 살리기 위한 신호)
LAYER_ANCHORS = [
    "asei", "artificial sei", "sei", "protective layer", "protection layer",
    "interphase", "interface layer", "coating layer", "coating", "film", "films",
    "membrane", "layer", "skin layer", "polymer layer", "surface layer",
    "pre-coated", "precoated", "pre-coated layer", "pretreated", "pre-treated",
    "cof film", "cof films", "cof-based", "polymer film",
]

# --- v7: interlayer/separator anchors
INTERLAYER_TERMS = [
    "interlayer", "separator", "glass fiber", "glass-fiber", "membrane separator",
    "celgard", "pp separator", "pe separator", "polypropylene separator",
    "separator membrane", "intermediate layer", "buffer layer on separator",
]

# --- v7: cathode anchors (강한 앵커 위주로)
CATHODE_STRONG_ANCHORS = [
    "cathode", "positive electrode", "positive cathode",
    "al foil", "aluminum foil", "aluminium foil",
    "current collector (al", "current collector: al",
    "nmp", "super p", "acetylene black", "pvdf binder", "cathode slurry",
    "doctor blade", "blade-coated", "slurry was coated", "cast on al foil",
    "active material", "loading mass", "areal loading",
]
CATHODE_MATERIAL_HINTS = [
    "mno2", "v2o5", "cvo", "nh4v4o10", "v6o13", "pani cathode", "polyaniline cathode",
    "vanadium oxide cathode", "manganese oxide cathode",
]

ZN_ANCHORS = [
    "zn anode", "zn foil", "zn electrode", "zinc anode",
    "zinc foil", "zinc electrode", "zinc metal", "zn plate", "zn sheet",
]

# ============================================================================
# v8: Zn-metal vs Zn-salt 컨텍스트 분리
# ============================================================================
ZN_METAL_CONTEXT = [
    "zn foil", "zn plate", "zn sheet", "zn anode", "zn electrode", "zinc foil",
    "zinc plate", "zinc anode", "zinc electrode", "zinc metal", "zn metal",
    "polished zn", "treated zn", "coated zn", "modified zn", "bare zn",
]
ZN_SALT_CONTEXT = [
    "znso4", "zn(otf)", "zn(tfsi)", "zncl2", "zn(no3)", "zn(cf3so3)",
    "zinc sulfate", "zinc triflate", "zinc chloride", "zn2+", "zn ions",
    "zn salt", "zinc salt", "aqueous zn",
]

# ============================================================================
# v8: Time-axis signals (pre-cycling evidence)
# ============================================================================
TIME_AXIS_PRE_SIGNALS = [
    "before cycling", "prior to cycling", "pre-cycling", "pre cycling",
    "before assembly", "prior to assembly", "before cell assembly",
    "pretreated", "pre-treated", "precoated", "pre-coated",
    "before electrochemical", "before battery",
]

# ============================================================================
# v8: Aqueous signals
# ============================================================================
AQUEOUS_SIGNALS = [
    "aqueous", "water-based", "water based", "h2o", "in water",
    "aqueous electrolyte", "aqueous solution", "aqueous znso4", "aqueous zn",
    "mild aqueous", "ph ", "ph=", "ph ", 
]

# ============================================================================
# v8: Lab-scale signals (vs review/simulation)
# ============================================================================
LAB_SCALE_SIGNALS = [
    "coin cell", "cr2032", "cr2016", "swagelok", "pouch cell",
    "current density", "ma cm", "ma/cm", "ma g", "mah g", "mah/g",
    "areal capacity", "mass loading", "areal loading", "active material loading",
    "coulombic efficiency", "ce ", "cycling stability", "cycle life",
    "charge-discharge", "galvanostatic", "assembled", "separator",
]

# ============================================================================
# v8: RECIPE_VERBS 2단계 분리 (Strong vs Weak)
# ============================================================================
# Strong verbs: 명확한 레시피 동사, conf 높게 인정
STRONG_RECIPE_VERBS = [
    # coating/deposition (명확)
    "coated", "coating", "spin-coated", "spin coating", "dip-coated", "dip coating",
    "spray-coated", "spraying", "drop-cast", "drop-casting", "blade-coated",
    "deposited", "deposition", "electrodeposited", "electrodeposition",
    "electroplated", "electroplating",
    # wet chemistry (명확한 과거분사/동명사)
    "dissolved", "dissolving", "dispersed", "dispersing",
    "poured", "pouring", "collected", "collecting",
    "transferred", "transferring", "evaporated", "evaporating",
    "immersed", "immersing", "soaked", "soaking",
    "washed", "washing", "rinsed", "rinsing",
    "centrifuged", "centrifuging", "filtered", "filtering",
    "stirred", "stirring", "sonicated", "sonication",
    "dried", "drying", "vacuum-dried", "vacuum drying",
    "annealed", "annealing", "cured", "curing", "heated", "heating",
    "polished", "polishing", "etched", "etching",
    "synthesized", "synthesised", "fabricated",
]

# Weak verbs: 명사로도 쓰일 수 있거나 애매함, conf 낮게 + weak_verb flag
WEAK_RECIPE_VERBS = [
    "coat", "cast", "spray", "deposit", "plate",  # 명사로도 쓰임
    "add", "added", "adding",  # 문맥에 따라 다름
    "remove", "removed", "removing",
    "place", "placed", "placing",
    "spread", "spreading",
    "dip", "dipped", "dipping",
    "filter",  # 명사 filter
    "stir", "mix", "mixed", "mixing",
    "dry", "heat", "age", "aged", "aging",
    "maintain", "maintained", "maintaining",
    "keep", "kept",
    "press", "pressed", "pressing",  # 명사 press
    "roll", "rolled", "rolling",
    "prepare", "prepared", "preparing",
    "modify", "modified", "modification",
    "synthesis", "fabrication",  # 명사
]

# 전체 RECIPE_VERBS (하위 호환)
RECIPE_VERBS = STRONG_RECIPE_VERBS + WEAK_RECIPE_VERBS

UNITS_PATTERNS = [
    r"\b\d+(\.\d+)?\s*(?:h|hr|hrs|hours|min|mins|minute|minutes|s|sec|secs|day|days)\b",
    r"\b\d+(\.\d+)?\s*°\s*C\b",
    r"\b\d+(\.\d+)?\s*(?:K)\b",
    r"\b\d+(\.\d+)?\s*(?:M|mM|wt%|mol%|vol%|v/v|w/v|mg\s*mL[-−]1|mg/mL|g\s*L[-−]1|g/L|mAh\s*g[-−]1|mAh/g)\b",
    r"\b\d+(\.\d+)?\s*(?:mg|g|kg|µg|ug|mL|ml|L|µL|uL|mmol|mol)\b",
    r"\b\d+(\.\d+)?\s*(?:mA|A)\s*cm[-−]2\b",
    r"\b\d+(\.\d+)?\s*(?:V|mV)\b",
    r"\b\d+(\.\d+)?\s*(?:rpm)\b",

]
UNITS_RE = re.compile("|".join(UNITS_PATTERNS), flags=re.IGNORECASE)

# v9: SI/ESI 단독 패턴은 주변 컨텍스트 필요 (supporting/supplementary/information 동반)
SUPP_POINTER_PATTERNS: List[Tuple[re.Pattern, float]] = [
    (re.compile(r"\bexperimental (?:details|procedure|procedures)\b.*\b(?:can be found|are provided|are given|are described)\b.*\b(?:supporting|supplementary)\b", re.IGNORECASE), 1.0),
    (re.compile(r"\b(?:details|procedure|procedures)\b.*\b(?:supporting|supplementary)\b.*\b(?:information|material|materials|data)\b", re.IGNORECASE), 0.9),
    (re.compile(r"\bsee\b.*\b(?:supporting|supplementary)\b", re.IGNORECASE), 0.7),
    (re.compile(r"\b(?:supporting|supplementary)\b\s*(?:information|material|materials|data)\b", re.IGNORECASE), 0.6),
    # v9: SI/ESI 단독은 supporting/supplementary/information 동반 시만 인정 (2-gram 조건)
    (re.compile(r"\b(?:esi|si)\b(?:.{0,30}\b(?:supporting|supplementary|information)\b|\b(?:supporting|supplementary|information)\b.{0,30})", re.IGNORECASE), 0.4),
]

# =============================================================================
# v7: aggressive noise patterns
# =============================================================================

# Numeric-only bracket citations: [12], [12,34], [12–14], [12-14]
CITATION_BRACKET_RE = re.compile(
    r"\[\s*\d+(?:\s*(?:,|;)\s*\d+|\s*[-–]\s*\d+)*\s*\]"
)

# Parenthetical numeric citations: (12), (12,34), (12–14)
CITATION_PAREN_RE = re.compile(
    r"\(\s*\d+(?:\s*(?:,|;)\s*\d+|\s*[-–]\s*\d+)*\s*\)"
)

# v7: linked citation like [[44,45]](#page-10-0) or [[47\]](#page...)
# (G) Allow optional backslash in the pattern
LINKED_CITATION_RE = re.compile(
    r"\[\[\s*[\d,\s;\u2013\-\\]+\s*\]\]\s*\([^)]+\)",
    flags=re.IGNORECASE
)

# Caption start lines (Fig/Table/Scheme/Eq...) - used for caption-block dropping
CAPTION_START_RE = re.compile(
    r"^\s*(?:fig(?:ure)?|table|scheme|eq(?:n)?|equation)\s*\.?\s*(?:s?\d+[a-z]?(?:\s*[-–]\s*\d+[a-z]?)?|\d+)\s*[:\.\)]?\s*",
    flags=re.IGNORECASE
)

# In-sentence fig/table refs (drop sentence if enabled)
FIGREF_SENT_RE = re.compile(
    r"\b(?:fig(?:ure)?|table|scheme|eq(?:n)?|equation)\s*\.?\s*(?:s?\d+[a-z]?(?:\s*[-–]\s*\d+[a-z]?)?|\d+)\b",
    flags=re.IGNORECASE
)

# v7: parenthetical fig/table refs 제거(문장 분리 전에 제거하기 위함)
PAREN_FIGREF_RE = re.compile(
    r"\((?:[^()]{0,120})\b(?:fig(?:ure)?|table|scheme|eq(?:n)?|equation)\b(?:[^()]{0,120})\)",
    flags=re.IGNORECASE
)

# Math/axis junk lines: mostly digits/operators with × or \times
MATH_JUNK_LINE_RE = re.compile(
    r"^\s*(?:\\times|×|\*|x)?\s*\d+(\.\d+)?(?:\s*(?:\\times|×|\*|x)\s*\d+(\.\d+)?)*\s*$",
    re.IGNORECASE
)

# =============================================================================
# v7: WORD-BOUNDARY regex for CHAR & ECHEM (오탐 제거 핵심)
# =============================================================================

CHAR_ABBR = [
    "SEM", "TEM", "HRTEM", "STEM",
    "XRD", "XPS", "FTIR", "AFM", "BET",
    "TGA", "DSC", "NMR", "GPC",
    "XANES", "EXAFS",
]
CHAR_PHRASES = [
    "contact angle", "zeta potential", "porosimetry", "uv-vis", "uv–vis", "raman", "gc-ms",
]
CHAR_ABBR_RE = re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, CHAR_ABBR)), flags=re.IGNORECASE)

ECHEM_ABBR = [
    "CV", "LSV", "EIS", "GCD", "CA", "CP",
]
ECHEM_PHRASES = [
    "cyclic voltammetry", "linear sweep", "electrochemical impedance",
    "galvanostatic", "chronoamperometry", "chronopotentiometry",
    "rate capability", "c-rate", "battery test system", "neware", "land",
]
ECHEM_ABBR_RE = re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, ECHEM_ABBR)), flags=re.IGNORECASE)


# (R) Extended RESULT_MARKERS with explanation/mechanism markers
RESULT_MARKERS = [
    "as shown in fig", "as shown in figure", "as shown in table",
    "see fig", "see figure", "see table",
    "figure shows", "fig shows", "table shows", "scheme shows",
    "demonstrates", "reveals", "indicates that", "suggests that",
    "we found", "it can be seen", "it is observed",
    # (R) Explanation/mechanism markers
    "rationale", "expected to", "is known to", "mechanism",
    "therefore", "thus", "attributed to", "can be explained",
    "is believed", "is thought", "might be", "could be",
]


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class Heading:
    raw: str
    level: int
    title: str
    line_index: int
    kind: str = "atx"


@dataclass
class Section:
    heading: Heading
    start_line: int
    end_line: int  # exclusive
    raw_lines: List[str] = field(default_factory=list)

    @property
    def raw_text(self) -> str:
        return "\n".join(self.raw_lines).rstrip("\n")


# v9: Provenance types for trust scoring by LLM
PROVENANCE_PRIMARY = "PRIMARY"
PROVENANCE_FALLBACK_HEADING = "FALLBACK_HEADING"
PROVENANCE_FALLBACK_SENTENCE = "FALLBACK_SENTENCE"
PROVENANCE_SECTIONLESS = "SECTIONLESS"


@dataclass
class Chunk:
    doc_id: str
    source_path: str
    heading: str
    heading_path: str
    category: str
    score: float
    text: str
    start_line: int
    end_line: int
    provenance: str = PROVENANCE_PRIMARY  # v9: track extraction source
    debug: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source_path": self.source_path,
            "heading": self.heading,
            "heading_path": self.heading_path,
            "category": self.category,
            "score": float(self.score),
            "text": self.text,
            "start_line": int(self.start_line),
            "end_line": int(self.end_line),
            "provenance": self.provenance,  # v9
            "debug": self.debug or {},
        }



@dataclass
class ExtractConfig:
    # Mode
    mode: str = "chunks"  # headings_only | chunks

    # IO
    root_dir: Optional[Path] = None
    headers_json: Optional[Path] = None
    out_json: Optional[Path] = None
    out_jsonl: Optional[Path] = None

    # Extraction
    include_cell_assembly: bool = True
    include_electrolyte: bool = True
    include_weak_materials: bool = True

    # Cleaning / segmentation
    min_paragraph_chars: int = 40
    max_paragraph_chars: int = 2500
    merge_short_paragraphs: bool = True
    merge_short_threshold_chars: int = 160
    drop_garbage_paragraphs: bool = True

    # v7: aggressive cleanup toggles
    remove_bracket_citations: bool = True
    remove_parenthetical_citations: bool = True
    remove_linked_citations: bool = True
    remove_parenthetical_figrefs: bool = True
    strip_in_sentence_figrefs: bool = False
    drop_caption_blocks: bool = True
    drop_fig_table_ref_sentences: bool = True

    # Sentence tagging
    enable_sentence_tagging: bool = True
    output_tagged_sentences: bool = True
    drop_tag_CHAR: bool = True
    drop_tag_RESULT: bool = True
    drop_tag_ECHEM_TEST: bool = True
    drop_tag_FIGREF: bool = True
    drop_tag_INTERLAYER: bool = True
    drop_tag_CATHODE: bool = True

    # =========================================================================
    # v8: Recall-first options (Soft-keep with flags)
    # =========================================================================
    # Zn-salt vs Zn-metal 분리 (PROC_ZN 오탐 방지)
    strict_proc_zn: bool = False  # True면 salt_like는 PROC_ZN에서 제외
    
    # 약한 recipe verb conf 낮춤
    penalize_weak_verbs: bool = True  # True면 weak verb만 있으면 conf↓
    
    # 하이브리드 헤딩 포함 (Synthesis and characterization → synth)
    allow_hybrid_headings: bool = True
    
    # Formation purpose를 배터리 컨텍스트에서만 인정
    strict_formation: bool = False  # True면 activation 단독은 OTHER
    
    # Soft-keep 모드 (drop 대신 flag 부여)
    soft_keep_mode: bool = True  # True면 가능한 keep하고 noise_flags에 표시
    
    # Side-channel 저장 (dropped 문장도 별도 필드에 저장)
    store_dropped_snippets: bool = True
    
    # Evidence pack 생성
    generate_evidence_pack: bool = True
    evidence_pack_topk: int = 8  # 태그별 top-k 문장
    
    # Sentence features 저장 (LLM 판단 보조)
    store_sentence_features: bool = True
    
    # Results fallback (rubric)
    enable_results_fallback: bool = True
    fallback_min_proc_zn_sentences: int = 1
    fallback_allow_heading_mining: bool = True
    fallback_allow_sentence_mining: bool = True
    fallback_global_max_sentences: int = 45
    fallback_context_sentences: int = 1

    # Supplementary scoring
    enable_supp_scoring: bool = True
    auto_find_supp_candidates: bool = True
    llm_supp_scoring: bool = False
    supp_flag_threshold: float = 0.65

    # LLM backend
    llm_backend: str = "none"  # none | ollama
    llm_model: str = "qwen2.5:14b-instruct"
    ollama_url: str = "http://localhost:11434"
    llm_timeout: int = 120
    llm_max_calls_per_doc: int = 8
    llm_debug: bool = False

    # Optional LLM assist features
    llm_sentence_tagging: bool = False
    llm_paragraph_repair: bool = False

    # v7: LLM refine throttling
    llm_refine_max_per_doc: int = 3
    llm_refine_cache: bool = True

    # Misc
    debug: bool = False
    log_level: str = "INFO"



# =============================================================================
# Markdown file discovery
# =============================================================================

def find_marker_md_file(root_dir: Path, doc_id: str) -> Optional[Path]:
    folder = root_dir / doc_id
    if not folder.exists():
        return None

    candidates = list(folder.rglob("*.md"))
    if not candidates:
        return None

    for c in candidates:
        if c.stem == doc_id:
            return c

    return max(candidates, key=lambda p: p.stat().st_size)


def doc_id_base(doc_id: str) -> str:
    m = re.match(r"^(1-s2\.0-[A-Za-z0-9]+)", doc_id)
    if m:
        return m.group(1)
    for suf in ["-main", "-supp", "-sup", "-si", "-s1", "-s2", "-supporting", "-supplementary"]:
        if doc_id.endswith(suf):
            return doc_id[: -len(suf)]
    return doc_id


def find_supp_candidates(root_dir: Path, doc_id: str, max_candidates: int = 6) -> List[str]:
    base = doc_id_base(doc_id)
    if not root_dir.exists():
        return []
    hits: List[str] = []
    for p in root_dir.glob(base + "*"):
        if not p.is_dir():
            continue
        name = p.name.lower()
        if name == doc_id.lower():
            continue
        if any(k in name for k in ["supp", "sup", "si", "support", "supplement"]):
            md = find_marker_md_file(root_dir, p.name)
            if md:
                hits.append(str(md))
        if len(hits) >= max_candidates:
            break
    return hits


# =============================================================================
# Text cleaning utilities
# =============================================================================

MD_HEADING_RE = re.compile(r'^\s*(?:>+\s*)?(?:[-*+•]\s*)?(#{1,6})\s*(.+?)\s*$')
SETEXT_H1_RE = re.compile(r'^\s*={3,}\s*$')
SETEXT_H2_RE = re.compile(r'^\s*-{3,}\s*$')

CODE_FENCE_RE = re.compile(r'^\s*```')

MD_IMAGE_RE = re.compile(r'!\[[^\]]*\]\([^)]+\)')
MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]+\)')

HTML_TAG_RE = re.compile(r"<[^>]+>")

MD_TABLE_SEP_RE = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$')
MD_TABLE_ROW_RE = re.compile(r'^\s*\|.+\|\s*$')

LATEX_BLOCK_MATH_START = re.compile(r'^\s*\$\$\s*$')
LATEX_INLINE_MATH_RE = re.compile(r"\$(?:\\.|[^$])+\$")


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def strip_markdown_emphasis(s: str) -> str:
    s = s.replace("**", "").replace("__", "")
    s = s.replace("*", "").replace("_", "")
    return s


def html_to_text_preserve_sub_sup(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"<\s*sub\s*>(.*?)<\s*/\s*sub\s*>", r"\1", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<\s*sup\s*>(.*?)<\s*/\s*sup\s*>", r"^\1", s, flags=re.IGNORECASE | re.DOTALL)
    s = HTML_TAG_RE.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&#160;", " ")
    s = normalize_ws(s)
    return s


def fix_chemical_formulas(text: str) -> str:
    """
    (A) Fix broken chemical formulas - extended patterns
    """
    # Common electrolytes/materials whitelist
    patterns = [
        # Sulfates
        (r"\bNa\s*2?\s*S\s*O\s*4\b", "Na2SO4"),
        (r"\bZn\s*S\s*O\s*4\b", "ZnSO4"),
        (r"\bK\s*2?\s*S\s*O\s*4\b", "K2SO4"),
        (r"\bMg\s*S\s*O\s*4\b", "MgSO4"),
        (r"\bCu\s*S\s*O\s*4\b", "CuSO4"),
        # Oxides
        (r"\bV\s*2?\s*O\s*5\b", "V2O5"),
        (r"\bMn\s*O\s*2\b", "MnO2"),
        (r"\bTi\s*O\s*2\b", "TiO2"),
        (r"\bAl\s*2?\s*O\s*3\b", "Al2O3"),
        (r"\bZn\s*O\b", "ZnO"),
        (r"\bCu\s*O\b", "CuO"),
        # Chlorides
        (r"\bZn\s*Cl\s*2\b", "ZnCl2"),
        (r"\bNa\s*Cl\b", "NaCl"),
        # Triflates/TFSI
        (r"\bZn\s*\(?\s*OTf\s*\)?\s*2\b", "Zn(OTf)2"),
        (r"\bZn\s*\(?\s*TFSI\s*\)?\s*2\b", "Zn(TFSI)2"),
        # Hydroxides
        (r"\bZn\s*\(?\s*OH\s*\)?\s*2\b", "Zn(OH)2"),
        (r"\bNa\s*OH\b", "NaOH"),
        # Carbonates
        (r"\bNa\s*2?\s*C\s*O\s*3\b", "Na2CO3"),
        (r"\bZn\s*C\s*O\s*3\b", "ZnCO3"),
        # Generic element-space-number collapse
        (r"\b([A-Z][a-z]?)\s+(\d)\s*([A-Z][a-z]?)\b", r"\g<1>\g<2>\g<3>"),
    ]
    for pat, repl in patterns:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    return text



def drop_markdown_images_and_links(s: str, cfg: Optional[ExtractConfig] = None) -> str:
    s = MD_IMAGE_RE.sub(" ", s)
    # v7: remove linked citation BEFORE normal link stripping
    if cfg is None or cfg.remove_linked_citations:
        s = LINKED_CITATION_RE.sub(" ", s)
    # then strip normal markdown links, keep link text
    s = MD_LINK_RE.sub(r"\1", s)
    return s


def remove_equation_only_blocks(lines: List[str]) -> List[str]:
    out: List[str] = []
    in_block_math = False
    buff: List[str] = []

    def flush_block_math(block: List[str]) -> None:
        joined = "\n".join(block).strip()
        words = re.findall(r"[A-Za-z]{3,}", joined)
        if len(words) <= 3:
            return
        out.extend(block)

    for ln in lines:
        if LATEX_BLOCK_MATH_START.match(ln):
            if in_block_math:
                in_block_math = False
                flush_block_math(buff)
                buff = []
            else:
                in_block_math = True
                buff = [ln]
            continue

        if in_block_math:
            buff.append(ln)
            continue

        out.append(ln)

    if in_block_math and buff:
        flush_block_math(buff)
    return out


def remove_markdown_tables(lines: List[str]) -> List[str]:
    out: List[str] = []
    in_table = False
    for ln in lines:
        if MD_TABLE_ROW_RE.match(ln) or MD_TABLE_SEP_RE.match(ln):
            in_table = True
            continue
        if in_table:
            if not ln.strip():
                in_table = False
            continue
        out.append(ln)
    return out


def is_heading_like_line(line: str) -> bool:
    return bool(MD_HEADING_RE.match(line))


def is_code_fence_line(line: str) -> bool:
    return bool(CODE_FENCE_RE.match(line))


def line_is_math_junk(ln: str) -> bool:
    s = ln.strip()
    if not s:
        return False
    if len(s) <= 32 and MATH_JUNK_LINE_RE.match(s):
        return True
    letters = re.findall(r"[A-Za-z]", s)
    digits = re.findall(r"\d", s)
    if len(digits) >= 8 and len(letters) == 0 and len(s) <= 50:
        return True
    if ("\\times" in s or "×" in s) and len(letters) == 0:
        return True
    return False


def repair_spurious_blank_lines(raw_lines: List[str]) -> List[str]:
    lines = list(raw_lines)
    out: List[str] = []
    n = len(lines)

    def looks_like_para_end(s: str) -> bool:
        s = s.strip()
        if not s:
            return True
        if s.endswith((".", "!", "?", ":", ";")):
            return True
        if is_heading_like_line(s) or re.match(r"^\s*[-*+•]\s+", s):
            return True
        if MD_TABLE_ROW_RE.match(s) or MD_TABLE_SEP_RE.match(s):
            return True
        return False

    def looks_like_continuation_start(s: str) -> bool:
        s2 = s.lstrip()
        if not s2:
            return False
        if s2[0].islower() or s2[0].isdigit():
            return True
        if s2.startswith(("(", "[", "{", ")", "]", "}")):
            return True
        if s2.startswith((",", ".", ";")):
            return True
        if s2.startswith(("$$", "$", "\\[")):
            return True
        return False

    for i, ln in enumerate(lines):
        if ln.strip():
            out.append(ln)
            continue

        prev = out[-1] if out else ""
        nxt = lines[i + 1] if i + 1 < n else ""
        if prev.strip() and nxt.strip():
            # (4) Stronger caption end check: if next line is full sentence, break caption block (used later)
            # Here we just handling spurious blank lines.
            if (not looks_like_para_end(prev)) and looks_like_continuation_start(nxt):
                continue
        out.append(ln)

    return out



def unwrap_hard_wrapped_lines(lines: List[str]) -> List[str]:
    out: List[str] = []
    buff: List[str] = []
    in_code = False

    def flush() -> None:
        nonlocal buff
        if buff:
            out.append(" ".join(x.strip() for x in buff if x.strip()))
            buff = []

    for ln in lines:
        if is_code_fence_line(ln):
            flush()
            out.append(ln.rstrip())
            in_code = not in_code
            continue
        if in_code:
            out.append(ln.rstrip())
            continue
        if not ln.strip():
            flush()
            out.append("")
            continue
        if is_heading_like_line(ln) or re.match(r"^\s*[-*+•]\s+", ln):
            flush()
            out.append(ln.rstrip())
            continue
        
        # (1) Fix "0." cut off: if buff ends with "digit." and ln starts with digit/unit, merge without space
        if buff:
            last = buff[-1].rstrip()
            # aggressive check for broken decimal: "0." or "12." at end of line
            if re.search(r"\b\d+\.$", last) and re.match(r"^\s*\d", ln):
                 # Merge directly without space to fix "0." + "5" -> "0.5"
                 buff[-1] = last + ln.lstrip()
                 continue
                 
        buff.append(ln.rstrip())

    flush()
    return out



def clean_line_basic(ln: str, cfg: Optional[ExtractConfig] = None) -> str:
    ln = drop_markdown_images_and_links(ln, cfg=cfg)
    ln = html_to_text_preserve_sub_sup(ln)
    ln = strip_markdown_emphasis(ln)
    ln = LATEX_INLINE_MATH_RE.sub(" ", ln)
    ln = ln.replace("\u00a0", " ").replace("\ufeff", " ")

    # (6) Unicode hyphen normalization
    ln = ln.replace("−", "-").replace("–", "-")
    
    # (2) Chemical formulas
    ln = fix_chemical_formulas(ln)

    # (3) Option to strip fig refs from text instead of dropping sentence
    if cfg is not None and cfg.strip_in_sentence_figrefs:
        ln = FIGREF_SENT_RE.sub(" ", ln)

    # v7: remove parenthetical figrefs BEFORE sentence splitting (line-level)
    if cfg is None or cfg.remove_parenthetical_figrefs:
        ln = PAREN_FIGREF_RE.sub(" ", ln)

    # v7: citations
    if cfg is None or cfg.remove_bracket_citations:
        ln = CITATION_BRACKET_RE.sub(" ", ln)
    if cfg is not None and cfg.remove_parenthetical_citations:
        ln = CITATION_PAREN_RE.sub(" ", ln)

    ln = normalize_ws(ln)
    return ln.rstrip()



def drop_caption_blocks(lines: List[str], cfg: Optional[ExtractConfig] = None) -> List[str]:
    """
    v9: 개선된 측션 블록 드롭
    - procedural 신호가 있는 문장은 keep
    - 호문 판별 조건 강화 (Fig. S1 시작 + 길이/구두점/동사/단위 기반)
    """
    out: List[str] = []
    in_cap = False
    
    def is_likely_caption_line(ln: str) -> bool:
        """v9: 측션 판별 조건 강화"""
        t = ln.strip()
        if not t:
            return False
        # Fig/Table 시작이 아니면 측션 아님
        if not CAPTION_START_RE.match(t):
            return False
        # 너무 짧으면 (< 40자) 확실히 측션
        if len(t) < 40:
            return True
        # procedural 신호가 있으면 측션이 아닐 수 있음
        t_lower = t.lower()
        procedural_signals = ["dissolved", "stirred", "heated", "coated", "deposited", 
                              "placed", "transferred", "dried", "washed", "immersed",
                              "min", "hour", "°c", "rpm"]
        if any(sig in t_lower for sig in procedural_signals):
            return False
        # 일반적인 측션: 설명적 동사가 있거나 관찰 내용
        description_signals = ["shows", "depicts", "illustrates", "presents", "indicates",
                               "displays", "observed", "measured"]
        if any(sig in t_lower for sig in description_signals):
            return True
        return True  # 기본적으로 측션으로 간주
    
    for ln in lines:
        if in_cap:
            if not ln.strip():
                in_cap = False
            # (4) Strengthen caption end condition: if line looks like a full new sentence
            elif len(ln) > 60 and ln.strip()[0].isupper() and ln.strip().endswith("."):
                in_cap = False
                out.append(ln)  # keep this line
            continue
        if is_likely_caption_line(ln.strip()):
            in_cap = True
            continue
        out.append(ln)
    return out



def preprocess_section_lines(raw_lines: List[str], cfg: Optional[ExtractConfig] = None) -> List[str]:
    # (5) Global References/Acknowledgements cut
    cut_idx = None
    for i, ln in enumerate(raw_lines):
        if is_heading_like_line(ln):
            t = strip_heading_hashes(ln).lower()
            if "references" in t or "acknowledgements" in t or "author contribution" in t:
                cut_idx = i
                break
    if cut_idx is not None:
        raw_lines = raw_lines[:cut_idx]

    lines = [clean_line_basic(ln, cfg=cfg) for ln in raw_lines]
    lines = remove_markdown_tables(lines)
    if cfg is not None and cfg.drop_caption_blocks:
        lines = drop_caption_blocks(lines, cfg)
    lines = [ln for ln in lines if not line_is_math_junk(ln)]
    lines = remove_equation_only_blocks(lines)
    lines = repair_spurious_blank_lines(lines)
    lines = unwrap_hard_wrapped_lines(lines)
    return lines



def split_markdown_into_paragraphs(lines: List[str]) -> List[str]:
    paras: List[str] = []
    buff: List[str] = []
    in_list = False
    in_code = False

    def flush() -> None:
        nonlocal buff, in_list
        txt = "\n".join(buff).strip("\n")
        if txt.strip():
            paras.append(txt)
        buff = []
        in_list = False

    for ln in lines:
        if is_code_fence_line(ln):
            flush()
            paras.append(ln.strip())
            in_code = not in_code
            continue

        if in_code:
            buff.append(ln.rstrip())
            continue

        if not ln.strip():
            flush()
            continue

        if is_heading_like_line(ln):
            flush()
            continue

        if re.match(r"^\s*[-*+•]\s+", ln):
            # (13) List item / bullet -> force flush to split paragraphs
            flush()
            if not in_list:
                in_list = True
            buff.append(ln.rstrip())
            continue


        if in_list and (ln.startswith("  ") or ln.startswith("\t")):
            buff.append(ln.rstrip())
            continue

        buff.append(ln.rstrip())

    flush()
    return paras


def paragraph_is_mostly_garbage(p: str) -> bool:
    if not p.strip():
        return True
    letters = re.findall(r"[A-Za-z]", p)
    if len(letters) < 8:
        return True
    if p.count("|") >= 8:
        return True
    return False


def truncate_long_paragraph(p: str, max_chars: int) -> str:
    p = p.strip()
    if len(p) <= max_chars:
        return p
    return p[:max_chars].rstrip() + " ..."


# =============================================================================
# Heading parsing and section building
# =============================================================================

def strip_heading_hashes(line: str) -> str:
    m = MD_HEADING_RE.match(line)
    if not m:
        return line.strip()
    return m.group(2).strip()


def normalize_heading_title(title: str) -> str:
    t = html_to_text_preserve_sub_sup(title)
    t = strip_markdown_emphasis(t)
    t = normalize_ws(t).lower()
    t = t.replace("&", "and")
    return t


def parse_numeric_prefix(title: str) -> Tuple[Optional[Tuple[int, ...]], str]:
    s = normalize_ws(title)
    m = re.match(r"^\s*(\d+(?:\.\d+)*)\s*[\.\)]?\s*(.*)$", s)
    if not m:
        return None, s
    num_str = m.group(1)
    rest = m.group(2).strip()
    try:
        nums = tuple(int(x) for x in num_str.split("."))
        return nums, rest
    except Exception:
        return None, s


# v9: 평문 헤딩 패턴 (marker-pdf에서 # 없이 출력되는 경우)
PLAIN_HEADING_PATTERNS = [
    # ALL CAPS 단독 라인 (3-40자)
    re.compile(r"^\s*([A-Z][A-Z\s]{2,38}[A-Z])\s*$"),
    # "2. Experimental" 패턴
    re.compile(r"^\s*(\d+\.?\s+(?:Experimental|Experiment|Methods|Methodology|Materials and Methods|Synthesis|Preparation|Fabrication|Results|Discussion|Conclusion)[s]?)\s*$", re.IGNORECASE),
    # "Experimental section", "Experimental details" 등
    re.compile(r"^\s*(Experimental\s+(?:section|details|procedure|procedures))\s*$", re.IGNORECASE),
]


def detect_headings(md_lines: List[str]) -> List[Heading]:
    """
    v9: 강화된 헤딩 탐지 - ATX/Setext + 평문 헤딩 패턴
    """
    headings: List[Heading] = []
    i = 0
    while i < len(md_lines):
        line = md_lines[i]
        
        # ATX 헤딩 (# 시작)
        m = MD_HEADING_RE.match(line)
        if m:
            hashes = m.group(1)
            title = m.group(2).strip()
            headings.append(Heading(raw=line.rstrip("\n"), level=len(hashes), title=title, line_index=i, kind="atx"))
            i += 1
            continue

        # Setext 헤딩 (===, ---)
        if i + 1 < len(md_lines) and md_lines[i].strip() and (SETEXT_H1_RE.match(md_lines[i + 1]) or SETEXT_H2_RE.match(md_lines[i + 1])):
            title = md_lines[i].strip()
            lvl = 1 if SETEXT_H1_RE.match(md_lines[i + 1]) else 2
            headings.append(Heading(raw=title, level=lvl, title=title, line_index=i, kind="setext"))
            i += 2
            continue
        
        # v9: 평문 헤딩 패턴 탐지
        for pat in PLAIN_HEADING_PATTERNS:
            pm = pat.match(line)
            if pm:
                title = pm.group(1).strip()
                # ALL CAPS는 level 2, 그 외는 level 1로 취급
                lvl = 2 if title.isupper() else 1
                headings.append(Heading(raw=line.rstrip("\n"), level=lvl, title=title, line_index=i, kind="plain"))
                break
        else:
            i += 1
            continue
        i += 1

    headings.sort(key=lambda h: h.line_index)
    return headings


def build_sections(md_lines: List[str], headings: List[Heading]) -> List[Section]:
    if not headings:
        return []
    sections: List[Section] = []
    for idx, h in enumerate(headings):
        start = h.line_index
        end = headings[idx + 1].line_index if idx + 1 < len(headings) else len(md_lines)
        raw = md_lines[start:end]
        sections.append(Section(heading=h, start_line=start, end_line=end, raw_lines=raw))
    return sections


# =============================================================================
# Candidate heading classification (rubric)
# =============================================================================

def is_method_main_heading(title: str) -> bool:
    t = normalize_heading_title(title)
    if t in {"materials", "material"}:
        return False
    return any(k in t for k in METHOD_MAIN_KEYWORDS)


def is_weak_material_heading(title: str) -> bool:
    t = normalize_heading_title(title)
    if any(x in t for x in ["characteriz", "characteris", "electrochemical", "measurement", "test", "testing"]):
        return False
    if any(x in t for x in SYNTH_MANUF_EXCLUDE_TERMS):
        return False
    return any(k in t for k in WEAK_MATERIAL_HEADINGS)


def is_recipe_heading(title: str) -> bool:
    t = normalize_heading_title(title)
    if any(x in t for x in SYNTH_MANUF_EXCLUDE_TERMS):
        return False
    return any(k in t for k in SYNTH_MANUF_KEYWORDS)


def is_characterization_heading(title: str) -> bool:
    t = normalize_heading_title(title)
    if "characteriz" in t or "characteris" in t:
        return True
    # heading에서는 phrase도 허용
    if CHAR_ABBR_RE.search(t):
        return True
    return any(ph in t for ph in CHAR_PHRASES)


def is_electrochem_heading(title: str) -> bool:
    t = normalize_heading_title(title)
    if "electrochemical" in t and ("test" in t or "measurement" in t):
        return True
    if ECHEM_ABBR_RE.search(t):
        return True
    return any(ph in t for ph in ECHEM_PHRASES)


def is_results_heading(title: str) -> bool:
    # (T) Extended to catch strategy/rationale headings
    t = normalize_heading_title(title)
    return any(k in t for k in ["results", "discussion", "result and discussion", "conclusion", "mechanism", "strategy", "rationale"])


def heading_category(title: str, cfg: ExtractConfig) -> str:
    """
    v9: hybrid heading 규칙 추가
    - "Synthesis and characterization" -> synth_manuf (우선) + hybrid flag
    """
    t = normalize_heading_title(title)
    
    # v9: Hybrid heading 처리 (synthesis + characterization)
    if cfg.allow_hybrid_headings:
        synth_keywords = ["synthes", "prepar", "fabricat", "manufactur"]
        char_keywords = ["characteriz", "characteris"]
        has_synth = any(k in t for k in synth_keywords)
        has_char = any(k in t for k in char_keywords)
        if has_synth and has_char:
            # Hybrid: synth로 우선 처리
            return "synth_manuf"  # debug["hybrid_heading"] = True는 호출자에서 처리
    
    if is_method_main_heading(title):
        return "method_main"
    if is_characterization_heading(title):
        return "characterization"
    if is_results_heading(title):
        return "results"
    if cfg.include_cell_assembly and ("assembly" in t or "cell" in t or "battery" in t):
        return "assembly"
    if cfg.include_electrolyte and "electrolyte" in t:
        return "electrolyte"
    if is_recipe_heading(title):
        return "synth_manuf"
    if cfg.include_weak_materials and is_weak_material_heading(title):
        return "materials_weak"
    if is_electrochem_heading(title):
        return "echem_test"
    return "other"


# =============================================================================
# Sentence splitting
# =============================================================================

# v9: 숫자 abbreviation 제거 - enumerated step(1., 2.)이 한 문장으로 합쳐지는 것 방지
# 소수점(0.5)은 _prev_token_before로 처리되므로 괜찮음
_ABBREV = {
    "e.g", "i.e", "etc", "vs", "mr", "mrs", "ms", "dr", "prof", "fig", "figs", "eq", "eqs",
    "ref", "refs", "no", "nos", "vol", "inc", "ltd", "co", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "wt", "at", 
}



def _prev_token_before(text: str, idx: int) -> str:
    j = idx - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    end = j + 1
    while j >= 0 and (text[j].isalnum() or text[j] in ".-"):
        j -= 1
    tok = text[j + 1:end]
    return tok.strip()


def split_sentences(text: str) -> List[str]:
    text = normalize_ws(text)
    if not text:
        return []
    out: List[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ".?!":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and (text[j].isupper() or text[j].isdigit() or text[j] in "(["):
                tok = _prev_token_before(text, i + 1).lower().rstrip(".")
                if tok in _ABBREV:
                    i += 1
                    continue
                sent = text[start:i + 1].strip()
                if sent:
                    out.append(sent)
                start = i + 1
        i += 1
    last = text[start:].strip()
    if last:
        out.append(last)
    return out


# =============================================================================
# Sentence tagging per rubric (v8)
# =============================================================================

def contains_any(text: str, terms: Sequence[str]) -> bool:
    t = text.lower()
    return any(term in t for term in terms)


def has_units(text: str) -> bool:
    return bool(UNITS_RE.search(text))


# ============================================================================
# v8: Feature extraction functions
# ============================================================================

def has_zn_metal_context(text: str) -> bool:
    """Check if text refers to Zn metal (foil/anode/electrode)"""
    t = text.lower()
    return any(term in t for term in ZN_METAL_CONTEXT)


def has_zn_salt_context(text: str) -> bool:
    """Check if text refers to Zn salt (ZnSO4, Zn(OTf)2, etc.)"""
    t = text.lower()
    return any(term in t for term in ZN_SALT_CONTEXT)


def has_strong_recipe_verb(text: str) -> bool:
    """Check if text has strong (unambiguous) recipe verbs"""
    t = text.lower()
    return any(v in t for v in STRONG_RECIPE_VERBS)


def has_weak_recipe_verb_only(text: str) -> bool:
    """Check if text has only weak recipe verbs (no strong ones)"""
    t = text.lower()
    has_weak = any(v in t for v in WEAK_RECIPE_VERBS)
    has_strong = any(v in t for v in STRONG_RECIPE_VERBS)
    return has_weak and not has_strong


def has_time_axis_signal(text: str) -> bool:
    """Check if text has pre-cycling/pre-assembly time signals"""
    t = text.lower()
    return any(sig in t for sig in TIME_AXIS_PRE_SIGNALS)


def has_aqueous_signal(text: str) -> bool:
    """Check if text has aqueous/water-based signals"""
    t = text.lower()
    return any(sig in t for sig in AQUEOUS_SIGNALS)


def has_lab_scale_signal(text: str) -> bool:
    """Check if text has lab-scale experiment signals"""
    t = text.lower()
    return any(sig in t for sig in LAB_SCALE_SIGNALS)


def extract_sentence_features(text: str) -> Dict[str, bool]:
    """
    v8: Extract lightweight features for each sentence.
    These help LLM understand why a sentence was tagged.
    """
    return {
        "has_zn_metal": has_zn_metal_context(text),
        "has_zn_salt": has_zn_salt_context(text),
        "has_strong_verb": has_strong_recipe_verb(text),
        "has_weak_verb_only": has_weak_recipe_verb_only(text),
        "has_layer_anchor": has_layer_anchor(text),
        "has_process_units": has_process_units(text),
        "has_performance_units": has_performance_units(text),
        "has_time_axis": has_time_axis_signal(text),
        "has_aqueous": has_aqueous_signal(text),
        "has_lab_scale": has_lab_scale_signal(text),
    }


def has_layer_anchor(text: str) -> bool:
    t = text.lower()
    return any(a in t for a in LAYER_ANCHORS)


def has_char_marker(text: str) -> bool:
    t = text.lower()
    if CHAR_ABBR_RE.search(text):
        return True
    return any(ph in t for ph in CHAR_PHRASES)


def has_echem_marker(text: str) -> bool:
    t = text.lower()
    if ECHEM_ABBR_RE.search(text):
        return True
    return any(ph in t for ph in ECHEM_PHRASES)


def is_figref_sentence(s: str) -> bool:
    return bool(FIGREF_SENT_RE.search(s))


def is_result_sentence(s: str, cfg: ExtractConfig) -> bool:
    t = s.lower()
    if cfg.drop_fig_table_ref_sentences and is_figref_sentence(s):
        return True
    return any(m in t for m in RESULT_MARKERS)


def is_char_sentence(s: str) -> bool:
    # v7: word-boundary 기반 (between/BET, temperature/TEM 오탐 제거)
    return has_char_marker(s)


def is_echem_test_sentence(s: str) -> bool:
    # v7: word-boundary 기반 (cv, eis 등 오탐 제거)
    t = s.lower()
    if "electrochemical" in t and ("test" in t or "measurement" in t):
        return True
    return has_echem_marker(s)


def is_interlayer_sentence(s: str) -> bool:
    t = s.lower()
    if any(x in t for x in INTERLAYER_TERMS):
        # Zn 보호층이 아니라 separator/interlayer 문장일 가능성이 큼
        return True
    return False


def is_cathode_sentence(s: str) -> bool:
    t = s.lower()
    # 강한 cathode anchor or cathode-material hint
    if any(x in t for x in CATHODE_STRONG_ANCHORS):
        return True
    if any(x in t for x in CATHODE_MATERIAL_HINTS) and ("cathode" in t or "positive" in t):
        return True
    return False


def is_formation_purpose(s: str, cfg: Optional["ExtractConfig"] = None) -> Tuple[bool, List[str]]:
    """
    v8: Returns (is_formation, flags)
    - activation_ambiguous flag if activation appears without battery context
    """
    t = s.lower()
    flags = []
    
    # Battery context keywords
    battery_context = ["cycle", "cycling", "cell", "electrochemical", "electrode", 
                       "formation", "conditioning", "sei", "anode", "cathode"]
    has_battery_ctx = any(k in t for k in battery_context)
    
    # Check for clear formation signals
    if any(k in t for k in ["formation cycle", "conditioning cycle"]):
        return True, flags
    
    if "activation" in t:
        if has_battery_ctx:
            return True, flags
        else:
            # Ambiguous - might be chemical activation, not electrochemical
            flags.append("activation_ambiguous")
            if cfg and cfg.strict_formation:
                return False, flags
            return True, flags
    
    if "sei" in t and any(k in t for k in ["form", "forming", "formation", "build", "construct", "generate"]):
        return True, flags
    if "to form" in t and ("sei" in t or "protective layer" in t or "interface" in t):
        return True, flags
    if "in-situ" in t and ("sei" in t or "formation" in t or "protective layer" in t):
        return True, flags
    if "during cycling" in t and ("sei" in t or "layer" in t or "interphase" in t):
        return True, flags
    
    return False, flags


def is_proc_zn_sentence_v8(s: str, cfg: Optional["ExtractConfig"] = None) -> Tuple[str, List[str]]:
    """
    v8: Returns (sub-type, flags)
    - sub-type: "TREAT", "USAGE", "SALT_LIKE", "NONE"
    - flags: ["salt_like", "weak_treat", "no_metal_context", etc.]
    """
    t = s.lower()
    flags = []
    
    # Check Zn presence
    if not contains_any(t, ZN_ANCHORS) and "zn" not in t:
        return "NONE", flags
    
    # v8: Detect salt vs metal context
    has_metal = has_zn_metal_context(s)
    has_salt = has_zn_salt_context(s)
    
    if has_salt and not has_metal:
        flags.append("salt_like")
        if cfg and cfg.strict_proc_zn:
            return "SALT_LIKE", flags  # Will be sent to ELECTROLYTE instead
    
    if not has_metal and not has_salt:
        flags.append("no_metal_context")
    
    # USAGE patterns take priority
    usage_patterns = ["used as anode", "employed as anode", "served as anode", 
                      "as the anode", "purchased", "obtained from", "acquired from",
                      "without further purification", "as received"]
    if any(p in t for p in usage_patterns):
        strong_treat = ["coated", "deposited", "plated", "immersed", "etched", "soaked"]
        if any(c in t for c in strong_treat) and has_units(s):
            return "TREAT", flags
        return "USAGE", flags

    # Strong treatment cues
    strong_treat_cues = ["coated", "deposited", "plated", "immersed", "etched", "soaked", "electrodeposited"]
    weak_treat_cues = ["washed", "rinsed", "dried", "polished", "cleaned", "transferred"]
    
    if any(c in t for c in strong_treat_cues):
        return "TREAT", flags
    
    # Weak treatment needs context
    if any(c in t for c in weak_treat_cues):
        if has_units(s) or "solution" in t or " at " in t or " for " in t:
            flags.append("weak_treat")
            return "TREAT", flags
        return "USAGE", flags

    # Recipe verbs + units = TREAT
    if contains_any(t, RECIPE_VERBS) and has_units(s):
        if has_weak_recipe_verb_only(s):
            flags.append("weak_verb")
        return "TREAT", flags

    return "USAGE", flags


# Legacy wrapper for backward compatibility
def is_proc_zn_sentence(s: str) -> str:
    result, _ = is_proc_zn_sentence_v8(s)
    return result




def is_proc_coat_material_sentence(s: str) -> bool:
    """
    v7: 보호층 material/film 제작 레시피를 적극적으로 살림
    (F) Added strong negative rules for description/characterization sentences
    """
    t = s.lower()
    
    # (F) Strong negative markers - these are CHAR/RESULT descriptions, not recipes
    char_result_markers = ["binding energy", "c 1s", "o 1s", "ev", "deprotonation", 
                           "assignment", "xps fitting", "peak at", "attributed to",
                           "can be explained", "is believed", "is thought"]
    if any(m in t for m in char_result_markers):
        return False
    
    # (F) Penalize lazy verbs - these don't constitute recipes
    if any(x in t for x in ["provides", "improves", "delivers", "exhibits", "shows", "demonstrates"]):
        # Only allow if also has STRONG recipe verbs with units
        strong_recipe = ["dissolved", "coated", "immersed", "deposited", "dried"]
        if not (any(v in t for v in strong_recipe) and has_process_units(s)):
            return False

    # Required: must have recipe verbs
    if not contains_any(t, RECIPE_VERBS):
        return False

    # 전형적 패턴(수동태)
    passive_patterns = ["was dissolved", "were dissolved", "was added", "were added",
                        "was poured", "were poured", "were collected", "was collected",
                        "was removed", "were removed", "was transferred", "were transferred"]
    if any(pat in t for pat in passive_patterns):
        return True

    # layer/material anchor가 있으면 units 없어도 레시피 동사만으로 충분 (but check penalties)
    if has_layer_anchor(s):
        if any(x in t for x in ["provides", "improves", "attributed to"]):
            return False
        return True

    # (F) Require at least one of: process units OR process cues
    process_cues = [
        "solution", "solvent", "precursor", "mixture", "dispersion",
        "room temperature", "rt", "overnight", "vacuum", "filtered",
        "centrifug", "washed", "dried", "evaporat", "aged", "kept", "maintained",
    ]
    if has_process_units(s) or any(c in t for c in process_cues):
        return True

    return False



def is_electrolyte_sentence(s: str) -> bool:
    t = s.lower()
    if "electrolyte" in t:
        return True
    if any(x in t for x in ["znso4", "zns", "zntfs", "zn(tf", "zncl2", "zn(br", "zn(otf"]):
        return True
    if any(x in t for x in ["additive", "added into the electrolyte", "added to the electrolyte"]):
        return True
    return False


def electrolyte_has_additive(s: str) -> bool:
    """v9: 확장된 첨가제 패턴으로 recall 향상"""
    t = s.lower()
    if "electrolyte" not in t and not any(x in t for x in ["znso4", "zns", "zntfs", "zn(tf", "zncl2"]):
        return False
    # v9: 더 많은 첨가제 표현 패턴
    add_words = [
        "additive", "added", "introduc", "dissolved", "mixed", "supplemented",
        "with", "containing", "inclusion of", "doped", "modified with",
    ]
    # v9: 농도 패턴도 체크 (x wt%, x M, x mM 등)
    has_conc_pattern = bool(re.search(r"\d+\s*(?:wt%|mol%|mM|M)\b", s, re.IGNORECASE))
    return any(w in t for w in add_words) or has_conc_pattern


def electrolyte_additive_for_formation(s: str) -> bool:
    t = s.lower()
    if not electrolyte_has_additive(s):
        return False
    cues = ["to form", "to build", "sei", "in situ", "formation", "during cycling", "protective layer", "interphase"]
    return any(c in t for c in cues)


def is_assembly_sentence(s: str) -> bool:
    # (Q) Extended assembly detection - check this BEFORE PROC_ZN
    t = s.lower()
    assembly_cues = ["cr2032", "coin cell", "swagelok", "cell was assembled", "assembled", 
                     "cells were assembled", "pouch cell", "full cell"]
    context_cues = ["separator", "electrolyte", "coin cell", "cathode", "anode"]
    if any(k in t for k in assembly_cues) and any(c in t for c in context_cues):
        return True
    # "assembled as cathode ... using Zn foil as anode" pattern
    if "assembled" in t and ("cathode" in t or "anode" in t):
        return True
    return False


def sentence_tag(s: str, cfg: ExtractConfig) -> Tuple[str, float, List[str]]:
    """
    v9: sentence_tag에서 v8 함수 직접 호출, flags 전달, weak verb 페널티 적용
    """
    flags: List[str] = []
    if not s.strip():
        return "OTHER", 0.0, flags

    # (Q) Check ASSEMBLY first - before PROC_ZN to catch mixed cathode/anode sentences
    if is_assembly_sentence(s):
        return "ASSEMBLY", 0.72, flags

    # (E) Rescue FIGREF sentences that have recipe content
    if cfg.drop_fig_table_ref_sentences and is_figref_sentence(s):
        # Check if it's also procedural - if so, strip figref text but keep sentence
        if looks_procedural_other(s) or (has_process_units(s) and contains_any(s.lower(), RECIPE_VERBS)):
            flags.append("figref_rescued")
            # Don't return FIGREF, continue to normal tagging
        else:
            return "FIGREF", 0.9, flags

    if is_result_sentence(s, cfg):
        return "RESULT", 0.9, flags

    # v7: interlayer/cathode 먼저 컷(누출 방지)
    if is_interlayer_sentence(s) and not contains_any(s.lower(), ZN_ANCHORS) and "zn" not in s.lower():
        return "INTERLAYER", 0.85, flags

    if is_cathode_sentence(s) and not contains_any(s.lower(), ZN_ANCHORS) and "zn" not in s.lower():
        return "CATHODE", 0.85, flags

    if is_char_sentence(s):
        return "CHAR", 0.9, flags

    # formation purpose는 ELECTROLYTE보다 먼저 - v9: flags 반환 활용
    is_form, form_flags = is_formation_purpose(s, cfg)
    if is_form:
        flags.extend(form_flags)
        return "FORMATION_IN_SITU", 0.75, flags

    # v9: PROC_ZN - is_proc_zn_sentence_v8 직접 호출 (flags 반환 활용)
    zn_type, zn_flags = is_proc_zn_sentence_v8(s, cfg)
    flags.extend(zn_flags)
    
    if zn_type == "SALT_LIKE":
        # v9: strict_proc_zn=True면 SALT_LIKE를 ELECTROLYTE로 라우팅
        if cfg.strict_proc_zn:
            return "ELECTROLYTE", 0.65, flags
        # 아니면 낮은 conf로 PROC_ZN 유지
        return "PROC_ZN", 0.55, flags
    
    if zn_type == "TREAT":
        conf = 0.9 if has_process_units(s) else 0.78
        # v9: weak verb 페널티 적용
        if cfg.penalize_weak_verbs and has_weak_recipe_verb_only(s):
            conf -= 0.15
            flags.append("weak_verb")
        return "PROC_ZN", conf, flags
    elif zn_type == "USAGE":
        return "ZN_USAGE", 0.5, flags

    if is_proc_coat_material_sentence(s):
        base_conf = 0.86 if (has_process_units(s) or has_layer_anchor(s)) else 0.75
        # v9: weak verb 페널티
        if cfg.penalize_weak_verbs and has_weak_recipe_verb_only(s):
            base_conf -= 0.15
            flags.append("weak_verb")
        return "PROC_COAT_MAT", base_conf, flags

    if is_electrolyte_sentence(s):
        return "ELECTROLYTE", 0.82 if has_process_units(s) else 0.68, flags

    if is_echem_test_sentence(s):
        return "ECHEM_TEST", 0.72, flags

    return "OTHER", 0.33, flags



# =============================================================================
# Recipe scoring and paragraph gates
# =============================================================================

# (H) Separate process units from performance units
PROCESS_UNITS_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(?:h|hr|hrs|hours|min|mins|minute|minutes|s|sec|secs|day|days)\b|"
    r"\b\d+(\.\d+)?\s*°\s*C\b|"
    r"\b\d+(\.\d+)?\s*(?:M|mM|wt%|mol%|vol%|v/v|w/v|mg\s*mL[-−]1|mg/mL|g\s*L[-−]1|g/L)\b|"
    r"\b\d+(\.\d+)?\s*(?:mg|g|kg|µg|ug|mL|ml|L|µL|uL|mmol|mol)\b|"
    r"\b\d+(\.\d+)?\s*(?:rpm)\b",
    flags=re.IGNORECASE
)

PERFORMANCE_UNITS_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(?:mAh\s*g[-−]1|mAh/g|Wh\s*kg[-−]1)\b|"
    r"\b\d+(\.\d+)?\s*(?:V|mV)\b|"
    r"\b\d+(\.\d+)?\s*(?:mA|A)\s*cm[-−]2\b|"
    r"\b\d+(\.\d+)?\s*eV\b",
    flags=re.IGNORECASE
)

def has_process_units(text: str) -> bool:
    return bool(PROCESS_UNITS_RE.search(text))

def has_performance_units(text: str) -> bool:
    return bool(PERFORMANCE_UNITS_RE.search(text))

def recipe_score_paragraph(paragraph: str) -> Tuple[float, Dict[str, Any]]:
    t = paragraph.lower()
    score = 0.0
    hits: List[str] = []

    if contains_any(t, ZN_ANCHORS) or " zn " in f" {t} ":
        score += 1.5
        hits.append("zn_anchor")
    if has_layer_anchor(paragraph):
        score += 1.0
        hits.append("layer_anchor")
    if contains_any(t, RECIPE_VERBS):
        score += 1.2
        hits.append("recipe_verb")
    # (H) Only process units contribute to score, performance units are neutral/negative
    if has_process_units(paragraph):
        score += 1.3
        hits.append("process_units")
    if has_performance_units(paragraph):
        score -= 0.3  # Slight penalty for performance units (likely results)
        hits.append("performance_units_penalty")
    if contains_any(t, ["before assembly", "prior to assembly", "before cell assembly", "prior to cell assembly"]):
        score += 0.8
        hits.append("pre_assembly")
    if contains_any(t, ["electrolyte", "znso4", "additive"]):
        score += 0.6
        hits.append("electrolyte")
    if contains_any(t, ["assembled", "cr2032", "swagelok", "coin cell"]):
        score += 0.5
        hits.append("assembly")
    if contains_any(t, ["formation cycle", "during cycling", "in-situ formation"]):
        score += 0.4
        hits.append("formation")
    if has_char_marker(paragraph):
        score -= 0.8
        hits.append("char_penalty")
    if any(m in t for m in RESULT_MARKERS) or is_figref_sentence(paragraph):
        score -= 0.6
        hits.append("result_or_figref_penalty")

    return score, {"hits": hits, "score": score}


def looks_like_pure_reagent_list(paragraph: str) -> bool:
    # (B) Strengthened reagent list detector - score-independent
    t = paragraph.strip().lower()
    vendor_words = ["sigma", "aldrich", "macklin", "alfa", "purchased", "supplier", 
                    "acquired", "obtained from", "without further purification", "as received"]
    
    # 2+ vendor words = definitely reagent list
    if sum(1 for w in vendor_words if w in t) >= 2:
        # (B) Only exception: has recipe verb + units + process cue together
        process_cues = ["solution", "dissolved", "mixed", "stirred", "heated"]
        if contains_any(t, RECIPE_VERBS) and has_process_units(paragraph) and any(c in t for c in process_cues):
            return False
        return True
    
    # comma heavy + purity + no recipe verbs
    if t.count(",") >= 4 and ("%" in t or "purity" in t):
        if not contains_any(t, ["was", "were", "prepared", "immersed", "coated", "dried", "assembled"]):
            return True
        
    if t.count(",") >= 6 and not contains_any(t, ["was", "were", "prepared", "immersed", "coated", "dried"]):
        return True
    return False



def paragraph_is_characterization(paragraph: str) -> bool:
    return has_char_marker(paragraph) and not contains_any(paragraph.lower(), RECIPE_VERBS)


def paragraph_is_echem_testing(paragraph: str) -> bool:
    t = paragraph.lower()
    if has_echem_marker(paragraph) and not is_formation_purpose(paragraph):
        return True
    return False


# =============================================================================
# Supplementary scoring (heuristic + optional LLM)
# =============================================================================

def clean_supp_evidence(evidence: List[str]) -> List[str]:
    """(O) Clean up SI evidence - remove DOI/URL noise"""
    cleaned = []
    doi_re = re.compile(r"\s*(?:doi|https?://|www\.).*$", re.IGNORECASE)
    for e in evidence:
        # Truncate at DOI/URL
        e_clean = doi_re.sub("", e).strip()
        if len(e_clean) > 20:  # Keep if still meaningful
            cleaned.append(e_clean[:200])  # Also limit length
    return cleaned[:6]

def detect_supp_pointer_evidence(text: str) -> Tuple[float, List[str]]:
    strength = 0.0
    evidence: List[str] = []
    # (P) Use paragraph-based splitting instead of single join
    for sent in split_sentences(text):
        # Skip very long "sentences" that are likely merged garbage
        if len(sent) > 500:
            continue
        for pat, sc in SUPP_POINTER_PATTERNS:
            if pat.search(sent):
                strength = max(strength, sc)
                evidence.append(sent.strip())
                break
    seen = set()
    out = []
    for e in evidence:
        if e not in seen:
            seen.add(e)
            out.append(e)
    # (O) Clean up DOI/URL noise from evidence
    return strength, clean_supp_evidence(out)


def compute_supp_importance(
    cfg: ExtractConfig,
    si_strength: float,
    si_evidence: List[str],
    method_ranges_found: bool,
    proc_zn_sentences: int,
    kept_sentences: int,
) -> Tuple[float, str]:
    score = 0.0
    reasons: List[str] = []

    if si_strength > 0:
        score = max(score, si_strength)
        reasons.append("SI/Supporting-Information pointer detected")
    if not method_ranges_found:
        score = max(score, 0.75)
        reasons.append("Methods/Experimental section not detected")
    if proc_zn_sentences < cfg.fallback_min_proc_zn_sentences:
        score = max(score, 0.70)
        reasons.append("Too few Zn-procedure sentences in main text")
    if kept_sentences <= 2:
        score = max(score, 0.60)
        reasons.append("Very small amount of usable recipe sentences")

    score = min(1.0, score)
    return score, "; ".join(reasons) if reasons else ""


# =============================================================================
# Ollama client (throttling + cache)
# =============================================================================

def extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    for end in range(len(text), start, -1):
        if text[end - 1] != "}":
            continue
        chunk = text[start:end]
        try:
            return json.loads(chunk)
        except Exception:
            continue
    return None


class LLMClient:
    def __init__(self, cfg: ExtractConfig) -> None:
        self.cfg = cfg
        self.calls = 0
        self.refine_calls = 0
        self._cache: Dict[str, Tuple[str, float, str]] = {}

    def enabled(self) -> bool:
        return self.cfg.llm_backend.lower() == "ollama"

    def _check(self) -> None:
        if not self.enabled():
            raise RuntimeError("LLM backend disabled")
        if requests is None:
            raise RuntimeError("requests not installed. Run: pip install requests")
        if self.calls >= self.cfg.llm_max_calls_per_doc:
            raise RuntimeError("LLM call budget exceeded for this document")

    def chat_json(self, system: str, user: str, temperature: float = 0.0) -> Dict[str, Any]:
        self._check()
        self.calls += 1
        url = self.cfg.ollama_url.rstrip("/") + "/api/chat"
        payload = {
            "model": self.cfg.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": temperature},
            "stream": False,
        }
        try:
            r = requests.post(url, json=payload, timeout=self.cfg.llm_timeout)
            r.raise_for_status()
            data = r.json()
            content = data.get("message", {}).get("content", "")
        except Exception as e:
            raise RuntimeError(f"Ollama chat failed: {e}")

        j = extract_first_json_object(content)
        if j is None:
            raise RuntimeError(f"LLM did not return JSON. Content: {content[:400]}")
        return j

    def _should_refine(self, sentence: str, conf: float) -> bool:
        if conf >= 0.45:
            return False
        t = sentence.lower()
        # v7: layer anchor도 refine 게이트에 포함(“Zn이 끝에만” 케이스 살리기)
        if contains_any(t, ZN_ANCHORS) and contains_any(t, RECIPE_VERBS):
            return True
        if has_units(sentence) and contains_any(t, RECIPE_VERBS):
            return True
        if has_layer_anchor(sentence) and contains_any(t, RECIPE_VERBS):
            return True
        if "electrolyte" in t and has_units(sentence):
            return True
        return False

    def refine_sentence_tag(self, sentence: str, base_conf: float) -> Tuple[Optional[str], float, str]:
        if not self.enabled() or not self.cfg.llm_sentence_tagging:
            return None, 0.0, ""
        if self.refine_calls >= self.cfg.llm_refine_max_per_doc:
            return None, 0.0, ""

        s_key = sentence.strip()
        if self.cfg.llm_refine_cache and s_key in self._cache:
            tag, conf, why = self._cache[s_key]
            return tag, conf, why

        if not self._should_refine(sentence, base_conf):
            return None, 0.0, ""

        system = "You label AZIB paper sentences into a fixed tag set. Return strict JSON only."
        user = (
            "Classify the following sentence into one of:\n"
            "PROC_ZN, PROC_COAT_MAT, ELECTROLYTE, ASSEMBLY, FORMATION_IN_SITU, CHAR, ECHEM_TEST, RESULT, FIGREF, INTERLAYER, CATHODE, OTHER.\n"
            "Return JSON: {\"tag\":..., \"confidence\":0-1, \"rationale\":\"<=15 words\"}.\n\n"
            f"Sentence: {sentence}"
        )
        try:
            j = self.chat_json(system, user, temperature=0.0)
            tag = str(j.get("tag", "")).strip()
            conf = float(j.get("confidence", 0.5))
            why = str(j.get("rationale", "")).strip()
            allowed = {"PROC_ZN", "PROC_COAT_MAT", "ELECTROLYTE", "ASSEMBLY", "FORMATION_IN_SITU",
                       "CHAR", "ECHEM_TEST", "RESULT", "FIGREF", "INTERLAYER", "CATHODE", "OTHER"}
            if tag not in allowed:
                return None, 0.0, ""
            conf = max(0.0, min(1.0, conf))
            self.refine_calls += 1
            if self.cfg.llm_refine_cache:
                self._cache[s_key] = (tag, conf, why)
            return tag, conf, why
        except Exception:
            return None, 0.0, ""


# =============================================================================
# Sentence trimming (v7)
# =============================================================================

def looks_procedural_other(sent: str) -> bool:
    """
    v7: OTHER→DROP 폭을 줄이기 위한 안전장치
    - units+recipe verb
    - recipe verb + (layer anchor / step cue / common process cue)
    """
    t = sent.lower()
    if has_units(sent) and contains_any(t, RECIPE_VERBS):
        return True
    if contains_any(t, RECIPE_VERBS) and has_layer_anchor(sent):
        return True
    step_cues = ["then", "after", "before", "finally", "subsequently", "followed by", "prior to"]
    process_cues = ["room temperature", "rt", "overnight", "vacuum", "washed", "dried", "filtered", "centrifug", "evaporat", "kept", "maintained"]
    if contains_any(t, RECIPE_VERBS) and (any(c in t for c in step_cues) or any(c in t for c in process_cues)):
        return True
    return False


def tag_and_filter_paragraph(paragraph: str, cfg: ExtractConfig, llm: Optional[LLMClient] = None) -> Tuple[str, Dict[str, Any]]:
    """
    v9: sentence_features 저장 추가, is_proc_zn_sentence_v8 사용
    """
    debug: Dict[str, Any] = {"kept": [], "dropped": [], "tags": []}
    sents = split_sentences(paragraph)
    kept: List[str] = []
    proc_zn_count = 0
    proc_zn_high_conf_count = 0  # (S) Only count high-conf PROC_ZN for KPI
    tag_counts: Dict[str, int] = {}
    
    # (9) Cathode context detection
    has_cathode_context = any(x in paragraph.lower() for x in ["cathode", "positive electrode"])
    
    # (L) Separator/interlayer paragraph gate - v9: is_proc_zn_sentence_v8 사용
    has_separator_heavy = sum(1 for term in INTERLAYER_TERMS if term in paragraph.lower()) >= 2
    has_zn_recipe_in_para = any(is_proc_zn_sentence_v8(s, cfg)[0] == "TREAT" for s in sents)

    for sent in sents:
        tag, conf, flags = sentence_tag(sent, cfg)
        
        # v9: sentence_features 추출 (store_sentence_features 옵션이 켜져 있을 때)
        sent_features = {}
        if cfg.store_sentence_features:
            sent_features = extract_sentence_features(sent)
        
        # (9) Apply Cathode context override
        if tag == "PROC_COAT_MAT" and has_cathode_context and "zn" not in sent.lower():
            tag = "CATHODE"
            flags.append("context_override")
        
        # (L) Separator-heavy paragraph: drop non-Zn sentences
        if has_separator_heavy and not has_zn_recipe_in_para:
            if tag not in {"PROC_ZN"}:
                tag = "INTERLAYER"
                flags.append("separator_para_override")

        tag_counts[tag] = tag_counts.get(tag, 0) + 1

        # LLM refine (throttled)
        if cfg.llm_sentence_tagging and llm is not None and conf < 0.55:
            tag2, conf2, why = llm.refine_sentence_tag(sent, base_conf=conf)
            if tag2:
                tag = tag2
                conf = conf2
                if why:
                    flags = flags + ["llm_refined"]

        drop = False

        if tag == "FIGREF" and cfg.drop_tag_FIGREF:
            drop = True
        if tag == "CHAR" and cfg.drop_tag_CHAR:
            drop = True
        if tag == "RESULT" and cfg.drop_tag_RESULT:
            drop = True
        if tag == "ECHEM_TEST" and cfg.drop_tag_ECHEM_TEST:
            drop = True
        if tag == "INTERLAYER" and cfg.drop_tag_INTERLAYER:
            drop = True
        if tag == "CATHODE" and cfg.drop_tag_CATHODE:
            drop = True

        if tag == "OTHER":
            if looks_procedural_other(sent):
                drop = False
                tag = "PROC_MISC"
            else:
                drop = True
        
        # (D) ZN_USAGE filtering: drop if it contains result/performance content
        if tag == "ZN_USAGE":
            t_lower = sent.lower()
            # (K) Drop if it's a vendor/chemicals sentence
            vendor_words = ["purchased", "acquired", "obtained from", "without further purification", "as received"]
            if any(v in t_lower for v in vendor_words):
                drop = True
                flags.append("vendor_drop")
            # (D) Drop if contains performance units or result markers
            elif has_performance_units(sent) or any(m in t_lower for m in RESULT_MARKERS):
                drop = True
                flags.append("result_context_drop")
            else:
                drop = False  # Keep usage context otherwise
        
        # v9: soft_keep_mode - drop 대신 flag 부여하고 유지
        if cfg.soft_keep_mode and drop and tag in {"CHAR", "ECHEM_TEST"}:
            # 명확한 노이즈(FIGREF, RESULT)는 여전히 drop, 그 외는 soft-keep
            if tag not in {"FIGREF", "RESULT"}:
                drop = False
                flags.append("soft_kept")
                conf = max(0.2, conf - 0.3)  # 낮은 conf로 유지
        
        if not drop:
            if tag == "PROC_ZN":
                proc_zn_count += 1
                # (S) Only count high-confidence for stable KPI
                if conf >= 0.85:
                    proc_zn_high_conf_count += 1

            if cfg.output_tagged_sentences:
                kept.append(f"[{tag}] {sent.strip()}")
            else:
                kept.append(sent.strip())
            # v9: features 포함
            debug["kept"].append({
                "tag": tag, "conf": conf, "text": sent.strip(), 
                "flags": flags, "features": sent_features
            })
        else:
            debug["dropped"].append({
                "tag": tag, "conf": conf, "text": sent.strip(), 
                "flags": flags, "features": sent_features
            })

        debug["tags"].append({"tag": tag, "conf": conf, "flags": flags})

    debug["proc_zn_sentences"] = proc_zn_count
    debug["proc_zn_high_conf"] = proc_zn_high_conf_count  # (S) New metric
    debug["tag_counts"] = tag_counts
    cleaned = " ".join(kept).strip()
    return cleaned, debug


# =============================================================================
# Markdown document wrapper
# =============================================================================

@dataclass
class MarkdownDocument:
    doc_id: str
    path: Path
    lines: List[str]
    headings: List[Heading] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)

    @classmethod
    def load(cls, doc_id: str, path: Path) -> "MarkdownDocument":
        txt = path.read_text(encoding="utf-8", errors="ignore")
        lines = txt.splitlines()
        return cls(doc_id=doc_id, path=path, lines=lines)

    def parse(self) -> None:
        self.headings = detect_headings(self.lines)
        self.sections = build_sections(self.lines, self.headings)


# =============================================================================
# Heading selection logic (range-based)
# =============================================================================

def find_method_ranges(sections: List[Section]) -> List[Tuple[int, int]]:
    ranges: List[Tuple[int, int]] = []
    if not sections:
        return ranges

    nums: List[Optional[Tuple[int, ...]]] = []
    for sec in sections:
        nt, _ = parse_numeric_prefix(strip_heading_hashes(sec.heading.raw))
        nums.append(nt)

    i = 0
    while i < len(sections):
        sec = sections[i]
        if is_method_main_heading(sec.heading.title):
            start = i
            end = i + 1
            main_num = nums[i][0] if nums[i] and len(nums[i]) == 1 else None
            main_level = sec.heading.level
            while end < len(sections):
                if is_method_main_heading(sections[end].heading.title) and sections[end].heading.level <= main_level:
                    break
                # (I) Fixed: break if top-level number changes, regardless of tuple length
                if main_num is not None and nums[end]:
                    if nums[end][0] != main_num:
                        break
                else:
                    if sections[end].heading.level <= main_level and is_results_heading(sections[end].heading.title):
                        break
                end += 1
            ranges.append((start, end))
            i = end
        else:
            i += 1
    return ranges


def select_candidate_sections(doc: MarkdownDocument, cfg: ExtractConfig) -> List[int]:
    sections = doc.sections
    ranges = find_method_ranges(sections)
    candidate: List[int] = []

    if ranges:
        for start, end in ranges:
            candidate.append(start)
            for i in range(start + 1, end):
                cat = heading_category(sections[i].heading.title, cfg)
                if cat in {"synth_manuf", "materials_weak"}:
                    candidate.append(i)
                elif cfg.include_electrolyte and cat == "electrolyte":
                    candidate.append(i)
                elif cfg.include_cell_assembly and cat == "assembly":
                    candidate.append(i)

        seen = set()
        out = []
        for i in candidate:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out

    for i, sec in enumerate(sections):
        cat = heading_category(sec.heading.title, cfg)
        if cat in {"synth_manuf", "materials_weak"}:
            candidate.append(i)

    seen = set()
    out = []
    for i in candidate:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def build_heading_path(doc: MarkdownDocument, sec_idx: int) -> str:
    sec = doc.sections[sec_idx]
    h = sec.heading
    parts = [strip_heading_hashes(h.raw)]
    lvl = h.level
    j = sec_idx - 1
    while j >= 0:
        hj = doc.sections[j].heading
        if hj.level < lvl:
            parts.append(strip_heading_hashes(hj.raw))
            lvl = hj.level
        j -= 1
    parts.reverse()
    return " > ".join(parts)


def merge_short_paragraph_list(paras: List[str], threshold: int) -> List[str]:
    out: List[str] = []
    buff = ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if not buff:
            buff = p
            continue
        if len(buff) < threshold:
            buff = buff.rstrip() + " " + p
        else:
            out.append(buff)
            buff = p
    if buff:
        out.append(buff)
    return out


# =============================================================================
# Results fallback mining
# =============================================================================

def mine_recipe_headings_global(doc: MarkdownDocument, cfg: ExtractConfig) -> List[Chunk]:
    """
    v9: CHAR/RESULT/ECHEM_TEST 태그 명시적 제외로 누출 방지
    """
    chunks: List[Chunk] = []
    for idx, sec in enumerate(doc.sections):
        title = sec.heading.title
        cat = heading_category(title, cfg)
        if cat not in {"synth_manuf", "materials_weak", "electrolyte", "assembly"}:
            continue
        if is_results_heading(title):
            continue

        content_lines = preprocess_section_lines(sec.raw_lines[1:], cfg=cfg)
        paras = split_markdown_into_paragraphs(content_lines)

        sents: List[str] = []
        for p in paras:
            for s in split_sentences(p):
                tag, _, _ = sentence_tag(s, cfg)
                # v9: CHAR/RESULT/ECHEM_TEST 명시적 제외 (누출 방지)
                if tag in {"CHAR", "RESULT", "ECHEM_TEST", "FIGREF", "INTERLAYER", "CATHODE"}:
                    continue
                if tag in {"PROC_ZN", "PROC_COAT_MAT", "ELECTROLYTE", "ASSEMBLY", "FORMATION_IN_SITU"}:
                    sents.append(f"[{tag}] {s.strip()}")

        if not sents:
            continue

        sents = sents[: min(len(sents), cfg.fallback_global_max_sentences // 2)]
        text = " ".join(sents)
        heading_path = build_heading_path(doc, idx)
        chunks.append(
            Chunk(
                doc_id=doc.doc_id,
                source_path=str(doc.path),
                heading=sec.heading.raw,
                heading_path=heading_path + " > (fallback_heading_mining)",
                category="fallback_heading_mining",
                score=3.5,
                text=text,
                start_line=sec.start_line,
                end_line=sec.end_line,
                provenance=PROVENANCE_FALLBACK_HEADING,  # v9
                debug={"mined_sentences": len(sents)} if cfg.debug else {},
            )
        )
    return chunks


def mine_recipe_sentences_global(doc: MarkdownDocument, cfg: ExtractConfig) -> List[Chunk]:
    lines = preprocess_section_lines(doc.lines, cfg=cfg)

    cut = None
    for i, ln in enumerate(lines):
        if is_heading_like_line(ln) and "references" in normalize_heading_title(strip_heading_hashes(ln)):
            cut = i
            break
    if cut is not None:
        lines = lines[:cut]

    paras = split_markdown_into_paragraphs(lines)
    mined_blocks: List[str] = []
    count = 0

    for p in paras:
        sents = split_sentences(p)
        for si, s in enumerate(sents):
            tag, _, _ = sentence_tag(s, cfg)
            if tag == "PROC_ZN" or (tag in {"PROC_COAT_MAT", "ELECTROLYTE"} and (has_units(s) or has_layer_anchor(s))):
                start = max(0, si - cfg.fallback_context_sentences)
                end = min(len(sents), si + cfg.fallback_context_sentences + 1)
                ctx = []
                for sj in range(start, end):
                    tagj, _, _ = sentence_tag(sents[sj], cfg)
                    if tagj in {"RESULT", "CHAR", "FIGREF", "INTERLAYER", "CATHODE"}:
                        continue
                    ctx.append(f"[{tagj}] {sents[sj].strip()}")
                block = " ".join(ctx).strip()
                if block:
                    mined_blocks.append(block)
                    count += 1
                    if count >= cfg.fallback_global_max_sentences:
                        break
        if count >= cfg.fallback_global_max_sentences:
            break

    if not mined_blocks:
        return []

    text = "\n".join(f"- {b}" for b in mined_blocks[: cfg.fallback_global_max_sentences])
    return [
        Chunk(
            doc_id=doc.doc_id,
            source_path=str(doc.path),
            heading="(document-wide)",
            heading_path="(fallback_sentence_mining)",
            category="fallback_sentence_mining",
            score=4.0,
            text=text,
            start_line=0,
            end_line=len(doc.lines),
            provenance=PROVENANCE_FALLBACK_SENTENCE,  # v9
            debug={"mined_blocks": len(mined_blocks)} if cfg.debug else {},
        )
    ]


# =============================================================================
# Extraction core (v8)
# =============================================================================

def detect_doc_signals(all_text: str, kept_sentences: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    v8: Detect document-level signals for LLM judgment support
    """
    signals = {
        "aqueous_flag": False,
        "aqueous_evidence": [],
        "lab_scale_flag": False,
        "lab_scale_evidence": [],
        "time_axis_flag": False,
        "time_axis_evidence": [],
    }
    
    t = all_text.lower()
    
    # Aqueous signals
    if has_aqueous_signal(all_text):
        signals["aqueous_flag"] = True
        # Find evidence sentences
        for item in kept_sentences[:50]:  # Limit search
            if has_aqueous_signal(item.get("text", "")):
                signals["aqueous_evidence"].append(item.get("text", "")[:150])
                if len(signals["aqueous_evidence"]) >= 3:
                    break
    
    # Lab-scale signals
    if has_lab_scale_signal(all_text):
        signals["lab_scale_flag"] = True
        for item in kept_sentences[:50]:
            if has_lab_scale_signal(item.get("text", "")):
                signals["lab_scale_evidence"].append(item.get("text", "")[:150])
                if len(signals["lab_scale_evidence"]) >= 3:
                    break
    
    # Time-axis (pre-cycling) signals
    if has_time_axis_signal(all_text):
        signals["time_axis_flag"] = True
        for item in kept_sentences[:50]:
            if has_time_axis_signal(item.get("text", "")):
                signals["time_axis_evidence"].append(item.get("text", "")[:150])
                if len(signals["time_axis_evidence"]) >= 3:
                    break
    
    # v9: Separate pre-cycling vs during-cycling signals
    signals["pre_cycling_flag"] = False
    signals["during_cycling_flag"] = False
    pre_patterns = ["before cycling", "prior to cycling", "pre-cycling", "pre cycling",
                    "before assembly", "pretreated", "pre-treated", "precoated", "pre-coated"]
    during_patterns = ["during cycling", "in-situ form", "in situ form", "formed during cycling",
                       "electrochemically form", "in first cycle", "plating/stripping"]
    
    for item in kept_sentences[:80]:
        txt = item.get("text", "").lower()
        if not signals["pre_cycling_flag"]:
            if any(p in txt for p in pre_patterns):
                signals["pre_cycling_flag"] = True
        if not signals["during_cycling_flag"]:
            if any(p in txt for p in during_patterns):
                signals["during_cycling_flag"] = True
    
    # v9: mixed_path_flag (both pre and during present)
    signals["mixed_path_flag"] = signals["pre_cycling_flag"] and signals["during_cycling_flag"]
    
    # v9: has_echem_results_flag - detect if experimental data exists
    signals["has_echem_results_flag"] = False
    signals["echem_results_snippets"] = []
    echem_patterns = ["cycle", "mah", "capacity", "coulombic", "retention", 
                      "current density", "overpotential", "impedance"]
    for item in kept_sentences[:50]:
        txt = item.get("text", "").lower()
        if any(p in txt for p in echem_patterns) and any(c.isdigit() for c in txt):
            signals["has_echem_results_flag"] = True
            signals["echem_results_snippets"].append(item.get("text", "")[:120])
            if len(signals["echem_results_snippets"]) >= 3:
                break
    
    return signals


def build_evidence_pack(
    tag_totals: Dict[str, int],
    kept_sentences: List[Dict[str, Any]],
    dropped_sentences: List[Dict[str, Any]],
    doc_signals: Dict[str, Any],
    cfg: ExtractConfig
) -> Dict[str, Any]:
    """
    v8: Build structured evidence pack for LLM consumption
    """
    topk = cfg.evidence_pack_topk
    
    # Group sentences by tag
    by_tag: Dict[str, List[Dict[str, Any]]] = {}
    for item in kept_sentences:
        tag = item.get("tag", "OTHER")
        if tag not in by_tag:
            by_tag[tag] = []
        by_tag[tag].append(item)
    
    # Build evidence pack
    pack: Dict[str, Any] = {
        "tag_counts": dict(tag_totals),
        "proc_zn_topk": [],
        "proc_coat_topk": [],
        "electrolyte_topk": [],
        "formation_topk": [],
        "assembly_topk": [],
    }
    
    # Extract top-k for key tags
    for item in by_tag.get("PROC_ZN", [])[:topk]:
        pack["proc_zn_topk"].append({
            "text": item.get("text", "")[:250],
            "conf": item.get("conf", 0),
            "flags": item.get("flags", []),
            "features": item.get("features", {}),
        })
    
    for item in by_tag.get("PROC_COAT_MAT", [])[:topk]:
        pack["proc_coat_topk"].append({
            "text": item.get("text", "")[:250],
            "conf": item.get("conf", 0),
            "flags": item.get("flags", []),
        })
    
    for item in by_tag.get("ELECTROLYTE", [])[:topk]:
        pack["electrolyte_topk"].append({
            "text": item.get("text", "")[:250],
            "conf": item.get("conf", 0),
        })
    
    for item in by_tag.get("FORMATION_IN_SITU", [])[:topk]:
        pack["formation_topk"].append({
            "text": item.get("text", "")[:250],
            "conf": item.get("conf", 0),
        })
    
    for item in by_tag.get("ASSEMBLY", [])[:topk]:
        pack["assembly_topk"].append({
            "text": item.get("text", "")[:200],
            "conf": item.get("conf", 0),
        })
    
    # Add document signals
    pack["signals"] = doc_signals
    
    # Side-channel: dropped snippets (for reference)
    if cfg.store_dropped_snippets:
        pack["dropped_result_snippets"] = []
        pack["dropped_echem_snippets"] = []
        pack["dropped_interlayer_snippets"] = []
        pack["dropped_cathode_snippets"] = []
        
        for item in dropped_sentences[:30]:
            tag = item.get("tag", "")
            text = item.get("text", "")[:150]
            if tag == "RESULT" and len(pack["dropped_result_snippets"]) < 5:
                pack["dropped_result_snippets"].append(text)
            elif tag == "ECHEM_TEST" and len(pack["dropped_echem_snippets"]) < 5:
                pack["dropped_echem_snippets"].append(text)
            elif tag == "INTERLAYER" and len(pack["dropped_interlayer_snippets"]) < 3:
                pack["dropped_interlayer_snippets"].append(text)
            elif tag == "CATHODE" and len(pack["dropped_cathode_snippets"]) < 3:
                pack["dropped_cathode_snippets"].append(text)
    
    return pack


def extract_doc_flags_from_stats(
    tag_totals: Dict[str, int], 
    kept_sentences_dump: List[Dict[str, Any]],
    dropped_sentences_dump: List[Dict[str, Any]] = None,
    all_doc_text: str = "",
    cfg: ExtractConfig = None
) -> Dict[str, Any]:
    """
    v8: Enhanced document-level flags with new signals
    """
    proc_zn_count = int(tag_totals.get("PROC_ZN", 0))
    proc_coat_count = int(tag_totals.get("PROC_COAT_MAT", 0))
    formation_count = int(tag_totals.get("FORMATION_IN_SITU", 0))
    interlayer_count = int(tag_totals.get("INTERLAYER", 0))
    zn_usage_count = int(tag_totals.get("ZN_USAGE", 0))
    salt_like_count = sum(1 for x in kept_sentences_dump if "salt_like" in x.get("flags", []))

    electrolyte_additive_flag = False
    additive_formation_flag = False
    for x in kept_sentences_dump:
        if x.get("tag") == "ELECTROLYTE":
            s = str(x.get("text", ""))
            if electrolyte_has_additive(s):
                electrolyte_additive_flag = True
            if electrolyte_additive_for_formation(s):
                additive_formation_flag = True

    # interlayer_flag: dominance ratio
    interlayer_flag = (interlayer_count >= 2 and 
                       interlayer_count >= 3 * (proc_zn_count + proc_coat_count))

    # mixed_flag
    mixed_flag = (proc_zn_count >= 1) and (formation_count >= 1 or additive_formation_flag)
    
    # v8: Document signals
    doc_signals = {}
    if all_doc_text:
        doc_signals = detect_doc_signals(all_doc_text, kept_sentences_dump)

    flags = {
        "proc_zn_count": proc_zn_count,
        "proc_coat_count": proc_coat_count,
        "formation_count": formation_count,
        "zn_usage_count": zn_usage_count,
        "salt_like_count": salt_like_count,
        "electrolyte_additive_flag": bool(electrolyte_additive_flag),
        "additive_formation_flag": bool(additive_formation_flag),
        "interlayer_flag": bool(interlayer_flag),
        "mixed_flag": bool(mixed_flag),
        **doc_signals,
    }
    
    # v8: Build evidence pack if enabled
    if cfg and cfg.generate_evidence_pack:
        dropped = dropped_sentences_dump or []
        flags["evidence_pack"] = build_evidence_pack(
            tag_totals, kept_sentences_dump, dropped, doc_signals, cfg
        )
    
    return flags



def extract_chunks_from_markdown(doc: MarkdownDocument, cfg: ExtractConfig) -> Dict[str, Any]:
    llm = LLMClient(cfg) if cfg.llm_backend.lower() == "ollama" else None
    if llm:
        llm.calls = 0
        llm.refine_calls = 0

    doc.parse()
    candidate_idxs = select_candidate_sections(doc, cfg)

    chunks: List[Chunk] = []
    kept_paragraphs = 0
    dropped_paragraphs = 0
    proc_zn_sentences_total = 0
    kept_sentences_total = 0
    tag_totals: Dict[str, int] = {}
    kept_sentence_dump_for_flags: List[Dict[str, Any]] = []
    dropped_sentence_dump_for_flags: List[Dict[str, Any]] = []  # v9: dropped 문장 전역 수집

    method_ranges = find_method_ranges(doc.sections)
    method_ranges_found = bool(method_ranges)

    for idx in candidate_idxs:
        sec = doc.sections[idx]
        cat = heading_category(sec.heading.title, cfg)

        raw = sec.raw_lines
        content_lines = raw[1:] if len(raw) >= 2 else []
        content_lines = preprocess_section_lines(content_lines, cfg=cfg)

        paras = split_markdown_into_paragraphs(content_lines)

        if cat == "method_main" and not paras:
            joined = preprocess_section_lines(raw, cfg=cfg)
            ptxt = normalize_ws(" ".join(joined[1:])) if len(joined) > 1 else ""
            if ptxt:
                paras = [ptxt]

        if cfg.merge_short_paragraphs:
            paras = merge_short_paragraph_list(paras, cfg.merge_short_threshold_chars)

        kept_for_section: List[Tuple[str, float, Dict[str, Any]]] = []
        for p in paras:
            p = p.strip()
            if not p:
                continue
            if cfg.drop_garbage_paragraphs and paragraph_is_mostly_garbage(p):
                dropped_paragraphs += 1
                continue
            if len(p) < cfg.min_paragraph_chars:
                continue

            if cat == "materials_weak" and looks_like_pure_reagent_list(p):
                sc, _ = recipe_score_paragraph(p)
                if sc < 2.0:
                    dropped_paragraphs += 1
                    continue

            sc, sc_dbg = recipe_score_paragraph(p)

            if paragraph_is_characterization(p) and sc < 2.8:
                dropped_paragraphs += 1
                continue
            if paragraph_is_echem_testing(p) and sc < 3.2:
                dropped_paragraphs += 1
                continue

            if cfg.enable_sentence_tagging:
                cleaned, tag_dbg = tag_and_filter_paragraph(p, cfg, llm=llm)

                proc_zn_sentences_total += int(tag_dbg.get("proc_zn_sentences", 0))
                counts = tag_dbg.get("tag_counts", {})
                for k, v in counts.items():
                    tag_totals[k] = tag_totals.get(k, 0) + int(v)
                kept_sentences_total += len(tag_dbg.get("kept", []))

                # 문서단 flag 계산용 dump(kept 문장만)
                for it in tag_dbg.get("kept", []):
                    kept_sentence_dump_for_flags.append(it)
                # v9: dropped 문장도 수집
                for it in tag_dbg.get("dropped", []):
                    dropped_sentence_dump_for_flags.append(it)

                if cleaned.strip():
                    kept_for_section.append(
                        (truncate_long_paragraph(cleaned, cfg.max_paragraph_chars), sc, {"score_dbg": sc_dbg, "tag_dbg": tag_dbg})
                    )
                    kept_paragraphs += 1
                else:
                    dropped_paragraphs += 1
            else:
                kept_for_section.append((truncate_long_paragraph(p, cfg.max_paragraph_chars), sc, {"score_dbg": sc_dbg}))
                kept_paragraphs += 1

        heading_path = build_heading_path(doc, idx)
        if kept_for_section:
            merge_block = (cat in {"method_main", "synth_manuf", "electrolyte", "assembly"} and len(kept_for_section) >= 2)
            if merge_block:
                merged_text = "\n\n".join(t for t, _, _ in kept_for_section)
                merged_score = sum(sc for _, sc, _ in kept_for_section) / max(1, len(kept_for_section))
                merged_debug = {"paragraph_scores": [sc for _, sc, _ in kept_for_section]}
                chunks.append(
                    Chunk(
                        doc_id=doc.doc_id,
                        source_path=str(doc.path),
                        heading=sec.heading.raw,
                        heading_path=heading_path,
                        category=cat,
                        score=merged_score,
                        text=merged_text,
                        start_line=sec.start_line,
                        end_line=sec.end_line,
                        debug=merged_debug if cfg.debug else {},
                    )
                )
            else:
                for p_text, p_score, p_dbg in kept_for_section:
                    chunks.append(
                        Chunk(
                            doc_id=doc.doc_id,
                            source_path=str(doc.path),
                            heading=sec.heading.raw,
                            heading_path=heading_path,
                            category=cat,
                            score=p_score,
                            text=p_text,
                            start_line=sec.start_line,
                            end_line=sec.end_line,
                            debug=p_dbg if cfg.debug else {},
                        )
                    )

    # Results fallback
    fallback_used = False
    fallback_reason = ""
    fallback_chunks: List[Chunk] = []

    if cfg.enable_results_fallback:
        need_fallback = False
        if not method_ranges_found:
            need_fallback = True
            fallback_reason = "no_method_section_detected"
        elif proc_zn_sentences_total < cfg.fallback_min_proc_zn_sentences:
            need_fallback = True
            fallback_reason = f"proc_zn_sentences<{cfg.fallback_min_proc_zn_sentences}"
        elif kept_sentences_total <= 2:
            need_fallback = True
            fallback_reason = "too_few_kept_sentences"

        if need_fallback:
            fallback_used = True
            if cfg.fallback_allow_heading_mining:
                fallback_chunks.extend(mine_recipe_headings_global(doc, cfg))
            if cfg.fallback_allow_sentence_mining:
                fallback_chunks.extend(mine_recipe_sentences_global(doc, cfg))

    # De-duplicate chunks
    seen_keys: Set[Tuple[str, str, str]] = set()
    final_chunks: List[Chunk] = []
    for ch in chunks + fallback_chunks:
        key = (ch.heading_path, ch.category, ch.text.strip())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        final_chunks.append(ch)

    # Supplementary scoring
    supp_strength = 0.0
    supp_evidence: List[str] = []
    supp_score = 0.0
    supp_reason = ""
    supp_llm_reason = ""
    supp_candidates: List[str] = []

    if cfg.enable_supp_scoring:
        # (14) Scan WHOLE cleaned document for SI pointers, not just chunks
        # We need a quick clean of all lines
        all_doc_text = " ".join(preprocess_section_lines(doc.lines, cfg=cfg))
        supp_strength, supp_evidence = detect_supp_pointer_evidence(all_doc_text)
        
        supp_score, supp_reason = compute_supp_importance(
            cfg=cfg,
            si_strength=supp_strength,
            si_evidence=supp_evidence,
            method_ranges_found=method_ranges_found,
            proc_zn_sentences=proc_zn_sentences_total,
            kept_sentences=kept_sentences_total,
        )
        if cfg.auto_find_supp_candidates and supp_score >= cfg.supp_flag_threshold and cfg.root_dir is not None:
            supp_candidates = find_supp_candidates(cfg.root_dir, doc.doc_id)


    # v9: Compute provenance counts for LLM trust weighting
    provenance_counts = {
        "PRIMARY": 0,
        "FALLBACK_HEADING": 0,
        "FALLBACK_SENTENCE": 0,
    }
    for ch in final_chunks:
        prov = ch.provenance
        if prov in provenance_counts:
            provenance_counts[prov] += 1
        else:
            provenance_counts[prov] = 1

    # v9: doc-level flags with all_doc_text for signal detection (dropped 문장도 전달)
    all_doc_text = " ".join(preprocess_section_lines(doc.lines, cfg=cfg))
    doc_flags = extract_doc_flags_from_stats(
        tag_totals, 
        kept_sentence_dump_for_flags,
        dropped_sentences_dump=dropped_sentence_dump_for_flags,  # v9: dropped 전달
        all_doc_text=all_doc_text,
        cfg=cfg
    )

    rec: Dict[str, Any] = {
        "doc_id": doc.doc_id,
        "md_path": str(doc.path),
        "selected_headings": [doc.sections[i].heading.raw for i in candidate_idxs],
        "chunks": [c.to_dict() for c in final_chunks],
        "stats": {
            # v9: extractor version 추가
            "extractor_version": EXTRACTOR_VERSION,
            "candidate_headings": len(candidate_idxs),
            "chunks": len(final_chunks),
            "kept_paragraphs": kept_paragraphs,
            "dropped_paragraphs": dropped_paragraphs,
            "proc_zn_sentences": proc_zn_sentences_total,
            "kept_sentences": kept_sentences_total,
            "tag_totals": tag_totals,
            "method_ranges_found": method_ranges_found,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            # v9: provenance counts
            "provenance_counts": provenance_counts,
            # Supplementary info
            "supp_pointer_strength": supp_strength,
            "supp_pointer_evidence": supp_evidence,
            "supp_importance": supp_score,
            "supp_reason": supp_reason,
            "supp_llm_reason": supp_llm_reason,
            "supp_candidates": supp_candidates,
            # v7/v9: flags
            **doc_flags,
            # LLM stats
            "llm_calls_used": getattr(llm, "calls", 0) if llm is not None else 0,
            "llm_refine_calls_used": getattr(llm, "refine_calls", 0) if llm is not None else 0,
        },
    }
    return rec


# =============================================================================
# Batch processing / IO
# =============================================================================

def load_headers_json(path: Path) -> Dict[str, List[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[str, List[str]] = {}
    for k, v in data.items():
        if isinstance(v, list):
            out[k] = [str(x) for x in v]
    return out


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def dedupe_preserve(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def select_headings_from_header_lines(headings: List[str], cfg: ExtractConfig) -> List[str]:
    selected: List[str] = []
    parsed: List[Tuple[Optional[Tuple[int, ...]], str, str]] = []
    for h in headings:
        title = strip_heading_hashes(h)
        nt, rest = parse_numeric_prefix(title)
        parsed.append((nt, rest, h))

    method_idx = None
    method_topnum = None
    for i, (nt, rest, raw) in enumerate(parsed):
        if rest and is_method_main_heading(rest):
            method_idx = i
            method_topnum = nt[0] if nt and len(nt) == 1 else None
            break

    if method_idx is None:
        for nt, rest, raw in parsed:
            if rest and (is_recipe_heading(rest) or is_weak_material_heading(rest)):
                selected.append(raw)
        return dedupe_preserve(selected)

    selected.append(parsed[method_idx][2])
    for j in range(method_idx + 1, len(parsed)):
        nt, rest, raw = parsed[j]
        if method_topnum is not None and nt and len(nt) == 1 and nt[0] != method_topnum:
            break
        if rest and (is_recipe_heading(rest) or is_weak_material_heading(rest)):
            selected.append(raw)
        if cfg.include_electrolyte and rest and "electrolyte" in normalize_heading_title(rest):
            selected.append(raw)
        if cfg.include_cell_assembly and rest and "assembly" in normalize_heading_title(rest):
            selected.append(raw)

    return dedupe_preserve(selected)


def run_headings_only(cfg: ExtractConfig) -> None:
    assert cfg.headers_json is not None and cfg.out_json is not None
    headers = load_headers_json(cfg.headers_json)

    out: Dict[str, List[str]] = {}
    for doc_id, headings in headers.items():
        selected = select_headings_from_header_lines(headings, cfg)
        out[doc_id] = selected

    write_json(cfg.out_json, out)
    LOGGER.info("Saved: %s", cfg.out_json)


def run_chunks(cfg: ExtractConfig) -> None:
    assert cfg.headers_json is not None and cfg.root_dir is not None and cfg.out_json is not None

    headers = load_headers_json(cfg.headers_json)
    doc_ids = sorted(headers.keys())

    out: Dict[str, Any] = {}
    jsonl_records: List[Dict[str, Any]] = []

    missing = 0
    it = tqdm(list(enumerate(doc_ids, 1)), total=len(doc_ids), desc="Extracting", unit="doc")
    for k, doc_id in it:
        md_path = find_marker_md_file(cfg.root_dir, doc_id)
        if md_path is None:
            missing += 1
            if cfg.debug:
                LOGGER.warning("Missing md for doc_id=%s", doc_id)
            continue

        try:
            doc = MarkdownDocument.load(doc_id, md_path)
            rec = extract_chunks_from_markdown(doc, cfg)
            out[doc_id] = rec

            if cfg.out_jsonl:
                for ch in rec["chunks"]:
                    flat = dict(ch)
                    flat["doc_id"] = doc_id
                    flat["md_path"] = rec.get("md_path", "")
                    stats = rec.get("stats", {})
                    flat["supp_importance"] = stats.get("supp_importance", 0.0)
                    flat["mixed_flag"] = stats.get("mixed_flag", False)
                    flat["interlayer_flag"] = stats.get("interlayer_flag", False)
                    jsonl_records.append(flat)

        except Exception as e:
            LOGGER.exception("Failed processing %s: %s", doc_id, e)

        if k % 50 == 0:
            LOGGER.info("Processed %d/%d (missing=%d)", k, len(doc_ids), missing)

    write_json(cfg.out_json, out)
    LOGGER.info("Saved chunks JSON: %s", cfg.out_json)

    if cfg.out_jsonl and jsonl_records:
        write_jsonl(cfg.out_jsonl, jsonl_records)
        LOGGER.info("Saved chunks JSONL: %s", cfg.out_jsonl)


# =============================================================================
# CLI
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AZIB Experimental Chunk Extractor (Recipe-first) v7")
    p.add_argument("--mode", choices=["headings_only", "chunks"], default="chunks", help="Output mode")
    p.add_argument("--root_dir", type=str, default=None, help="marker-pdf output root directory (required for chunks)")
    p.add_argument("--headers_json", type=str, required=True, help="01_headers_summary.json")
    p.add_argument("--out_json", type=str, required=True, help="output JSON path")
    p.add_argument("--out_jsonl", type=str, default=None, help="optional output JSONL path (chunks mode)")

    # rubric controls
    p.add_argument("--include_cell_assembly", action="store_true")
    p.add_argument("--no_include_cell_assembly", action="store_true")
    p.add_argument("--include_electrolyte", action="store_true")
    p.add_argument("--no_include_electrolyte", action="store_true")
    p.add_argument("--include_weak_materials", action="store_true")
    p.add_argument("--no_include_weak_materials", action="store_true")

    # v7 cleanup toggles
    p.add_argument("--remove_bracket_citations", action="store_true", help="Remove numeric-only bracket citations like [12,34] (default ON)")
    p.add_argument("--keep_bracket_citations", action="store_true", help="Do NOT remove [12,34] citations")
    p.add_argument("--remove_parenthetical_citations", action="store_true", help="Remove numeric-only parenthetical citations like (12,34)")
    p.add_argument("--keep_linked_citations", action="store_true", help="Keep linked citations like [[44,45]](#page...)")
    p.add_argument("--keep_parenthetical_figrefs", action="store_true", help="Keep (Fig. S1) style parenthetical refs")
    p.add_argument("--strip_in_sentence_figrefs", action="store_true", help="Remove Fig refs text from sentence but keep sentence")
    p.add_argument("--drop_caption_blocks", action="store_true", help="Drop Fig/Table caption blocks (default ON)")
    p.add_argument("--keep_caption_blocks", action="store_true", help="Keep caption blocks")
    p.add_argument("--drop_fig_table_ref_sentences", action="store_true", help="Drop sentences that reference Fig/Table/Scheme/Eq (default ON)")
    p.add_argument("--keep_fig_table_ref_sentences", action="store_true", help="Keep Fig/Table reference sentences")


    # results fallback
    p.add_argument("--disable_results_fallback", action="store_true")
    p.add_argument("--fallback_min_proc_zn_sentences", type=int, default=1)

    # supplementary scoring
    p.add_argument("--disable_supp_scoring", action="store_true")
    p.add_argument("--auto_find_supp_candidates", action="store_true")
    p.add_argument("--supp_flag_threshold", type=float, default=0.65)

    # LLM (ollama)
    p.add_argument("--llm_backend", choices=["none", "ollama"], default="none")
    p.add_argument("--llm_model", type=str, default="qwen2.5:14b-instruct")
    p.add_argument("--ollama_url", type=str, default="http://localhost:11434")
    p.add_argument("--llm_timeout", type=int, default=120)
    p.add_argument("--llm_max_calls_per_doc", type=int, default=8)
    p.add_argument("--llm_supp_scoring", action="store_true")
    p.add_argument("--llm_sentence_tagging", action="store_true")
    p.add_argument("--llm_paragraph_repair", action="store_true")
    p.add_argument("--llm_debug", action="store_true")

    # v7: LLM throttling
    p.add_argument("--llm_refine_max_per_doc", type=int, default=3, help="Max sentence-tag refine calls per doc (default 3)")
    p.add_argument("--no_llm_refine_cache", action="store_true", help="Disable sentence refine cache")

    # general
    p.add_argument("--debug", action="store_true")
    p.add_argument("--log_level", type=str, default="INFO")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    cfg = ExtractConfig()
    cfg.mode = args.mode
    cfg.headers_json = Path(args.headers_json)
    cfg.out_json = Path(args.out_json)
    cfg.out_jsonl = Path(args.out_jsonl) if args.out_jsonl else None
    cfg.root_dir = Path(args.root_dir) if args.root_dir else None

    # rubric toggles
    cfg.include_cell_assembly = args.include_cell_assembly or (not args.no_include_cell_assembly)
    cfg.include_electrolyte = args.include_electrolyte or (not args.no_include_electrolyte)
    cfg.include_weak_materials = args.include_weak_materials or (not args.no_include_weak_materials)

    cfg.enable_results_fallback = not args.disable_results_fallback
    cfg.fallback_min_proc_zn_sentences = int(args.fallback_min_proc_zn_sentences)

    cfg.enable_supp_scoring = not args.disable_supp_scoring
    cfg.auto_find_supp_candidates = bool(args.auto_find_supp_candidates)
    cfg.supp_flag_threshold = float(args.supp_flag_threshold)

    # v7 cleanup defaults ON unless explicitly kept
    cfg.remove_bracket_citations = True
    if args.keep_bracket_citations:
        cfg.remove_bracket_citations = False
    if args.remove_bracket_citations:
        cfg.remove_bracket_citations = True

    cfg.remove_parenthetical_citations = bool(args.remove_parenthetical_citations)

    cfg.remove_linked_citations = not bool(args.keep_linked_citations)
    cfg.remove_parenthetical_figrefs = not bool(args.keep_parenthetical_figrefs)

    cfg.drop_caption_blocks = True
    if args.keep_caption_blocks:
        cfg.drop_caption_blocks = False
    if args.drop_caption_blocks:
        cfg.drop_caption_blocks = True

    cfg.drop_fig_table_ref_sentences = True
    if args.keep_fig_table_ref_sentences:
        cfg.drop_fig_table_ref_sentences = False
    if args.drop_fig_table_ref_sentences:
        cfg.drop_fig_table_ref_sentences = True
        
    cfg.strip_in_sentence_figrefs = bool(args.strip_in_sentence_figrefs)


    # LLM
    cfg.llm_backend = args.llm_backend
    cfg.llm_model = args.llm_model
    cfg.ollama_url = args.ollama_url
    cfg.llm_timeout = int(args.llm_timeout)
    cfg.llm_max_calls_per_doc = int(args.llm_max_calls_per_doc)
    cfg.llm_supp_scoring = bool(args.llm_supp_scoring)
    cfg.llm_sentence_tagging = bool(args.llm_sentence_tagging)
    cfg.llm_paragraph_repair = bool(args.llm_paragraph_repair)
    cfg.llm_debug = bool(args.llm_debug)

    cfg.llm_refine_max_per_doc = int(args.llm_refine_max_per_doc)
    cfg.llm_refine_cache = not bool(args.no_llm_refine_cache)

    # misc
    cfg.debug = bool(args.debug)
    cfg.log_level = args.log_level

    setup_logging(cfg.log_level)

    if cfg.mode == "chunks" and cfg.root_dir is None:
        LOGGER.error("--root_dir is required for mode=chunks")
        return 2

    LOGGER.info(
        "Mode=%s | LLM=%s(%s) | drop_caption=%s | drop_figref_sent=%s | rm_[12]=%s | rm_linked_cit=%s | rm_paren_figref=%s",
        cfg.mode, cfg.llm_backend, cfg.llm_model,
        cfg.drop_caption_blocks, cfg.drop_fig_table_ref_sentences, cfg.remove_bracket_citations,
        cfg.remove_linked_citations, cfg.remove_parenthetical_figrefs
    )

    if cfg.mode == "headings_only":
        run_headings_only(cfg)
        return 0
    run_chunks(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
