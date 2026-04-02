from __future__ import annotations

import argparse
import csv
import json
import hashlib
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from tqdm import tqdm


# ==============================================================================
# DEFAULT PATHS
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_JSON = BASE_DIR / "01_run_out_v2" / "grobid_results_all.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "04_llm_classification"


# ==============================================================================
# STAGE 2: PARAGRAPH-LEVEL PROMPT (unchanged)
# ==============================================================================
PARAGRAPH_VERIFY_PROMPT = """You are an expert in mining aqueous zinc-ion battery (AZIB) papers.
Your task: decide whether a PARAGRAPH contains RECIPE / PREPARATION evidence relevant to:

(A) Zn ANODE protective layers formed EX-SITU (pre-cycling coatings / artificial interphases / engineered interfaces), OR
(B) AZIB cell/electrode assembly or electrolyte preparation that is NOT cathode-only.

IMPORTANT: We optimize for RECIPE / PREPARATION evidence, not results.

========================
NORMALIZATION
- Treat text case-insensitively.
- Ignore punctuation/noise; focus on key terms and actionable steps.

========================
DECISION PRIORITY (apply in this order)
1) EXPLICIT Zn-COATING OVERRIDE-YES (highest priority)
2) HARD-NO (override) rules
3) HARD-YES rules
4) Otherwise default NO

========================
KEY DEFINITIONS (signals)

[FABRICATION / PREPARATION VERBS] ✅
Any of:
- preparation, fabricate, synthesis, construct, assembly
- coating, coat, spin-coat, doctor-blade, cast, dip-coat, spray
- deposition, deposit, sputter, grow, form on
- soaking, dipping, immersion, immersed
- drying, annealing, calcination, curing
- polishing, cleaning, washing (supportive signals)

[Zn-SURFACE / ANODE ANCHORS] ✅ (strong)
Any of:
- Zn, zinc, Zn foil, Zn anode, anode, negative electrode
- @Zn, Zn@, coated Zn, modified Zn, treated Zn
- layer on Zn, grown on Zn, deposited on Zn, coated on Zn

[ASSEMBLY / CELL ANCHORS] ✅
Any of:
- cell assembly, battery assembly, coin cell, CR2032
- separator, electrolyte, full cell, symmetric cell

[COATING-MATERIAL SIGNALS] ✅ (allowed but not sufficient)
Examples:
- MOF, COF, zeolite, ZIF, MIL, UiO
- PVDF, PVA, PAA, alginate, chitosan, cellulose, PEO/PEG, hydrogel, film, membrane
- oxides / interphase layers used for coatings (e.g., IGZO, TiO2, Al2O3 etc.)
NOTE: Material keywords alone do NOT guarantee YES.

========================
0) EXPLICIT Zn-COATING OVERRIDE-YES ✅ (HIGHEST PRIORITY)
Even if cathode materials are mentioned, answer YES if the paragraph EXPLICITLY states that
a material/layer is fabricated ON Zn (anode) surface AND includes recipe details.

Must have BOTH:
(a) Zn-SURFACE / ANODE ANCHORS present
AND
(b) FABRICATION / PREPARATION VERBS clearly applied to Zn surface
PLUS at least one recipe detail (amount, time, temperature, voltage/current, pressure, sccm, concentration, solvent).

Examples (paraphrased):
- "X slurry was spin-coated on Zn foil and dried..."
- "IGZO was sputtered on Zn..."
- "X grew on Zn under ..."

========================
1) HARD-NO (OVERRIDE) RULES ❌

A) STRICT CATHODE-ONLY ENFORCEMENT (with override exception)
Answer NO if the paragraph is ONLY about cathode (positive electrode) material/electrode preparation
AND it does NOT include ANY Zn-SURFACE/ANODE ANCHOR AND does NOT include ANY ASSEMBLY/CELL ANCHOR.

Cathode-only signals include (non-exhaustive):
- explicit: "cathode", "positive electrode"
- typical cathode materials: MnO2, V2O5, NH4V4O10, vanadium oxides, PBAs, etc.
- patterns like: "The cathode slurry was prepared...", "prepared cathode electrode..."

IMPORTANT:
- If rule (0) triggered (explicit Zn-coating), do NOT apply this cathode-only NO.

B) Results/Discussion/Analysis oriented text
If the paragraph is mainly about results, discussion, conclusions, performance, cycling, capacity,
mechanism, kinetics/behavior/evolution of deposition, dendrite suppression (as results),
=> MUST be NO.

C) Characterization-only text
If the paragraph is mainly technique lists or measurement descriptions like XRD/SEM/TEM/XPS/FTIR/Raman/BET/AFM
AND it does not include any fabrication/preparation steps,
=> MUST be NO.

D) Electrochemical testing-only text
If the paragraph is mainly about CV/LSV/EIS/GCD or electrochemical measurements
AND it does not include any fabrication/preparation steps,
=> MUST be NO.

E) Non-target device context
If the paragraph is about supercapacitors / ZHSC / hybrid supercapacitors
and does not explicitly include Zn-anode protective-layer fabrication or AZIB cell assembly,
=> MUST be NO.

========================
2) HARD-YES (OVERRIDE) RULES ✅
Answer YES if ANY of the following is true (and HARD-NO did not trigger):

1) Zn-anode ex-situ protective layer fabrication evidence
- Zn-SURFACE / ANODE ANCHOR present
AND FABRICATION / PREPARATION VERBS present
AND recipe details exist.

2) Cell/battery assembly or electrolyte preparation evidence (not cathode-only)
- ASSEMBLY / CELL ANCHOR present
AND actionable assembly/prep steps exist (amounts, soaking, drying, stacking components, etc.)

3) Coating-material preparation that is clearly used for Zn coating within this paragraph
- COATING-MATERIAL SIGNALS present
AND paragraph explicitly ties it to Zn surface modification (Zn anchor + coating verbs).

========================
DEFAULT LOGIC
- If HARD-NO triggered => NO.
- Else if any HARD-YES triggered => YES.
- Otherwise => NO.

========================
INPUT (for this decision)
Section heading: "{heading}"

Paragraph:
\"\"\"{paragraph}\"\"\"

========================
OUTPUT (JSON ONLY)
{{ "decision":"YES/NO", "confidence":0.0-1.0, "reason":"brief; cite Zn/assembly anchors or cathode-only exclusion or Zn-coating override" }}
"""


# ==============================================================================
# KEYWORDS / HEURISTICS (mostly unchanged)
# ==============================================================================
FAB_VERBS = [
    "preparation", "prepare", "prepared",
    "fabrication", "fabricate", "fabricated",
    "synthesis", "synthesize", "synthesized",
    "construction", "construct", "constructed",
    "assembly", "assemble", "assembled",
    "coating", "coat", "coated",
    "deposition", "deposit", "deposited",
    "growth", "grow", "grown",
    "grafting", "grafted",
    "casting", "cast", "casted",
    "spraying", "spray", "sprayed",
    "spin-coating", "spin coating", "spin-coated", "spin coated",
    "dip-coating", "dip coating", "dip-coated", "dip coated",
    "soaking", "soak", "soaked",
    "dipping", "dip", "dipped",
    "immersion", "immerse", "immersed",
    "drying", "dry", "dried",
    "annealing", "anneal", "annealed",
    "calcination", "calcine", "calcined",
    "curing", "cure", "cured",
    "sputtering", "sputter", "sputtered",
    "electrodeposition", "electroplating", "plating",
]

ZN_SIGNALS = [
    "@zn", "zn@", "coated zn", "modified zn", "treated zn", "pretreated zn",
    "zinc foil", "zn foil", "zinc anode", "zn anode", "negative electrode",
    "layer on zn", "grown on zn", "deposited on zn", "sputtered on zn",
    "protective layer", "artificial layer", "interface layer", "interphase",

    "zn surface", "zinc surface", "zn metal", "zinc metal",
    "zn plate", "zinc plate", "zn sheet", "zinc sheet",
    "zn electrode", "zinc electrode",

    "on zn", "onto zn", "upon zn",
    "on zinc", "onto zinc", "upon zinc",
    "coated on zn", "coated onto zn",
    "deposited on zn", "deposited onto zn",
    "grown on zn", "grown onto zn",
    "formed on zn", "formed onto zn",
    "covered on zn", "covered onto zn",

    "polished zn", "etched zn", "acid-etched zn", "alkali-etched zn",
    "cleaned zn", "washed zn", "rinsed zn",
    "activated zn", "pretreated zinc",

    "zn deposition", "zinc deposition",
    "zn plating", "zinc plating",
    "electrodeposited zn", "electrodeposited zinc",
    "plated zn", "plated zinc",

    "artificial sei", "sei layer", "solid electrolyte interphase",
    "artificial interphase", "artificial interface",
    "protective coating", "surface coating", "coating layer",
    "interfacial layer",
]

COATING_MATERIALS = [
    "mof", "metal-organic framework", "metal organic framework",
    "zif", "mil", "uio", "cof", "covalent organic framework",
    "porous organic cage", "poc",
    "zeolite", "zsm", "sapo",

    "ldh", "layered double hydroxide",
    "mxene", "ti3c2", "ti3c2tx",
    "graphene", "graphene oxide", "go", "reduced graphene oxide", "rgo",
    "cnt", "carbon nanotube", "carbon nanotubes",
    "carbon nanofiber", "cnf", "carbon fiber",
    "carbon cloth", "cc", "activated carbon",
    "porous carbon", "carbon black",

    "pvdf", "pva", "paa", "pam", "peg", "peo",
    "pan", "pmma", "pvp", "ptfe",
    "nafion", "polyimide", "pi",
    "cellulose", "nanocellulose", "cnc",
    "alginate", "sodium alginate",
    "chitosan",
    "gel polymer electrolyte", "gpe",
    "hydrogel", "polymer gel", "ion gel",
    "film", "membrane", "coating",

    "igzo", "in2o3", "zno", "gao", "ga2o3", "al2o3", "tio2",
    "sio2", "zro2", "hfo2", "ceo2",
    "mgo", "cao",
    "tin oxide", "sno2",
    "mno2", "v2o5",
    "znf2", "zinc fluoride",
    "zns", "zinc sulfide",
    "zn3(po4)2", "zinc phosphate",
    "zno4",
    "aln", "tinn", "bn", "hbn", "hexagonal boron nitride",
    "sic", "silicon carbide",
    "tiN", "tin",

    "znf2", "nh4f", "lif", "naf",
    "zinc triflate", "zn(otf)2", "zn(tfsi)2", "zntfsi2",
    "pfs", "pf6", "tfsI", "tfsi",
]

RESULTY_WORDS = [
    "results", "discussion", "conclusion", "summary", "findings", "analysis",
    "performance", "cycling", "rate capability", "capacity", "mechanism",
    "evolution", "behavior", "kinetics", "dynamics", "suppression",
    "corrosion resistance", "dendrite", "nucleation", "deposition behavior",
]

CHAR_TECH = [
    "xrd", "sem", "tem", "xps", "raman", "ftir", "bet", "afm", "eds", "tof-sims",
    "rigaku", "thermo", "bruker", "zeiss", "shimadzu",
]

ECHEM_TECH = [
    "eis", "cv", "lsv", "gcd", "galvanostatic", "chronoamperometry", "tafel",
    "land", "chi", "zahner", "cr2032",
]

# (중요) Stage-1 DROP-ONLY: "진짜 메타"만 DROP 후보
STRICT_META_DROP = [
    "abstract", "graphical abstract", "highlights", "keywords",
    "author", "authors", "author contributions", "contribution",
    "acknowledgement", "acknowledgment",
    "conflict of interest", "competing interests",
    "ethics", "data availability",
    "reference", "references", "bibliography",
    "supplementary", "supporting information",
]

# Stage-1 DROP 금지(KEEP override): 이런 단어가 heading에 있으면 무조건 PASS
STAGE1_KEEP_OVERRIDE = [
    "experimental", "method", "materials", "preparation", "synthesis",
    "fabrication", "assembly", "electrode", "anode", "cathode",
    "electrolyte", "separator",
]

# regex (existing)
RE_UNITS = re.compile(r"\b(mg|g|kg|ml|l|µl|ul|mmol|mol|m|wt%|vol%|sccm|pa|v|w|a|ma|ua|cm|cm-2|mA\s*cm-2|mAh\s*cm-2)\b", re.I)
RE_TEMP = re.compile(r"(\b\d+(\.\d+)?\s*(°c|c)\b)|(\b\d+\s*k\b)", re.I)
RE_TIME = re.compile(r"\b(\d+(\.\d+)?\s*(h|hr|hrs|hour|hours|min|mins|minute|minutes|s|sec|secs|second|seconds|day|days))\b", re.I)
RE_CONC = re.compile(r"\b(\d+(\.\d+)?\s*(m|mol\s*l-1|mol/l|molar|wt%|vol%))\b", re.I)
RE_STEPWORDS = re.compile(r"\b(stirring|stirred|sonication|sonicated|centrifug|washed|rinsed|dried|vacuum|oven|sealed|autoclave|calcined)\b", re.I)

# rescue용 detail은 좀 더 보수적으로(단독 "m"/"cm" 같은 잡음 제거)
RE_RESCUE_UNITS = re.compile(r"\b(mg|g|kg|ml|l|µl|ul|mmol|mol|wt%|vol%|sccm|pa|v|w|a|ma|ua|cm-2|mA\s*cm-2|mAh\s*cm-2)\b", re.I)


# ==============================================================================
# DATA STRUCTURES
# ==============================================================================
@dataclass
class HeadingGate:
    stage1_action: str  # PASS/DROP
    decision: str       # YES/NO (rule-based here)
    confidence: float
    reason: str
    by: str             # RULE


@dataclass
class ParagraphHit:
    paragraph_index: int
    score: float
    snippet: str
    decision: str
    confidence: float
    reason: str


@dataclass
class SectionResult:
    doc_id: str
    source_file: str
    title: str
    section_path: str
    level: int
    heading_raw: str
    heading_norm: str

    stage1_action: str
    stage1_by: str
    stage1_confidence: float
    stage1_reason: str

    final_decision: str
    final_confidence: float
    final_reason: str

    total_paragraphs: int
    candidate_paragraphs: int
    hit_paragraphs: List[ParagraphHit]


# ==============================================================================
# HELPERS
# ==============================================================================
def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_heading(heading: str) -> str:
    """Remove numbering and lowercase."""
    if not heading:
        return ""
    patterns = [
        r"^[0-9]+\.?[0-9]*\.?[0-9]*\.?\s*",
        r"^[SsAaBbCcDd][0-9]+\.?\s*",
        r"^\([0-9]+\)\s*",
        r"^[0-9]+\)\s*",
        r"^\[[0-9]+\]\s*",
    ]
    h = heading
    for p in patterns:
        h = re.sub(p, "", h)
    return h.strip().lower()


def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def safe_snippet(s: str, max_len: int = 220) -> str:
    s = normalize_text(s)
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip() + " ..."


def count_any(text: str, keywords: List[str]) -> int:
    t = text.lower()
    return sum(1 for k in keywords if k in t)


# ==============================================================================
# OLLAMA CALL + JSON PARSE (shared)
# ==============================================================================
def _parse_llm_json(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if "```json" in t:
        t = t.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in t:
        t = t.split("```", 1)[1].split("```", 1)[0].strip()
    return json.loads(t)


def call_ollama(ollama_url: str, model: str, prompt: str, timeout_sec: int, temperature: float, top_p: float) -> Dict[str, Any]:
    resp = requests.post(
        f"{ollama_url}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "top_p": top_p},
        },
        timeout=timeout_sec,
    )
    if resp.status_code != 200:
        return {"decision": "ERROR", "confidence": 0.0, "reason": f"HTTP {resp.status_code}"}
    payload = resp.json()
    out = (payload.get("response") or "").strip()
    try:
        parsed = _parse_llm_json(out)
    except Exception as e:
        return {"decision": "ERROR", "confidence": 0.0, "reason": f"JSON parse error: {str(e)[:120]}"}

    decision = str(parsed.get("decision", "")).upper().strip()
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


# ==============================================================================
# CACHE (JSONL key-value)
# ==============================================================================
def load_cache_jsonl(path: Path) -> Dict[str, Dict[str, Any]]:
    cache: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return cache
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                key = obj.get("key")
                if key:
                    cache[key] = obj
            except Exception:
                continue
    return cache


def append_cache_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ==============================================================================
# STAGE 2: SCORING / CHEAP GATE (unchanged)
# ==============================================================================
def recipe_score(paragraph: str, heading: str = "") -> float:
    p = paragraph.lower()
    h = heading.lower()
    score = 0.0

    score += 2.0 * count_any(p, FAB_VERBS)
    score += 1.0 * len(RE_STEPWORDS.findall(p))
    score += 2.0 * len(RE_UNITS.findall(p))
    score += 2.0 * len(RE_TEMP.findall(p))
    score += 2.0 * len(RE_TIME.findall(p))
    score += 2.0 * len(RE_CONC.findall(p))

    score += 3.0 * count_any(p, ZN_SIGNALS)
    score += 1.5 * count_any(p, COATING_MATERIALS)

    if any(x in h for x in ["experimental", "methods", "materials and methods", "preparation", "fabrication", "synthesis"]):
        score += 2.0

    # penalties
    results_hits = count_any(p, RESULTY_WORDS)
    fab_hits = count_any(p, FAB_VERBS)
    if results_hits >= 3 and fab_hits == 0:
        score -= 3.0
    if count_any(p, CHAR_TECH) >= 2 and fab_hits == 0:
        score -= 3.0
    if count_any(p, ECHEM_TECH) >= 2 and fab_hits == 0:
        score -= 3.0

    return score


def cheap_gate(paragraph: str, heading: str = "") -> bool:
    p = paragraph.lower()
    h = heading.lower()
    if any(v in p for v in FAB_VERBS):
        return True
    if RE_UNITS.search(p) or RE_TEMP.search(p) or RE_TIME.search(p) or RE_CONC.search(p):
        return True
    if any(z in p for z in ZN_SIGNALS) or any(m in p for m in COATING_MATERIALS):
        return True
    if any(x in h for x in ["experimental", "methods", "materials and methods", "experimental section"]):
        return True
    return False


# ==============================================================================
# STAGE 1 (NEW): DROP-ONLY + RESCUE
# ==============================================================================
def _has_zn_anchor(t: str) -> bool:
    tt = t.lower()
    return any(z in tt for z in ZN_SIGNALS)


def _has_fab_verb(t: str) -> bool:
    tt = t.lower()
    return any(v in tt for v in FAB_VERBS)


def _has_recipe_detail_strong(t: str) -> bool:
    tt = t.lower()
    return bool(
        RE_RESCUE_UNITS.search(tt) or RE_TEMP.search(tt) or RE_TIME.search(tt) or RE_CONC.search(tt)
    )


# --- micro-chunking helper (Stage2 & rescue에서 공용으로 사용)
_ABBREV_PATTERNS = [
    re.compile(r"\b(e\.g\.)", re.I),
    re.compile(r"\b(i\.e\.)", re.I),
    re.compile(r"\b(etc\.)", re.I),
    re.compile(r"\b(fig\.)", re.I),
    re.compile(r"\b(figs\.)", re.I),
    re.compile(r"\b(eq\.)", re.I),
    re.compile(r"\b(eqs\.)", re.I),
    re.compile(r"\b(ref\.)", re.I),
    re.compile(r"\b(refs\.)", re.I),
    re.compile(r"\b(vs\.)", re.I),
    re.compile(r"\b(no\.)", re.I),
]


def _protect_abbrev(text: str) -> str:
    t = text
    for pat in _ABBREV_PATTERNS:
        t = pat.sub(lambda m: m.group(0).replace(".", "<DOT>"), t)
    return t


def _restore_abbrev(text: str) -> str:
    return text.replace("<DOT>", ".")


def split_into_sentences(text: str) -> List[str]:
    """Scientific-friendly sentence split (cheap & robust)."""
    t = normalize_text(text)
    if not t:
        return []
    t = _protect_abbrev(t)

    # split on punctuation + whitespace, next token looks like a new sentence starter
    parts = re.split(r"(?<=[\.\?\!])\s+(?=[A-Za-z(])", t)
    parts = [_restore_abbrev(p).strip() for p in parts if p and p.strip()]

    # merge tiny fragments that are likely split artifacts
    merged: List[str] = []
    for p in parts:
        if not merged:
            merged.append(p)
            continue
        if len(p) < 40 and not (_has_zn_anchor(p) or _has_fab_verb(p) or _has_recipe_detail_strong(p)):
            merged[-1] = (merged[-1] + " " + p).strip()
        else:
            merged.append(p)

    return merged


def generate_microchunks(paragraph: str, max_sentences: int = 40) -> List[str]:
    """
    Create micro-chunks to avoid 'mixed paragraph' LLM rejection:
    - single sentence chunks
    - 2-sentence window chunks (to preserve context: "It was dried..." needs previous sentence)
    """
    sents = split_into_sentences(paragraph)
    if not sents:
        return []
    sents = sents[:max_sentences]

    chunks: List[str] = []
    # 1-sentence
    for s in sents:
        if s.strip():
            chunks.append(s.strip())
    # 2-sentence window
    for i in range(len(sents) - 1):
        c = (sents[i].strip() + " " + sents[i + 1].strip()).strip()
        if c:
            chunks.append(c)

    # de-dup preserve order
    seen = set()
    out: List[str] = []
    for c in chunks:
        key = sha256_text(normalize_text(c).lower())[:16]
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def is_rescue_chunk(text: str) -> bool:
    """
    Strong rescue: Zn-anchor + fab-verb + recipe detail (unit/temp/time/conc).
    """
    t = normalize_text(text)
    if not t:
        return False
    return _has_zn_anchor(t) and _has_fab_verb(t) and _has_recipe_detail_strong(t)


def find_best_rescue(paragraphs: List[str], heading_raw: str) -> Optional[Tuple[int, str, float]]:
    """
    Return best rescue candidate: (paragraph_index, chunk_text, score)
    """
    h = (heading_raw or "")
    best: Optional[Tuple[int, str, float]] = None

    for p_idx, p in enumerate(paragraphs or []):
        p_norm = normalize_text(p)
        if not p_norm:
            continue
        chunks = generate_microchunks(p_norm)
        if not chunks:
            continue
        for c in chunks:
            if is_rescue_chunk(c) or recipe_score(c, h) >= 3.0:
                s = recipe_score(c, h)
                if best is None or s > best[2]:
                    best = (p_idx, c, s)

    return best


def stage1_drop_only(heading_raw: str, paragraphs: List[str]) -> HeadingGate:
    """
    HARD GUARANTEE:
    - Stage-1 can DROP only if:
      (A) heading is strict paper-meta
      AND
      (B) no rescue signal in paragraphs (recipe possibility ~ 0)
    - Otherwise PASS (Stage-1 never blocks)
    """
    h = normalize_heading(heading_raw)

    # If rescue exists, ALWAYS PASS
    rescue_best = find_best_rescue(paragraphs, heading_raw)
    if rescue_best is not None:
        return HeadingGate(
            stage1_action="PASS",
            decision="YES",
            confidence=0.70,
            reason="RESCUE: paragraph contains Zn+fab+detail or high recipe_score micro-chunk.",
            by="RULE",
        )

    # Empty heading -> PASS (do not block)
    if not h:
        return HeadingGate(
            stage1_action="PASS",
            decision="YES",
            confidence=0.20,
            reason="Empty heading; Stage-1 is drop-only -> PASS by default (no rescue found).",
            by="RULE",
        )

    # KEEP override: never DROP if heading hints methods/materials etc.
    if any(k in h for k in STAGE1_KEEP_OVERRIDE):
        return HeadingGate(
            stage1_action="PASS",
            decision="YES",
            confidence=0.60,
            reason="KEEP override keyword in heading (methods/materials/prep etc) -> PASS.",
            by="RULE",
        )

    # Strict meta -> can DROP (only when no rescue)
    if any(k in h for k in STRICT_META_DROP):
        return HeadingGate(
            stage1_action="DROP",
            decision="NO",
            confidence=0.98,
            reason="Strict paper-meta heading and no rescue signals in section text.",
            by="RULE",
        )

    # Everything else PASS
    return HeadingGate(
        stage1_action="PASS",
        decision="YES",
        confidence=0.50,
        reason="Default PASS (Stage-1 is drop-only; decision moved to Stage-2).",
        by="RULE",
    )


# ==============================================================================
# STAGE 2: CANDIDATE SELECTION (UPDATED: micro-chunking + rescue chunk forced)
# ==============================================================================
def select_candidate_paragraphs(
    paragraphs: List[str],
    heading: str,
    top_k: int,
    max_chars: int,
    min_score: float,
) -> List[Tuple[int, str, float]]:
    """
    Returns list of (paragraph_index, chunk_text, score)
    - micro-chunking: sentence / 2-sentence windows
    - rescue chunks bypass min_score and are forced into candidates (up to budget)
    """
    h = heading or ""

    rescue_candidates: List[Tuple[int, str, float]] = []
    regular_candidates: List[Tuple[int, str, float]] = []

    for p_idx, p in enumerate(paragraphs or []):
        p_norm = normalize_text(p)
        if not p_norm:
            continue

        chunks = generate_microchunks(p_norm)
        if not chunks:
            # fallback: treat whole paragraph as one chunk
            chunks = [p_norm]

        for c in chunks:
            c_norm = normalize_text(c)
            if not c_norm:
                continue
            c_trunc = c_norm[:max_chars]

            # compute score on truncated chunk (consistent with original behavior)
            s = recipe_score(c_trunc, h)

            # RESCUE chunk: always include (min_score 무시)
            if is_rescue_chunk(c_trunc):
                rescue_candidates.append((p_idx, c_trunc, s))
                continue

            # Regular candidate: keep your existing cheap_gate + min_score logic
            if not cheap_gate(c_trunc, h):
                continue
            if s >= min_score:
                regular_candidates.append((p_idx, c_trunc, s))

    # sort
    rescue_candidates.sort(key=lambda x: x[2], reverse=True)
    regular_candidates.sort(key=lambda x: x[2], reverse=True)

    # force include up to 2 rescue chunks (or up to top_k if top_k < 2)
    forced_n = min(2, top_k)
    forced = rescue_candidates[:forced_n]

    # fill remaining slots with regular (avoid duplicates)
    out: List[Tuple[int, str, float]] = []
    seen = set()

    def _push(item: Tuple[int, str, float]):
        key = sha256_text(f"{item[0]}::{normalize_text(item[1]).lower()}")[:16]
        if key in seen:
            return
        seen.add(key)
        out.append(item)

    for it in forced:
        _push(it)

    for it in regular_candidates:
        if len(out) >= top_k:
            break
        _push(it)

    # if still empty but we had rescue candidates, fallback include best rescue
    if not out and rescue_candidates:
        _push(rescue_candidates[0])

    return out


# ==============================================================================
# STAGE 2: PARAGRAPH (CHUNK) LLM VERIFY (same function name; input is chunk now)
# ==============================================================================
def stage2_paragraph_verify(
    heading_raw: str,
    paragraph: str,
    score: float,
    ollama_url: str,
    model: str,
    cache: Dict[str, Dict[str, Any]],
    cache_path: Path,
    timeout_sec: int,
    temperature: float,
    top_p: float,
) -> Dict[str, Any]:
    """
    Returns {decision, confidence, reason}
    Note: 'paragraph' here can be a micro-chunk.
    """
    key_text = "P2\n" + normalize_heading(heading_raw) + "\n" + normalize_text(paragraph)
    cache_key = sha256_text(key_text)
    if cache_key in cache:
        obj = cache[cache_key]
        return {
            "decision": obj.get("decision", "ERROR"),
            "confidence": float(obj.get("confidence", 0.0) or 0.0),
            "reason": obj.get("reason", ""),
        }

    prompt = PARAGRAPH_VERIFY_PROMPT.format(heading=heading_raw, paragraph=paragraph)
    res = call_ollama(ollama_url, model, prompt, timeout_sec, temperature, top_p)

    cache_obj = {
        "key": cache_key,
        "decision": res.get("decision", "ERROR"),
        "confidence": float(res.get("confidence", 0.0) or 0.0),
        "reason": res.get("reason", ""),
    }
    cache[cache_key] = cache_obj
    append_cache_jsonl(cache_path, cache_obj)
    return res


# ==============================================================================
# GROBID SECTION ITERATION (unchanged)
# ==============================================================================
def iter_sections(papers: List[Dict[str, Any]]) -> Iterable[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    for idx, paper in enumerate(papers):
        source_file = str(paper.get("source_file", "")).strip()
        doc_id = sha256_text(f"{idx:04d}::{source_file}")[:16]

        sections_flat = paper.get("sections_flat")
        if isinstance(sections_flat, list) and sections_flat:
            for sec in sections_flat:
                if isinstance(sec, dict):
                    yield doc_id, paper, sec
            continue

        # fallback: recursive walk in `sections`
        def walk(sections: Any, path_prefix: str = ""):
            if not isinstance(sections, list):
                return
            for j, s in enumerate(sections):
                if not isinstance(s, dict):
                    continue
                path = str(s.get("path", "")).strip()
                if not path:
                    path = f"{path_prefix}{j}"
                s["_fallback_path"] = path
                yield s
                children = s.get("children", [])
                if children:
                    yield from walk(children, path_prefix=path + "/")

        sections = paper.get("sections", [])
        for sec in walk(sections):
            yield doc_id, paper, sec


def get_section_path(sec: Dict[str, Any]) -> str:
    p = str(sec.get("path", "")).strip()
    if p:
        return p
    return str(sec.get("_fallback_path", sec.get("heading", ""))).strip()


def get_section_level(sec: Dict[str, Any]) -> int:
    try:
        return int(sec.get("level", 0) or 0)
    except Exception:
        return 0


def get_section_heading(sec: Dict[str, Any]) -> str:
    return str(sec.get("heading", "")).strip()


def get_section_paragraphs(sec: Dict[str, Any]) -> List[str]:
    paragraphs = sec.get("paragraphs") or []
    if isinstance(paragraphs, list):
        return [p for p in paragraphs if isinstance(p, str)]
    if isinstance(paragraphs, str):
        return [paragraphs]
    text = sec.get("text")
    if isinstance(text, str) and text.strip():
        return [text]
    return []


# ==============================================================================
# OUTPUT (unchanged)
# ==============================================================================
def save_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def save_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def flatten_for_csv(sr: SectionResult) -> Dict[str, Any]:
    hits = sr.hit_paragraphs or []
    hit_str = " | ".join(
        [f"p{h.paragraph_index}(score={h.score:.1f},conf={h.confidence:.2f}): {h.snippet}" for h in hits[:3]]
    )
    if len(hits) > 3:
        hit_str += f" | ... (+{len(hits)-3} more)"
    return {
        "doc_id": sr.doc_id,
        "source_file": sr.source_file,
        "title": sr.title,
        "section_path": sr.section_path,
        "level": sr.level,
        "heading_raw": sr.heading_raw,
        "stage1_action": sr.stage1_action,
        "stage1_by": sr.stage1_by,
        "stage1_confidence": f"{sr.stage1_confidence:.3f}",
        "stage1_reason": sr.stage1_reason,
        "final_decision": sr.final_decision,
        "final_confidence": f"{sr.final_confidence:.3f}",
        "final_reason": sr.final_reason,
        "total_paragraphs": sr.total_paragraphs,
        "candidate_paragraphs": sr.candidate_paragraphs,
        "hit_paragraphs": hit_str,
    }


# ==============================================================================
# MAIN
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="2-Stage Section Filter (Stage1 DROP-ONLY+RESCUE -> Stage2 LLM Verify)")
    parser.add_argument("--input_json", type=Path, default=DEFAULT_INPUT_JSON, help="Path to GROBID JSON output")
    parser.add_argument("--output_dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory")

    # LLM Settings (Stage2 only)
    parser.add_argument("--ollama_url", type=str, default="http://localhost:11434")
    parser.add_argument("--llm_model", type=str, default="qwen2.5:14b-instruct")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--timeout", type=int, default=60)

    # Stage 2 Settings
    parser.add_argument("--top_k", type=int, default=3, help="Max chunks to check per section")
    parser.add_argument("--max_chars", type=int, default=1000, help="Max chars per chunk")
    parser.add_argument("--min_score", type=float, default=0.5, help="Min heuristic score to verify (non-rescue only)")

    # Filter
    parser.add_argument("--yes_only", action="store_true", help="Only save YES sections in final output")

    args = parser.parse_args()

    if not args.input_json.exists():
        print(f"Input file not found: {args.input_json}")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_jsonl = args.output_dir / f"classified_sections_{ts}.jsonl"
    out_csv = args.output_dir / f"classified_sections_{ts}.csv"

    cache_dir = args.output_dir / ".cache"
    cache_path_P = cache_dir / "paragraph_verify_cache.jsonl"

    cache_P = load_cache_jsonl(cache_path_P)

    print("=" * 90)
    print("2-Stage Section Filter (Stage1 DROP-ONLY+RESCUE -> Stage2 micro-chunk LLM Verify)")
    print("=" * 90)
    print(f"Input: {args.input_json}")
    print(f"Output: {args.output_dir}")
    print(f"LLM(Stage2): {args.llm_model} @ {args.ollama_url}")
    print(f"Stage2: top_k={args.top_k}, max_chars={args.max_chars}, min_score={args.min_score} (non-rescue only)")
    print(f"Cache: paragraph={len(cache_P)} entries")
    print("=" * 90)

    # Load Papers
    with open(args.input_json, "r", encoding="utf-8") as f:
        try:
            papers = json.load(f)
        except json.JSONDecodeError:
            print("Failed to load JSON (maybe check if it is JSON or JSONL?)")
            return

    if isinstance(papers, dict):
        papers = papers.get("papers", [])
    if not isinstance(papers, list):
        print("Input JSON must be a list of papers.")
        return

    print(f"Loaded papers: {len(papers)}")

    all_sections_iter = list(iter_sections(papers))
    print(f"Total sections: {len(all_sections_iter)}")

    results: List[SectionResult] = []

    for doc_id, paper, sec in tqdm(all_sections_iter, desc="Processing sections"):
        heading_raw = get_section_heading(sec)
        paragraphs = get_section_paragraphs(sec)
        section_path = get_section_path(sec)
        level = get_section_level(sec)

        source_file = str(paper.get("source_file", ""))
        title = paper.get("title", "")

        # ----------------------------------------------------------------------
        # STAGE 1: DROP-ONLY + RESCUE  (NEW)
        # ----------------------------------------------------------------------
        gate = stage1_drop_only(heading_raw, paragraphs)

        if gate.stage1_action == "DROP":
            results.append(SectionResult(
                doc_id=doc_id,
                source_file=source_file,
                title=title,
                section_path=section_path,
                level=level,
                heading_raw=heading_raw,
                heading_norm=normalize_heading(heading_raw),
                stage1_action="DROP",
                stage1_by=gate.by,
                stage1_confidence=gate.confidence,
                stage1_reason=gate.reason,
                final_decision="NO",
                final_confidence=gate.confidence,
                final_reason="Stage1 DROP (strict meta + no rescue).",
                total_paragraphs=len(paragraphs),
                candidate_paragraphs=0,
                hit_paragraphs=[],
            ))
            continue

        # ----------------------------------------------------------------------
        # STAGE 2: micro-chunk candidate selection & LLM verify
        # ----------------------------------------------------------------------
        candidates = select_candidate_paragraphs(
            paragraphs=paragraphs,
            heading=heading_raw,
            top_k=args.top_k,
            max_chars=args.max_chars,
            min_score=args.min_score,
        )

        # 최후 안전망: candidates가 비었는데 rescue_best가 있으면 1개라도 강제
        if not candidates:
            rescue_best = find_best_rescue(paragraphs, heading_raw)
            if rescue_best is not None:
                candidates = [(rescue_best[0], normalize_text(rescue_best[1])[:args.max_chars], rescue_best[2])]

        if not candidates:
            results.append(SectionResult(
                doc_id=doc_id,
                source_file=source_file,
                title=title,
                section_path=section_path,
                level=level,
                heading_raw=heading_raw,
                heading_norm=normalize_heading(heading_raw),
                stage1_action="PASS",
                stage1_by=gate.by,
                stage1_confidence=gate.confidence,
                stage1_reason=gate.reason,
                final_decision="NO",
                final_confidence=0.0,
                final_reason="No candidate chunks found (after micro-chunking).",
                total_paragraphs=len(paragraphs),
                candidate_paragraphs=0,
                hit_paragraphs=[],
            ))
            continue

        hit_paragraphs: List[ParagraphHit] = []
        any_yes = False

        for p_idx, c_text, c_score in candidates:
            res = stage2_paragraph_verify(
                heading_raw=heading_raw,
                paragraph=c_text,
                score=c_score,
                ollama_url=args.ollama_url,
                model=args.llm_model,
                cache=cache_P,
                cache_path=cache_path_P,
                timeout_sec=args.timeout,
                temperature=args.temperature,
                top_p=args.top_p,
            )

            dec = res["decision"]
            conf = res["confidence"]
            reas = res["reason"]

            if dec == "YES":
                any_yes = True
                hit_paragraphs.append(ParagraphHit(
                    paragraph_index=p_idx,
                    score=c_score,
                    snippet=safe_snippet(c_text),
                    decision="YES",
                    confidence=conf,
                    reason=reas,
                ))

        if any_yes:
            best_conf = max((h.confidence for h in hit_paragraphs), default=0.5)
            joined_reasons = " | ".join([h.reason for h in hit_paragraphs])

            results.append(SectionResult(
                doc_id=doc_id,
                source_file=source_file,
                title=title,
                section_path=section_path,
                level=level,
                heading_raw=heading_raw,
                heading_norm=normalize_heading(heading_raw),
                stage1_action="PASS",
                stage1_by=gate.by,
                stage1_confidence=gate.confidence,
                stage1_reason=gate.reason,
                final_decision="YES",
                final_confidence=best_conf,
                final_reason=f"Found {len(hit_paragraphs)} verify-PASS chunks. ({joined_reasons})",
                total_paragraphs=len(paragraphs),
                candidate_paragraphs=len(candidates),
                hit_paragraphs=hit_paragraphs,
            ))
        else:
            results.append(SectionResult(
                doc_id=doc_id,
                source_file=source_file,
                title=title,
                section_path=section_path,
                level=level,
                heading_raw=heading_raw,
                heading_norm=normalize_heading(heading_raw),
                stage1_action="PASS",
                stage1_by=gate.by,
                stage1_confidence=gate.confidence,
                stage1_reason=gate.reason,
                final_decision="NO",
                final_confidence=0.9,
                final_reason="All candidate chunks rejected by LLM.",
                total_paragraphs=len(paragraphs),
                candidate_paragraphs=len(candidates),
                hit_paragraphs=[],
            ))

    # ----------------------------------------------------------------------
    # SAVE
    # ----------------------------------------------------------------------
    if args.yes_only:
        final_rows = [flatten_for_csv(r) for r in results if r.final_decision == "YES"]
    else:
        final_rows = [flatten_for_csv(r) for r in results]

    save_jsonl(out_jsonl, [asdict(r) for r in results if (r.final_decision == "YES" or not args.yes_only)])

    csv_fields = [
        "doc_id", "source_file", "title", "heading_raw", "final_decision", "final_confidence", "final_reason",
        "hit_paragraphs", "stage1_action", "stage1_reason", "total_paragraphs", "candidate_paragraphs"
    ]
    save_csv(out_csv, final_rows, csv_fields)

    print("\n" + "=" * 90)
    print("DONE.")
    print(f"Total processed: {len(results)}")
    print(f"YES count: {sum(1 for r in results if r.final_decision == 'YES')}")
    print(f"JSONL saved: {out_jsonl}")
    print(f"CSV saved: {out_csv}")
    print("=" * 90)


if __name__ == "__main__":
    main()
