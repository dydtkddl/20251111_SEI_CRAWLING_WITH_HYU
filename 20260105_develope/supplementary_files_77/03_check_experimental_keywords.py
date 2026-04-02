# -*- coding: utf-8 -*-
"""
Experimental Evidence Finder v15.6 (Added Cleaned Column)
Modified by: Anyongsan (AI Agent)
Date: 2026-01-06

Updates:
1. [Feature] Added `strip_markdown_emphasis`: Removes markdown emphasis chars (*, _) for cleaner NLP processing.
2. [Output] Added new CSV column: `EvidenceJSON_Cleaned` (contains text without ** or * markers).
3. [Integrity] Preserves all previous logic (HTML cleaning, Regex filters).
"""

import re
import csv
import json
import argparse
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from tqdm import tqdm

# ============================================================
# 0) Logging Setup
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================
# 1) Unicode & HTML Cleanup Tools
# ============================================================
INVISIBLE_CHARS = ["\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"]
WEIRD_SPACES = ["\u00a0", "\u202f", "\u2007", "\u2009", "\u200a"]

def normalize_invisibles(s: str) -> str:
    if not s: return s
    for ch in INVISIBLE_CHARS: s = s.replace(ch, "")
    for sp in WEIRD_SPACES: s = s.replace(sp, " ")
    return s

def strip_html_tags(text: str) -> str:
    """
    Completely removes HTML tags.
    e.g., "NH<sub>4</sub>" -> "NH4", "<b>Materials</b>" -> "Materials"
    """
    if not text: return ""
    return re.sub(r'<[^>]+>', '', text)

def strip_markdown_emphasis(text: str) -> str:
    """
    [NEW] Removes Markdown emphasis characters (** or * or __ or _).
    It effectively removes the markers while keeping the text content.
    e.g., "**High** efficiency" -> "High efficiency"
    """
    if not text: return ""
    # Remove sequences of * or _ (1 to 3 occurrences typically)
    # Using a global replace for these chars is safer/faster for "cleaning" 
    # unless they are part of a math equation, but in this context (emphasis removal)
    # stripping them is the requested behavior.
    return re.sub(r"[*_]+", "", text)

# ============================================================
# 2) ALL REGEX DEFINITIONS (Defined BEFORE functions)
# ============================================================

# [A] Positive Keywords
KEYWORD_PATTERNS: List[str] = [
    r"\bpreparation\b", r"\bpreparative\b", r"\bprepare(d|s|ing)?\b",
    r"\bsynthesis\b", r"\bsyntheses\b", r"\bprocedures\b", r"\bsynthesi[sz]e(d|s|ing)?\b",
    r"\bfabrication\b", r"\bfabricate(d|s|ing)?\b", r"\bmanufactur(e|ed|ing)\b",
    r"\bproduction\b", r"\bprocess(ing|ed|es)?\b",
    r"\bformation\b", r"\bgrowth\b",
    r"\bdeposition\b", r"\bcoating\b", r"\bplating\b", r"\belectrodeposition\b",
    r"\bcasting\b", r"\bprinting\b",
    r"\bfunctionalization\b", r"\bmodification\b",
    r"\bpre[-\s]?treatment\b", r"\bpost[-\s]?treatment\b",
    r"\bactivation\b", r"\banneal(ing|ed)?\b", r"\bcalcination\b",
    r"\bmaterials?\b",
    r"\banode\s+preparation\b", r"\bZn\s+anode\b", r"\bZinc\s+anode\b",
]
COMPILED_PATTERNS = [(p, re.compile(p, flags=re.IGNORECASE)) for p in KEYWORD_PATTERNS]

# [B] Header Negative Filters
HEADER_NEGATIVE_PATTERNS: List[str] = [
    r"\bcathode\b", r"\bpositive\s+electrode\b", r"\bcathode\s+material\b",
    r"\bactivated\s+carbon\b", r"\bAC\s+electrode\b", r"\bAC\s+cathode\b",
    r"\bvanadate\b", r"\bvanadium\b", 
    r"\bV\d*O\d*\b", r"\bNaV\d*O\d*\b", r"\bNH4\d*V\d*O\d*\b", 
    r"\bNH\d*V\d*O\d*\b", r"\bZn\d*V\d*O\d*\b", r"\bCa\d*V\d*O\d*\b", r"\bLiV\d*O\d*\b",        
    r"\bNVO\b", r"\bZVO\b", r"\bLVO\b",
    r"\bmanganate\b", r"\bmanganese\b",
    r"\bMn\d*O\d*\b", r"\bZn\d*Mn\d*O\d*\b", r"\bMg\d*Mn\d*O\d*\b", r"\bCa\d*Mn\d*O\d*\b", r"\bZMO\b", r"\bCMO\b",
    r"\bPrussian\b", r"\bPBA\b", r"\bhexacyanoferrate\b", r"\bHCF\b",
    r"\bphosphate\b", r"\bLFP\b", r"\bLCO\b", 
    r"\bquinone\b", r"\bpolyaniline\b", r"\bPANI\b", r"\bpolypyrrole\b", r"\bPPy\b", r"\bspinel\b",
    r"\bcarbon\s+cloth\b", r"\bCC\b", r"\bcarbon\s+felt\b", r"\bgraphite\s+felt\b", r"\btitanium\s+foil\b",
    r"^\s*(?:[\d\.]+\s*)?materials?\s*$", 
    r"^\s*(?:[\d\.]+\s*)?raw\s+materials?\s*$", 
    r"^\s*(?:[\d\.]+\s*)?experimental\s+materials?\s*$",
    r"chemicals?\s+and\s+materials?", r"reagents?\s+and\s+materials?",
    r"^\s*chemicals?\s*$", r"^\s*reagents?\s*$",
    r"\bavailability\b", 
    r"\bfabrication\s+of\s+.*(?:batter(?:y|ies)|cell|device)",
    r"\bassembly\s+of\s+.*(?:batter(?:y|ies)|cell|device)",
    r"\bcell\s+assembly\b", r"\bfull\s+cell\b", r"\bsymmetric(?:al)?\s+cell\b",
    r"\bcoin\s+cell\b", r"\bpouch\s+cell\b",
    r"\bcharacteri[sz]ations?\b", r"\bmeasurements?\b", r"\banaly(?:sis|ses)\b",
    r"\bproperties\b", r"\bperformances?\b", r"\binstrumentation\b",
    r"\bcalculation\b", r"\bequation\b", r"\bcomputational\b",
    r"\bresult", r"\bdiscussion",
    r"\bXRD\b", r"\bSEM\b", r"\bTEM\b", r"\bXPS\b", r"\bFTIR\b", r"\bRaman\b", r"\bAFM\b", r"\bEDS\b", r"\bEDX\b",
    r"\bsupporting\s+information\b", r"\bsupplementary\b",
    r"\bfigures?\b", r"\btables?\b", r"\breferences?\b",
    r"\bcontents\b", r"\backnowledg(e)?ments\b", r"\bcorresponding\s+author\b",
]
HEADER_NEGATIVE_RE = re.compile("|".join(f"(?:{p})" for p in HEADER_NEGATIVE_PATTERNS), flags=re.IGNORECASE)

# [C] Body Negative Filters
BODY_NEGATIVE_PATTERNS: List[str] = [
    r"\bpositive\s+electrode\b", r"\bcathode\s+capacity\b",
    r"\bpreparation\s+of\s+.*cathode\b",
    r"\bNi\(OH\)2\b", r"\bLiFePO4\b", r"\bLiCoO2\b", 
    r"\bNaV3O8\b", r"\bNH4V4O10\b", r"\bMnO2\b",
    r"\bseparator\s+was\s+used\b", r"\bglass\s+fiber\s+separator\b",
    r"\bcoin\s+cell\b", r"\bCR2032\b",
    r"\bseparator\b.*\belectrolyte\b",
]
BODY_NEGATIVE_RE = re.compile("|".join(f"(?:{p})" for p in BODY_NEGATIVE_PATTERNS), flags=re.IGNORECASE)

# [D] Metadata & Reference Filters
METADATA_BODY_PATTERNS: List[str] = [
    r"\bdepartment\s+of\b", r"\buniversity\s+of\b", r"\binstitute\s+of\b",
    r"\bkey\s+laboratory\b", r"\bschool\s+of\b", r"\bacademy\s+of\b",
    r"\bcorresponding\s+author\b", r"\be-?mail\s*:", r"\bfax\s*:", r"\btel\s*:",
    r"P\.?O\.?\s*Box\b", r"\bRoad\b.*\bChina\b", r"\bStreet\b.*\bUSA\b",
    r"\bkeywords\b", r"\bcopyright\b",
]
METADATA_BODY_RE = re.compile("|".join(f"(?:{p})" for p in METADATA_BODY_PATTERNS), flags=re.IGNORECASE)

REFERENCE_LIKE_RE = re.compile(
    r"^\s*\d+\.\s+[A-Z][a-z]+.*(?:\(\d{4}\)|https?://|doi\.org|\bvol\b)", 
    flags=re.IGNORECASE
)

# [E] Formatting Regex
CAPTION_PREFIXES = ("figure", "fig", "fig.", "scheme", "table", "caption")
CAPTION_START_RE = re.compile(
    r"^\s*(?:#+\s*)?(?:\*{1,3}|_{1,3})?\s*(?:" + "|".join(re.escape(x) for x in CAPTION_PREFIXES) + r")\b",
    flags=re.IGNORECASE,
)
MAX_CAPTION_LINES = 160

FENCED_CODE_RE = re.compile(r"(?s)(```.*?```|~~~.*?~~~)")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
ATX_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
EMPH_LINE_RE = re.compile(r"^\s*(\*{1,3}|_{1,3})\s*(.+?)\s*\1\s*$")
INLINE_HEADER_RE = re.compile(r"^\s*((?:\*{2,3}.+?\*{2,3}\s*)+)[:.-]?\s+")
BOLD_SEG_RE = re.compile(r"(\*{2,3}|_{2,3})(.+?)\1")
LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d{1,3}[.)]\s+)(.+?)\s*$")
LINK_MD_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MD_MARKUP_RE = re.compile(r"[*_]{1,3}")
HTML_TABLEISH_RE = re.compile(r"</?(table|thead|tbody|tr|td|th|colgroup|col)\b|class\s*=\s*\"(odd|even|header)\"", re.IGNORECASE)
REMOVE_OTHER_TAGS_RE = re.compile(r"(?is)</?(?!sub\b|/sub\b|sup\b|/sup\b)[a-z0-9]+\b[^>]*>")

TITLELIKE_RE = re.compile(r"\b(batter(y|ies)|zinc|anode|cathode|interface|towards|advanced|based\s+on|for\s+aqueous|pouch\s+cell)\b", re.IGNORECASE)
S_SECTION_RE = re.compile(r"^\s*(?:Text\s*)?S\s*[-:.\s]*\s*\d{1,3}(?:\s*[\.\-]\s*\d{1,3})*\s*[\.\)]?\s+(.+?)\s*$", re.IGNORECASE)
NUM_SECTION_RE = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3})*\s*[\.\)]\s+(.+?)\s*$")

# ============================================================
# 3) FUNCTIONS
# ============================================================

def preprocess_text(raw: str) -> str:
    raw = normalize_invisibles(raw)
    text = FENCED_CODE_RE.sub("\n", raw)
    text = INLINE_CODE_RE.sub("", text)
    return text

def is_tableish_or_html_noise(line: str) -> bool:
    if HTML_TABLEISH_RE.search(line): return True
    if line.strip().startswith("|"): return True
    low = line.lower()
    if "td>" in low or "tr>" in low or "th>" in low or "colgroup" in low: return True
    return False

def normalize_heading_text(s: str) -> str:
    s = normalize_invisibles(s)
    s = REMOVE_OTHER_TAGS_RE.sub("", s)
    s = LINK_MD_RE.sub(r"\1", s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = MD_MARKUP_RE.sub("", s).strip()
    s = re.sub(r"[\s:.\-–—]+$", "", s).strip()
    return s

def _mixed_emphasis_heading_heuristic(line: str) -> bool:
    s = normalize_invisibles(line).strip()
    if not s or s.endswith((".", ";")): return False
    if s.startswith("|"): return False
    segs = list(BOLD_SEG_RE.finditer(s))
    if not segs: return False
    total_len = len(re.sub(r"\s+", "", s))
    bold_len = sum(len(re.sub(r"\s+", "", m.group(2))) for m in segs)
    coverage = (bold_len / total_len) if total_len else 0.0
    starts_bold = bool(re.match(r"^\s*(\*{2,3}|_{2,3})", s))
    if not (starts_bold or coverage >= 0.30): return False
    norm = normalize_heading_text(s)
    if not norm or not (1 <= len(norm.split()) <= 30) or len(norm) > 180: return False
    return True

def _fallback_S_heading(line: str) -> bool:
    s = normalize_invisibles(line).strip()
    if not s or s.endswith((".", ";")): return False
    if re.match(r"^\s*S\D*\d{1,3}\b", s, flags=re.IGNORECASE):
        if len(s) <= 200 and len(s.split()) <= 35: return True
    return False

def is_probable_title(line_idx_0based: int, heading_norm: str, heading_raw: str) -> bool:
    s_clean = normalize_invisibles(heading_raw).strip()
    if S_SECTION_RE.match(s_clean) or NUM_SECTION_RE.match(s_clean): return False
    
    # Check for Reference look-alikes
    if REFERENCE_LIKE_RE.match(s_clean):
        return True 

    wcnt = len(heading_norm.split())
    if line_idx_0based <= 5 and wcnt >= 5 and TITLELIKE_RE.search(heading_norm): return True
    if line_idx_0based <= 60 and wcnt >= 10 and TITLELIKE_RE.search(heading_norm): return True
    if len(heading_norm) >= 140: return True
    return False

def should_exclude_hit_heading(line_idx_0based: int, heading_norm: str, heading_raw: str) -> bool:
    # [NEW] Aggressively strip HTML tags before checking filters
    clean_heading = strip_html_tags(heading_norm)
    
    if not clean_heading.strip(): return True
    
    # Check filters on CLEANED heading
    if HEADER_NEGATIVE_RE.search(clean_heading):
        logger.debug(f"Excluded by Header Filter: {clean_heading}")
        return True

    if REFERENCE_LIKE_RE.search(clean_heading):
        logger.debug(f"Excluded as Reference: {clean_heading}")
        return True

    if is_probable_title(line_idx_0based, clean_heading, heading_raw):
        logger.debug(f"Excluded as Probable Title: {clean_heading}")
        return True
    return False

def extract_heading(line: str) -> Optional[Tuple[str, bool, str]]:
    if not line or not line.strip(): return None
    if is_tableish_or_html_noise(line): return None

    s = normalize_invisibles(line).rstrip("\n").strip()
    mlist = LIST_PREFIX_RE.match(s)
    if mlist: s = normalize_invisibles(mlist.group(1)).strip()

    m = ATX_HEADING_RE.match(s)
    if m: return normalize_heading_text(m.group(2)), False, m.group(2)

    ms = S_SECTION_RE.match(s)
    if ms: return normalize_heading_text(s), False, s

    mn = NUM_SECTION_RE.match(s)
    if mn: return normalize_heading_text(s), False, s

    me = EMPH_LINE_RE.match(s)
    if me: return normalize_heading_text(me.group(2)), True, me.group(2)

    mi = INLINE_HEADER_RE.match(s)
    if mi:
        raw_heading_part = mi.group(1)
        return normalize_heading_text(raw_heading_part), True, raw_heading_part

    if _mixed_emphasis_heading_heuristic(s): return normalize_heading_text(s), True, s
    if _fallback_S_heading(s): return normalize_heading_text(s), False, s

    return None

def remove_caption_blocks(lines: List[str]) -> List[str]:
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = normalize_invisibles(lines[i])
        if CAPTION_START_RE.match(line.strip()):
            blank_run, skipped = 0, 0
            i += 1
            while i < n and skipped < MAX_CAPTION_LINES:
                cur = normalize_invisibles(lines[i])
                if cur.strip() == "": blank_run += 1
                else: blank_run = 0
                if blank_run >= 2:
                    i += 1
                    break
                if extract_heading(cur) is not None: break
                i += 1
                skipped += 1
            continue
        out.append(lines[i])
        i += 1
    return out

def cleanup_block_text(block_text: str) -> str:
    # Aggressively strip HTML tags from the body as well
    t = strip_html_tags(normalize_invisibles(block_text))
    
    # Existing cleanup
    t = re.sub(r"(?is)<table\b.*?</table>", "\n", t)
    t = re.sub(r"(?is)<img\b[^>]*>", "\n", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", t)
    t = re.sub(r"(?is)<br\s*/?>", "\n", t)
    t = re.sub(r"(?is)</?p\b[^>]*>", "\n", t)
    t = re.sub(r"(?is)</?span\b[^>]*>", "", t)
    t = re.sub(r"(?is)</?(thead|tbody|tr|td|th|colgroup|col)\b[^>]*>", "", t)
    t = REMOVE_OTHER_TAGS_RE.sub("", t)

    cleaned_lines = []
    tableish_line = re.compile(r"^\s*(</?(thead|tbody|tr|td|th|table)\b|<col\b|<colgroup\b|class\s*=\s*\"(odd|even|header)\"|,?\s*td>|,?\s*tr>|,?\s*th>)", re.IGNORECASE)
    
    for ln in t.splitlines():
        ln = normalize_invisibles(ln).strip()
        if not ln: 
            cleaned_lines.append("")
            continue
        if ln.startswith("|"): continue
        if tableish_line.search(ln) or is_tableish_or_html_noise(ln): continue
        if ln in {",", " ,", ".,", ";", ":", "-----", "----"}: continue
        cleaned_lines.append(ln)

    t = "\n".join(cleaned_lines)
    return re.sub(r"\n{3,}", "\n\n", t).strip()

def analyze_md(md_path: Path) -> Optional[Dict]:
    try:
        raw = md_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.error(f"Failed to read {md_path}: {e}")
        return None

    text = preprocess_text(raw)
    lines = text.splitlines()
    lines = remove_caption_blocks(lines)

    headings_all = []
    for idx, line in enumerate(lines):
        h = extract_heading(line)
        if h is None: continue
        norm, wrapped, rawh = h
        if norm.strip(): headings_all.append((idx, norm, wrapped, rawh))

    if not headings_all:
        return {"found": False, "hit_count": 0, "evidence": []}

    evidence = []
    for j, (idx, h_norm, wrapped, h_raw) in enumerate(headings_all):
        # 1. Keyword Check
        has_kw = False
        for _, pat_re in COMPILED_PATTERNS:
            if pat_re.search(h_norm):
                has_kw = True
                break
        if not has_kw:
            continue

        # 2. Header Negative Check
        if should_exclude_hit_heading(idx, h_norm, h_raw):
            continue

        # 3. Block Extraction
        next_idx = headings_all[j + 1][0] if (j + 1) < len(headings_all) else len(lines)
        block_lines = lines[idx:next_idx] 
        
        has_body = False
        if len(block_lines) >= 1: 
             full_content = "".join(block_lines)
             if len(full_content) > len(h_raw) + 10: 
                 has_body = True
        
        if not has_body:
            continue

        clean_block = cleanup_block_text("\n".join(block_lines).strip())
        
        # 4. Body Content Negative Check
        if BODY_NEGATIVE_RE.search(clean_block):
            logger.debug(f"Excluded by Body Content Filter: {h_norm}")
            continue

        if METADATA_BODY_RE.search(clean_block):
            logger.debug(f"Skipped Affiliation/Metadata Block: {h_norm}")
            continue

        words = clean_block.split()
        if len(words) < 8 or len(clean_block) < 60:
            continue

        evidence.append({
            "heading_norm": h_norm,
            "heading_raw": h_raw,
            "block_text": clean_block
        })

    return {"found": len(evidence) > 0, "hit_count": len(evidence), "evidence": evidence}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", required=True)
    parser.add_argument("--output_csv", required=True)
    parser.add_argument("--only_true", action="store_true")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        logger.error(f"Input dir not found: {input_dir}")
        return

    md_files = list(input_dir.rglob("*.md"))
    logger.info(f"Found {len(md_files)} markdown files.")

    # [CHANGE] Added EvidenceJSON_Cleaned to the header
    with open(args.output_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["File", "Found", "HitCount", "EvidenceJSON", "EvidenceJSON_Cleaned"])

        for md in tqdm(md_files, desc="Processing Files", unit="file"):
            res = analyze_md(md)
            if res is None: continue
            if args.only_true and not res["found"]: continue
            
            if res["found"]:
                logger.info(f"Hit in {md.name}: {res['hit_count']} blocks found.")

            # Prepare Cleaned Version (No ** or *)
            cleaned_evidence = []
            for item in res["evidence"]:
                cleaned_item = item.copy()
                cleaned_item["heading_norm"] = strip_markdown_emphasis(item["heading_norm"])
                cleaned_item["block_text"] = strip_markdown_emphasis(item["block_text"])
                cleaned_evidence.append(cleaned_item)

            writer.writerow([
                str(md.relative_to(input_dir)),
                res["found"],
                res["hit_count"],
                json.dumps(res["evidence"], ensure_ascii=False),
                json.dumps(cleaned_evidence, ensure_ascii=False), # New Column Data
            ])

    logger.info(f"Done. Saved to {args.output_csv}")

if __name__ == "__main__":
    main()