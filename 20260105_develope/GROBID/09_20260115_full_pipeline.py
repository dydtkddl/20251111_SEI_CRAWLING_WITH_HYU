#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Copyright (c) 2026, Kyung Hee University
All rights reserved.

@writer: Yongsang An
@writer email: [yongsang.an@khu.ac.kr]
@date: 2026-01-14
@update: 2026-01-17
"""

"""
END-TO-END Ollama + GROBID Pipeline (Single File)
================================================
PII list 입력 > (본문 PDF + Supplementary 파일 탐색) > GROBID TEI 생성 >
(본문/서플 섹션 추출 + SI 캡션 누수 방지 클리닝 + method-like 캡션 분리) >
섹션 단위 LLM 분류(heading+content, prefilter+sampling+cache+retry) >
문서 단위 최종 판정(Stage1: AZMB? / Stage2: ex-situ? & lab-scale? + focus) >
산출물(per-PII 아티팩트 + 전체 summary CSV/JSONL + YES evidence 모음)

핵심 설계 의도(네 코드들 계승/강화)
- GROBID TEI > 섹션 구조(div/head + paragraphs/sentences) 추출
- SI 캡션 처리: figure/table caption blocks + body 내 캡션 분리 + mid-paragraph split +
  caption_blocks leakage 방지 + method-like caption 별도 보관(옵션)
- 섹션 LLM 분류: heading+content, 너무 길면 method-likelihood 기반 문단 우선 샘플링
- rule-based prefilter(HARD-NO/HARD-YES)로 LLM 호출 절감(precision 우선)
- cache/retry/logging/robust JSON parse 등 운영 안정성 장치

사전 준비
- GROBID 서버 실행(예: docker run --rm -p 8070:8070 lfoppiano/grobid:0.8.0)
- Ollama 실행 + 모델 pull(예: qwen2.5:14b-instruct 또는 qwen3:30b-a3b-instruct 등)

예시 실행
python pipeline_azmb_exsitu_full.py \
  --pii_list pii_list.txt \
  --pdf_dir D:/.../pdfs \
  --supp_dir D:/.../supplementary_files \
  --xml_dir D:/.../xmls_meta_abs \
  --out_dir D:/.../out \
  --ollama_url http://localhost:11434 \
  --llm_model qwen2.5:14b-instruct

"""




import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
from lxml import etree
from tqdm import tqdm

try:
    import pandas as pd  # optional for CSV convenience
except Exception:
    pd = None

# =============================================================================
# Versioning for reproducible cache keys
# =============================================================================
PIPELINE_VERSION = "v1.0.0"
PROMPT_VERSION = "2026-01-15.master_v1"  # bump whenever you change prompt templates/rules

# =============================================================================
# Logging
# =============================================================================
def setup_logging(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), mode="w", encoding="utf-8"),
        ],
    )
    logging.info(f"Logging to: {log_file}")

# =============================================================================
# Regex & Heuristics (NEVER "loosen" these; we bias to precision)
# =============================================================================
PII_PATTERN = re.compile(r"(S[0-9A-Z]{16})")

WHITESPACE_RE = re.compile(r"\s+")
HYPHEN_LINEBREAK_RE = re.compile(r"(\w)-\s+(\w)")

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}

# SI page marker artifacts like "S-2" at beginning
SI_PAGE_MARKER_RE = re.compile(r"^\s*S-\d+\b\s*", re.IGNORECASE)

# Caption labels (anywhere / start)
CAPTION_LABEL_ANYWHERE_RE = re.compile(
    r"\b(?:Figure|Fig\.?|Table|Tab\.?|Scheme|Sch\.?|Chart|Graph)\s*(?:S?\d+[A-Za-z]?|[IVX]+|[A-Z])\b",
    re.IGNORECASE,
)
CAPTION_START_RE = re.compile(
    r"^\s*(?:Figure|Fig\.?|Table|Tab\.?|Scheme|Sch\.?|Chart|Graph)\s*(?:S?\d+[A-Za-z]?|[IVX]+|[A-Z])\b",
    re.IGNORECASE,
)
S_ONLY_START_RE = re.compile(r"^\s*S\d+\s*[\.:]\s+", re.IGNORECASE)

# Results/analysis-ish (hard NO if dominates + no recipe signals)
RESULTSISH_RE = re.compile(
    r"\b(results?|discussion|conclusion|summary|findings|analysis|performance|mechanism|regulation|evolution|behavior|"
    r"kinetics|dynamics|cycling|rate capability|capacity|reversibility|nucleation|deposition behavior)\b",
    re.IGNORECASE,
)

# Measurement/characterization-only signals
CHAR_OR_MEASURE_RE = re.compile(
    r"\b(characterization(s)?|electrochemical measurement(s)?|electrochemical testing|measurement methods?)\b|"
    r"\b(XRD|SEM|TEM|XPS|Raman|FTIR|BET|AFM|EIS|CV|LSV|GCD)\b",
    re.IGNORECASE,
)
META_SECTION_RE = re.compile(
    r"\b("
    r"introduction|background|related work|literature review|"
    r"conclusion(s)?|summary|outlook|perspective|"
    r"declaration of competing interest(s)?|conflict(s)? of interest(s)?|"
    r"credit authorship|author contribution(s)?|"
    r"acknowledg(e)?ment(s)?|funding|grant|"
    r"data availability|code availability|materials availability|resource availability|lead contact|"
    r"ethics|consent|"
    r"references|bibliography"
    r")\b",
    re.IGNORECASE
)
# Method verbs / conditions / units
METHOD_VERB_RE = re.compile(
    r"\b(prepar|synthes|fabricat|construct|assembl|coat|deposit|grow|graft|cast|spray|spin[- ]coat|dip[- ]coat|"
    r"soak|immerse|dipp|dry|anneal|calcina|cure|stir|mix|dissolv|filter|wash|centrifug)\w*\b",
    re.IGNORECASE,
)
UNIT_RE = re.compile(
    r"\b(\d+(\.\d+)?\s*(mg|g|kg|mL|L|µL|mmol|mol|M|wt%|vol%|°C|K|h|min|s|rpm))\b",
    re.IGNORECASE,
)
CONDITION_RE = re.compile(
    r"\b(overnight|room temperature|RT|under vacuum|argon|nitrogen|air[- ]dry|freeze[- ]dry|pH|stirring)\b",
    re.IGNORECASE,
)

# Zn / anode / coating signals
ZN_SIGNAL_RE = re.compile(
    r"\b(zinc|zn)\b|@zn|zn@\b|zn\s*foil\b|zn\s*anode\b|anode\b|negative electrode\b|"
    r"protective layer\b|artificial (sei|layer)\b|interphase\b|interface layer\b|coated\s+zn\b|modified\s+zn\b",
    re.IGNORECASE,
)

# Ex-situ vs in-situ cues (title/abstract level)
EXSITU_CUE_RE = re.compile(
    r"\b(ex[- ]situ|artificial\s+sei|pre[- ]coated|precoated|pre[- ]formed|preformed|coating|protective layer|"
    r"interlayer|host structure|3d host|modified zn|coated zn|surface treated zn|dip[- ]coating|spin[- ]coating|"
    r"electrodeposition|sputter|spray[- ]coating|casting|grafting)\b",
    re.IGNORECASE,
)
INSITU_CUE_RE = re.compile(
    r"\b(in[- ]situ|electrolyte additive|additive|salt additive|solvation structure|water[- ]in[- ]salt|WIS|"
    r"electrolyte engineering|electrolyte formulation|SEI formation during cycling)\b",
    re.IGNORECASE,
)

# Lab-scale experiment cues
LAB_CUE_RE = re.compile(
    r"\b(we prepared|we fabricated|we synthesized|we constructed|we coated|we assembled|we demonstrated|"
    r"symmetric\s*zn\|\|zn|coin cell|pouch cell|full cell|zn\|\|mnO2|zn\|\|v2o5|zn\|\|iodine|zn\|\|cu|three[- ]electrode|"
    r"electrochemical performance was evaluated|cycling tests|rate tests)\b",
    re.IGNORECASE,
)
THEORY_ONLY_RE = re.compile(
    r"\b(DFT|density functional theory|first[- ]principles|simulation[- ]only|modeling[- ]only|"
    r"finite element|phase[- ]field|theoretical study|computational study)\b",
    re.IGNORECASE,
)
REVIEW_PUB_RE = re.compile(r"\b(review|perspective|roadmap|overview)\b", re.IGNORECASE)

# =============================================================================
# Utility
# =============================================================================
def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00ad", "")  # soft hyphen
    s = s.replace("\n", " ")
    s = HYPHEN_LINEBREAK_RE.sub(r"\1\2", s)
    s = WHITESPACE_RE.sub(" ", s).strip()
    return s

def strip_si_page_marker(s: str) -> str:
    return SI_PAGE_MARKER_RE.sub("", s or "")

def extract_pii(text: str) -> Optional[str]:
    if not text:
        return None
    m = PII_PATTERN.search(text)
    return m.group(1) if m else None

def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

# =============================================================================
# GROBID Client
# =============================================================================
@dataclass
class GrobidClient:
    base_url: str
    timeout_sec: int = 300

    def __post_init__(self):
        self.session = requests.Session()

    def is_alive(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/api/isalive", timeout=2)
            return r.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def process_fulltext(self, pdf_path: Path, segment_sentences: bool = True,
                         generate_ids: bool = True, tei_coordinates: Optional[str] = None) -> str:
        url = f"{self.base_url}/api/processFulltextDocument"
        with pdf_path.open("rb") as f:
            files = {"input": (pdf_path.name, f, "application/pdf")}
            data = {
                "segmentSentences": "1" if segment_sentences else "0",
                "generateIDs": "1" if generate_ids else "0",
            }
            if tei_coordinates:
                data["teiCoordinates"] = tei_coordinates
            r = self.session.post(url, files=files, data=data, timeout=self.timeout_sec)
            r.raise_for_status()
            return r.text

def parse_tei_xml(tei_xml: str) -> etree._Element:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(tei_xml.encode("utf-8"), parser=parser)

# =============================================================================
# TEI Extractors (Body sections + figure/table captions)
# =============================================================================
def element_text_without_banned(elem: etree._Element, banned_tags: Set[str]) -> str:
    parts: List[str] = []
    def walk(e: etree._Element):
        tag = etree.QName(e).localname
        if tag in banned_tags:
            return
        if e.text:
            parts.append(e.text)
        for child in e:
            walk(child)
            if child.tail:
                parts.append(child.tail)
    walk(elem)
    return normalize_text("".join(parts))

def extract_title(root: etree._Element) -> str:
    t = root.xpath("//tei:teiHeader//tei:titleStmt/tei:title[@type='main']/text()", namespaces=TEI_NS)
    if not t:
        t = root.xpath("//tei:teiHeader//tei:titleStmt/tei:title/text()", namespaces=TEI_NS)
    return normalize_text(t[0]) if t else ""

def extract_abstract_paragraphs(root: etree._Element) -> List[str]:
    ps = root.xpath("//tei:teiHeader//tei:profileDesc//tei:abstract//tei:p", namespaces=TEI_NS)
    out: List[str] = []
    for p in ps:
        txt = element_text_without_banned(p, banned_tags={"ref", "note"})
        txt = strip_si_page_marker(txt)
        if txt:
            out.append(txt)
    return out

def nearest_div_is_self(p: etree._Element, div: etree._Element) -> bool:
    anc = p.xpath("ancestor::tei:div[1]", namespaces=TEI_NS)
    return bool(anc) and (anc[0] is div)

def is_inside_any(elem: etree._Element, ancestor_localnames: Set[str]) -> bool:
    cur = elem.getparent()
    while cur is not None:
        if etree.QName(cur).localname in ancestor_localnames:
            return True
        cur = cur.getparent()
    return False

def section_path_from_ancestors(div: etree._Element) -> str:
    """Build a stable-ish section path using ancestor heads."""
    heads = []
    anc_divs = div.xpath("ancestor::tei:div", namespaces=TEI_NS)
    for d in anc_divs:
        h = d.xpath("./tei:head", namespaces=TEI_NS)
        if h:
            heads.append(normalize_text(" ".join(h[0].itertext())))
    h_self = div.xpath("./tei:head", namespaces=TEI_NS)
    if h_self:
        heads.append(normalize_text(" ".join(h_self[0].itertext())))
    heads = [strip_si_page_marker(x) for x in heads if x]
    return " / ".join(heads)

def extract_body_sections(root: etree._Element) -> List[Dict[str, Any]]:
    SKIP_ANCESTORS = {"listBibl", "biblStruct", "bibl", "back", "note", "figure", "figDesc", "table"}
    divs = root.xpath("//tei:text/tei:body//tei:div", namespaces=TEI_NS)
    sections: List[Dict[str, Any]] = []

    for div in divs:
        head = div.xpath("./tei:head", namespaces=TEI_NS)
        heading = normalize_text(" ".join(head[0].itertext())) if head else ""
        heading = strip_si_page_marker(heading)

        ps = div.xpath(".//tei:p", namespaces=TEI_NS)
        paragraphs: List[str] = []
        sentences_by_paragraph: List[List[str]] = []

        for p in ps:
            if not nearest_div_is_self(p, div):
                continue
            if is_inside_any(p, SKIP_ANCESTORS):
                continue

            s_nodes = p.xpath("./tei:s", namespaces=TEI_NS)
            if s_nodes:
                sents: List[str] = []
                for s in s_nodes:
                    stxt = element_text_without_banned(s, banned_tags={"ref", "note"})
                    stxt = strip_si_page_marker(stxt)
                    if stxt:
                        sents.append(stxt)
                ptxt = normalize_text(" ".join(sents))
                if ptxt:
                    paragraphs.append(ptxt)
                    sentences_by_paragraph.append(sents)
                continue

            ptxt = element_text_without_banned(p, banned_tags={"ref", "note"})
            ptxt = strip_si_page_marker(ptxt)
            if ptxt:
                paragraphs.append(ptxt)
                sentences_by_paragraph.append([ptxt])  # keep simple

        if paragraphs:
            level = len(div.xpath("ancestor::tei:div", namespaces=TEI_NS)) + 1
            path = section_path_from_ancestors(div) or heading
            sections.append({
                "kind": "section",
                "level": level,
                "heading": heading,
                "path": path,
                "paragraphs": paragraphs,
                "sentences": sentences_by_paragraph,
            })
    return sections

def table_elem_to_text(table_elem: etree._Element) -> str:
    rows = table_elem.xpath(".//tei:row", namespaces=TEI_NS)
    if not rows:
        return normalize_text(" ".join(table_elem.itertext()))
    out_lines = []
    for r in rows:
        cells = r.xpath("./tei:cell", namespaces=TEI_NS)
        if not cells:
            out_lines.append(normalize_text(" ".join(r.itertext())))
            continue
        line = "\t".join([normalize_text(" ".join(c.itertext())) for c in cells])
        out_lines.append(line)
    return "\n".join([x for x in out_lines if x])

def extract_figure_table_blocks(root: etree._Element) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []

    figures = root.xpath("//tei:text/tei:body//tei:figure", namespaces=TEI_NS)
    for fig in figures:
        ftype = (fig.get("type") or "").lower()
        is_table = (ftype == "table")

        label = normalize_text(" ".join(fig.xpath("./tei:label//text()", namespaces=TEI_NS)))
        head = normalize_text(" ".join(fig.xpath("./tei:head//text()", namespaces=TEI_NS)))
        desc = normalize_text(" ".join(fig.xpath(".//tei:figDesc//text()", namespaces=TEI_NS)))

        p_texts: List[str] = []
        for p in fig.xpath(".//tei:p", namespaces=TEI_NS):
            ptxt = element_text_without_banned(p, banned_tags={"ref", "note"})
            if ptxt:
                p_texts.append(ptxt)
        caption = normalize_text(" ".join([x for x in [desc] + p_texts if x]))

        heading = label or head or ("Table" if is_table else "Figure")
        heading = strip_si_page_marker(heading)
        caption = strip_si_page_marker(caption)
        if not caption:
            continue

        kind = "table_caption" if is_table else "figure_caption"
        blocks.append({
            "kind": kind,
            "heading": heading,
            "paragraphs": [caption],
            "sentences": [[caption]],
        })

    tables = root.xpath("//tei:text/tei:body//tei:table", namespaces=TEI_NS)
    for tb in tables:
        if tb.xpath("ancestor::tei:figure[@type='table']", namespaces=TEI_NS):
            continue
        txt = strip_si_page_marker(table_elem_to_text(tb))
        if not txt:
            continue
        blocks.append({
            "kind": "table_content",
            "heading": "Table",
            "paragraphs": [txt],
            "sentences": [[txt]],
        })

    return blocks

# =============================================================================
# Caption split/detach from body paragraphs (handles flattened SI captions)
# =============================================================================
def find_caption_label_positions(text: str) -> List[Tuple[int, int, str]]:
    t = normalize_text(text)
    out = []
    for m in CAPTION_LABEL_ANYWHERE_RE.finditer(t):
        out.append((m.start(), m.end(), m.group(0)))
    return out

def looks_like_caption_start(text: str) -> bool:
    if not text:
        return False
    t = normalize_text(text)
    return bool(CAPTION_START_RE.match(t) or S_ONLY_START_RE.match(t))

def split_mid_paragraph_caption(text: str) -> Optional[Tuple[str, str, str]]:
    t = normalize_text(text)
    if not t:
        return None
    pos = find_caption_label_positions(t)
    if not pos:
        return None
    first_start, _, first_label = pos[0]
    if first_start <= 0:
        return None
    if first_start < 20:
        return None
    prefix = t[:first_start].strip()
    suffix = t[first_start:].strip()
    if len(suffix) < 10:
        return None
    return prefix, suffix, first_label

def section_kind_is_caption(kind: str) -> bool:
    k = (kind or "").strip().lower()
    return k in {"figure_caption", "table_caption", "caption_from_body", "table_content"}

def heading_is_caption_like(heading: str) -> bool:
    h = normalize_text(heading)
    return bool(h and looks_like_caption_start(h))

def is_method_like_caption(text: str) -> bool:
    t = normalize_text(text)
    if not t:
        return False
    if not METHOD_VERB_RE.search(t):
        return False
    if UNIT_RE.search(t) or CONDITION_RE.search(t):
        return True
    return False

@dataclass
class RemovedItem:
    paper_id: str
    source: str
    heading: str
    index: int
    text: str
    reason: str
    action: str  # "removed" | "methodlike_kept"

def clean_captions_in_doc(
    doc: Dict[str, Any],
    paper_id: str,
    save_methodlike: bool = True,
    drop_caption_blocks_in_kept: bool = True,
) -> Tuple[Dict[str, Any], List[RemovedItem], List[RemovedItem]]:
    removed: List[RemovedItem] = []
    methodlike: List[RemovedItem] = []

    # 0) caption_blocks leakage prevention
    caption_blocks = doc.get("caption_blocks", None)
    if drop_caption_blocks_in_kept:
        if caption_blocks is not None and isinstance(caption_blocks, list):
            for bi, b in enumerate(caption_blocks):
                if not isinstance(b, dict):
                    continue
                heading = normalize_text(b.get("heading", ""))
                paras = b.get("paragraphs", [])
                if not isinstance(paras, list):
                    continue
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
                        reason="caption_blocks removed from kept doc (leak prevention)",
                        action="removed",
                    ))
        doc.pop("caption_blocks", None)

    # 1) abstract_paragraphs
    new_abs: List[str] = []
    abs_list = doc.get("abstract_paragraphs", [])
    if isinstance(abs_list, list):
        for ai, ptxt in enumerate(abs_list):
            txt = normalize_text(str(ptxt))
            if not txt:
                continue

            mid = split_mid_paragraph_caption(txt)
            if mid:
                prefix, suffix, lab = mid
                if prefix:
                    new_abs.append(prefix)
                if save_methodlike and is_method_like_caption(suffix):
                    methodlike.append(RemovedItem(paper_id, "abstract", "", ai, suffix,
                                                  f"Mid-paragraph caption split (label={lab}) -> methodlike",
                                                  "methodlike_kept"))
                else:
                    removed.append(RemovedItem(paper_id, "abstract", "", ai, suffix,
                                               f"Mid-paragraph caption split (label={lab})",
                                               "removed"))
                continue

            if looks_like_caption_start(txt):
                if save_methodlike and is_method_like_caption(txt):
                    methodlike.append(RemovedItem(paper_id, "abstract", "", ai, txt,
                                                  "Caption-like but method-like (kept separately)",
                                                  "methodlike_kept"))
                else:
                    removed.append(RemovedItem(paper_id, "abstract", "", ai, txt,
                                               "Caption start pattern in abstract",
                                               "removed"))
                continue

            new_abs.append(txt)
    doc["abstract_paragraphs"] = new_abs

    # 2) sections
    new_sections: List[Dict[str, Any]] = []
    sections = doc.get("sections", [])
    if isinstance(sections, list):
        for si, sec in enumerate(sections):
            if not isinstance(sec, dict):
                continue

            kind = str(sec.get("kind", "section"))
            heading = normalize_text(sec.get("heading", ""))

            is_caption_section = section_kind_is_caption(kind) or heading_is_caption_like(heading)
            if is_caption_section:
                paras = sec.get("paragraphs", [])
                if isinstance(paras, list):
                    for pi, ptxt in enumerate(paras):
                        txt = normalize_text(str(ptxt))
                        if not txt:
                            continue
                        if save_methodlike and is_method_like_caption(txt):
                            methodlike.append(RemovedItem(
                                paper_id, f"section_{si}", heading, pi, txt,
                                f"Caption section dropped ({kind}) but method-like kept separately",
                                "methodlike_kept"
                            ))
                        else:
                            removed.append(RemovedItem(
                                paper_id, f"section_{si}", heading, pi, txt,
                                f"Caption section dropped ({kind})",
                                "removed"
                            ))
                continue

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
                sents = sents_list[pi] if pi < len(sents_list) else []

                mid = split_mid_paragraph_caption(txt)
                if mid:
                    prefix, suffix, lab = mid
                    if prefix:
                        new_paras.append(prefix)
                        new_sents.append(sents)
                    if save_methodlike and is_method_like_caption(suffix):
                        methodlike.append(RemovedItem(
                            paper_id, f"section_{si}", heading, pi, suffix,
                            f"Mid-paragraph caption split (label={lab}) -> methodlike",
                            "methodlike_kept"
                        ))
                    else:
                        removed.append(RemovedItem(
                            paper_id, f"section_{si}", heading, pi, suffix,
                            f"Mid-paragraph caption split (label={lab})",
                            "removed"
                        ))
                    continue

                if looks_like_caption_start(txt):
                    if save_methodlike and is_method_like_caption(txt):
                        methodlike.append(RemovedItem(
                            paper_id, f"section_{si}", heading, pi, txt,
                            "Caption-like paragraph but method-like kept separately",
                            "methodlike_kept"
                        ))
                    else:
                        removed.append(RemovedItem(
                            paper_id, f"section_{si}", heading, pi, txt,
                            "Caption start pattern (paragraph)",
                            "removed"
                        ))
                    continue

                new_paras.append(txt)
                new_sents.append(sents)

            sec["paragraphs"] = new_paras
            sec["sentences"] = new_sents
            if new_paras or heading:
                new_sections.append(sec)

    doc["sections"] = new_sections
    return doc, removed, methodlike

# =============================================================================
# Section sampling (method-likelihood based) 
# =============================================================================
def paragraph_score(p: str) -> int:
    if not p:
        return 0
    s = 0
    if METHOD_VERB_RE.search(p):
        s += 3
    if UNIT_RE.search(p):
        s += 3
    if ZN_SIGNAL_RE.search(p):
        s += 2
    if CAPTION_START_RE.match(p.strip()) or S_ONLY_START_RE.match(p.strip()):
        s -= 3
    if RESULTSISH_RE.search(p):
        s -= 1
    return s

def build_section_content(paragraphs: List[str], max_chars: int, max_paras: int) -> Tuple[str, Dict[str, Any]]:
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

    scored = [(paragraph_score(p), i, p) for i, p in enumerate(paras)]
    scored.sort(key=lambda x: (x[0], -len(x[2])), reverse=True)

    selected: List[Tuple[int, str]] = []
    used = 0

    for _, i, p in scored:
        if len(selected) >= max_paras:
            break
        if used + len(p) + 2 > max_chars:
            continue
        selected.append((i, p))
        used += len(p) + 2

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

    try_add(0); try_add(1)
    try_add(len(paras) - 2); try_add(len(paras) - 1)

    selected.sort(key=lambda x: x[0])
    content = "\n\n".join(p for _, p in selected)
    meta["used_paras"] = len(selected)
    meta["used_chars"] = len(content)
    meta["truncated"] = (meta["used_paras"] < meta["total_paras"]) or (meta["used_chars"] < meta["total_chars"])
    return content, meta

# =============================================================================
# Prefilter (precision-first)
# =============================================================================
def is_obvious_no_section(heading: str, content: str) -> Optional[str]:
    h = normalize_text(heading or "").lower()
    c = normalize_text(content or "")

    # 0) meta/boilerplate sections (hard NO)
    if META_SECTION_RE.search(h):
        return "Meta/boilerplate section (intro/conclusion/conflict/funding/references etc.)."

    # 1) caption-like heading
    if looks_like_caption_start(heading):
        return "Caption-like heading (Figure/Table/Scheme)."

    # 2) measurement/characterization-only heading & content lacks strong recipe signals
    if CHAR_OR_MEASURE_RE.search(h) and not (METHOD_VERB_RE.search(c) and (UNIT_RE.search(c) or CONDITION_RE.search(c))):
        return "Characterization/measurement-only heading and no clear procedural recipe in content."

    # 3) results-ish dominates heading & no recipe signals
    if RESULTSISH_RE.search(h) and not (METHOD_VERB_RE.search(c) and (UNIT_RE.search(c) or CONDITION_RE.search(c))):
        return "Results/analysis-oriented heading with no clear procedural recipe in content."

    return None

def is_obvious_yes_section(heading: str, content: str) -> Optional[str]:
    h = normalize_text(heading or "").lower()
    c = normalize_text(content or "")

    # umbrella methods headings
    if re.search(r"\b(materials and methods|materials & methods|methods|experimental|experimental section|"
                 r"experimental details|methodology)\b", h, flags=re.IGNORECASE):
        # still require not being purely results/measurement only
        if not RESULTSISH_RE.search(h):
            return "Methods/Experimental umbrella heading."

    # explicit prep/fabrication in heading + some procedural signals
    if METHOD_VERB_RE.search(h) and (METHOD_VERB_RE.search(c) or UNIT_RE.search(c) or CONDITION_RE.search(c)):
        return "Preparation/fabrication cue in heading with procedural signals in content."

    return None

# =============================================================================
# Ollama client + robust JSON parse
# =============================================================================
def extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()

    # strip code fences
    if "```" in t:
        if "```json" in t:
            t = t.split("```json", 1)[1]
            t = t.split("```", 1)[0].strip()
        else:
            parts = t.split("```")
            if len(parts) >= 2:
                t = parts[1].strip()

    # direct parse
    try:
        return json.loads(t)
    except Exception:
        pass

    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if not m:
        return None
    cand = m.group(0)
    try:
        return json.loads(cand)
    except Exception:
        return None

def ollama_generate_http(
    ollama_url: str,
    model: str,
    prompt: str,
    timeout: int,
    temperature: float,
    top_p: float,
) -> str:
    r = requests.post(
        f"{ollama_url}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "top_p": top_p},
        },
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    return (data.get("response") or "").strip()

def ollama_generate_cli(model: str, prompt: str) -> str:
    cmd = ["ollama", "run", model, "--format", "json"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate(prompt)
    if err:
        e = err.strip()
        if e:
            logging.warning(f"Ollama STDERR: {e}")
    return out.strip() if out else ""

def call_llm_with_retries(
    mode: str,
    ollama_url: str,
    model: str,
    prompt: str,
    timeout: int,
    temperature: float,
    top_p: float,
    max_retries: int,
    backoff_sec: float,
) -> Tuple[str, Optional[Dict[str, Any]], str]:
    last_err = ""
    for attempt in range(max_retries):
        try:
            if mode == "cli":
                raw = ollama_generate_cli(model, prompt)
                parsed = extract_first_json_object(raw)
                return raw, parsed, ""
            else:
                raw = ollama_generate_http(ollama_url, model, prompt, timeout, temperature, top_p)
                parsed = extract_first_json_object(raw)
                return raw, parsed, ""
        except requests.Timeout:
            last_err = "Timeout"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
        time.sleep(backoff_sec * (attempt + 1))
    return "", None, f"LLM call failed after retries: {last_err}"

# =============================================================================
# Cache (disk JSONL)
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
# Prompt Templates (Upgraded Master Set)
# =============================================================================
# 1. Removed 'f' prefix to disable auto-formatting at definition time.
# 2. Used {{double braces}} for JSON examples (literals).
# 3. Used {single braces} for variables to be injected via .format().

SECTION_CLASSIFICATION_PROMPT = """You are an expert in mining METHODS/RECIPE evidence in aqueous zinc-ion batteries (AZIB/AZMB),
with special focus on Zn METAL ANODE EX-SITU protective layers (pre-cycling coatings / artificial interphases / engineered interfaces).

You will receive:
1) Section heading
2) Section content (paragraphs; may be truncated)

Your task:
Decide if this section is LIKELY to contain PROCEDURAL experimental details (materials, steps, conditions)
relevant to:
- Ex-situ Zn anode surface treatment / coating / protective layer fabrication, OR
- Cell/battery assembly / electrolyte preparation / protocol-like methods in AZIB/AZMB.

STRICT RULES (precision-first)
==============================
HARD-NO (override) ❌
Return NO if the section is mainly:
- results/discussion/analysis/performance/mechanism/evolution, OR
- characterization-only (XRD/SEM/TEM/XPS/Raman/FTIR/BET/AFM etc.) without fabrication/protocol steps, OR
- electrochemical measurement/testing-only (CV/EIS/LSV/GCD etc.) without fabrication/protocol steps, OR
- figure/table/scheme/caption-only content.

Do NOT answer YES just because you see characterization or electrochemical terms.

HARD-YES (override) ✅
Return YES if the content includes any CLEAR procedure signals such as:
- preparation/fabrication/synthesis/coating/deposition/growth/dip-coating/soaking/drying/annealing steps
- units/conditions (mg, mL, mmol, °C, h, rpm, vacuum, etc.) in a procedural context
- Zn foil/anode treatment steps, coated/modified Zn specimen preparation
- electrolyte preparation or cell assembly protocols

ANTI-HALLUCINATION
==================
- Use ONLY what is explicitly supported by the provided heading and content.
- Section structure is NOT evidence by itself; it is only a hint.

OUTPUT (STRICT JSON ONLY)
=========================
Return ONLY this JSON:
{{"decision":"YES/NO","confidence":0.0-1.0,"reason":"brief, referencing heading/content"}}

INPUT
=====
Section heading: "{heading}"

Section content:
\"\"\"
{content}
\"\"\"
"""

STAGE1_AZMB_PROMPT = """You are an expert in aqueous zinc metal batteries (AZMB).

Goal
----
Decide whether the given paper is primarily about rechargeable aqueous zinc-based batteries (or related systems like capacitors/air-batteries) that use a metallic Zn anode.

Definition and scope
--------------------
Treat a paper as AZMB-related (answer "YES") only if ALL of the following are true:
- The active anode material is metallic zinc (Zn foil, Zn plate, Zn powder, 3D Zn host, etc.).
- The electrochemical cell operates in an aqueous electrolyte (salt dissolved in water, including water-in-salt systems).
- The main topic is energy storage (rechargeable zinc batteries, zinc ion batteries, zinc metal batteries, zinc-air batteries, zinc hybrid capacitors, etc.).

Answer "YES" also when:
- The system is called a "zinc ion hybrid capacitor" or "aqueous zinc ion capacitor" but still uses a Zn metal anode and aqueous electrolyte.
- The system is a "zinc-air battery" (ZAB), provided it uses a Zn metal anode and aqueous electrolyte.

Answer "NO" if:
- The main system is Li/Na/K/Mg/Ca/Al batteries (non-zinc).
- The zinc chemistry is non-aqueous (organic solvent, polymer gel without clear water, ionic liquid) with no clear water-based electrolyte.
- The focus is on Zn corrosion, plating, sensing, photocatalysis, or other electrochemistry not directly targeting rechargeable aqueous zinc batteries.
- The Zn species are only in the cathode host (e.g., Zn2+ intercalation into MnO2) without using a Zn metal anode.

Input
-----
You will see a short metadata block plus title and abstract.

Title: {title}
Abstract: {abstract}

Task
----
Return ONLY this JSON object:

{{
  "is_aqueous_zmb": "YES" or "NO",
  "reason": "<ONE short sentence that explains your decision>"
}}

Note: Zinc-Air Batteries (ZABs) ARE included if they use an aqueous electrolyte.
Now, based on ALL the rules above and ONLY the given text block, return ONLY the JSON object, with no additional text.
"""

STAGE2_EXSITU_LAB_PROMPT = """You are a strict technical auditor for aqueous zinc metal battery research.

Goal
----
Determine if the paper reports an **EX-SITU PROTECTIVE LAYER** applied to the anode.
This includes:
1. Coatings on planar Zn foil.
2. Coatings on 3D Zn hosts (ONLY if an extra protective layer is added).

Inputs
------
Title/Abstract, Structure Hints, Evidence Snippets.

*** CRITICAL RULES (READ CAREFULLY) ***
---------------------------------------

**TARGET (YES) - The "Protective Layer" Criterion:**
You must find a distinct **Ex-situ Surface Layer** or **Artificial SEI**.

**CASE A: Planar Zn Foil (YES)**
- If the substrate is standard Zn foil/plate and it is coated/treated before assembly -> **YES**.
- *Keywords:* Dip-coating, soaking, chemical etching, artificial SEI on Zn foil.

**CASE B: 3D Host / Structured Anode (CONDITIONAL)**
- **SCENARIO 1 (YES):** The authors fabricate a 3D Host (e.g., CNT, Cu foam), plate Zn, AND THEN apply an **ADDITIONAL protective layer** (e.g., polymer, inorganic coating, Nafion, TiO2) on top.
    - *Logic:* Host + Zn + **Layer** = **YES**.
- **SCENARIO 2 (NO):** The authors ONLY fabricate a 3D Host and plate Zn into it to lower local current density, with **NO extra protective coating**.
    - *Logic:* Host + Zn (only) = **NO**.
    - *Reason:* This is just "Structure Design", not "Surface Protection".

**EXCLUSIONS (Hard NO):**
1. **Pure In-situ:** Layer forms *only* during cycling via electrolyte additives.
2. **Hybrid Strategy:** New Coating + New Electrolyte Additive mixed.
3. **Separator/Interlayer:** Membrane NOT bonded to the anode surface.

**TERMINOLOGY TRAP (WARNING):**
- **IGNORE** "In-situ" if the process is: Immerse -> Dry -> Assemble.
- **Electrodeposition:** If used to create the *Protective Layer* (not just plating Zn) before assembly -> **YES**.

Task
----
1. Identify the **Substrate** (Zn Foil vs. 3D Host).
2. If **3D Host**: Check for an **EXTRA** coating layer.
   - Is there a layer *besides* the Zn metal and the Host skeleton?
   - If YES -> Classify as **YES** (ZN_3D_HOST).
   - If NO (just Zn on Skeleton) -> Classify as **NO**.
3. If **Zn Foil**: Check for coating -> **YES** (ZN_EX_SITU_LAYER).

OUTPUT (STRICT JSON ONLY)
-------------------------
{{
  "has_exsitu_protective_layer": "YES/NO",
  "has_lab_scale_experiments": "YES/NO",
  "modification_focus": "ZN_EX_SITU_LAYER/ZN_3D_HOST/ZN_IN_SITU_SEI/ELECTROLYTE/SEPARATOR_INTERLAYER/HYBRID/OTHER",
  "confidence": 0.0-1.0,
  "reason": "Explain the structure. E.g., '3D Host + Zn + Nafion coating (YES)' or 'Just 3D Host + Zn (NO)' or 'Coated Zn Foil (YES)'."
}}

INPUT
-----
Title:
{title}

Abstract:
{abstract}

Body section structure (HINT ONLY):
{body_structure}

Supplementary section structure (HINT ONLY):
{supp_structure}

Evidence snippets (explicit text only):
{evidence_snippets}
"""
# =============================================================================
# Validation helpers (force allowed values)
# =============================================================================
def clamp01(x: Any, default: float = 0.5) -> float:
    try:
        v = float(x)
    except Exception:
        v = default
    return max(0.0, min(1.0, v))

def ensure_yesno(v: Any) -> str:
    s = str(v or "").strip().upper()
    return s if s in {"YES", "NO"} else "NO"

def ensure_focus(v: Any) -> str:
    allowed = {
        "ZN_EX_SITU_LAYER",
        "ZN_IN_SITU_SEI_OR_ADDITIVE",
        "ELECTROLYTE_ONLY",
        "CATHODE_OR_SEPARATOR_MOD",
        "OTHER",
    }
    s = str(v or "").strip()
    return s if s in allowed else "OTHER"

# =============================================================================
# XML metadata (optional, Elsevier META_ABS)
# =============================================================================
def parse_xml_metadata(xml_dir: Optional[Path], pii: str) -> Tuple[str, str]:
    if not xml_dir:
        return "", ""
    xml_path = xml_dir / f"{pii}"
    if not xml_path.exists():
        return "", ""
    title = ""
    abstract = ""
    try:
        tree = etree.parse(str(xml_path))
        root = tree.getroot()
        namespaces = {"dc": "http://purl.org/dc/elements/1.1/", "ce": "http://www.elsevier.com/xml/common/dtd"}

        tnode = root.find(".//dc:title", namespaces)
        if tnode is None:
            tnode = root.find(".//{http://purl.org/dc/elements/1.1/}title")
        if tnode is not None and tnode.text:
            title = tnode.text.strip()

        dnode = root.find(".//dc:description", namespaces)
        if dnode is not None and dnode.text:
            abstract = dnode.text.strip()
    except Exception:
        pass
    return title, abstract

# =============================================================================
# Supplementary conversion (DOC/DOCX > PDF) (optional)
# =============================================================================
def get_libreoffice_cmd() -> Optional[str]:
    for cmd in ["soffice", "libreoffice"]:
        if shutil.which(cmd):
            return cmd
    if sys.platform == "win32":
        possible = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for p in possible:
            if Path(p).exists():
                return p
    return None

def convert_to_pdf_libreoffice(source_file: Path) -> Optional[Path]:
    target_pdf = source_file.with_suffix(".pdf")
    if target_pdf.exists():
        return target_pdf
    lo = get_libreoffice_cmd()
    if not lo:
        logging.warning(f"[LibreOffice missing] Cannot convert: {source_file}")
        return None
    try:
        cmd = [lo, "--headless", "--convert-to", "pdf", "--outdir", str(source_file.parent), str(source_file)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return target_pdf if target_pdf.exists() else None
    except Exception as e:
        logging.warning(f"[LibreOffice convert failed] {source_file.name} | {e}")
        return None

# =============================================================================
# File discovery
# =============================================================================
def find_main_pdf_by_pii(pdf_dir: Path, pii: str) -> Optional[Path]:
    if not pdf_dir.exists():
        return None
    # prioritize direct filename match
    candidates = []
    for p in pdf_dir.glob("*.pdf"):
        if extract_pii(p.name) == pii:
            candidates.append(p)
    if candidates:
        # if multiple, choose latest modified
        return max(candidates, key=lambda x: x.stat().st_mtime)
    # fallback: search recursively
    for p in pdf_dir.rglob("*.pdf"):
        if extract_pii(str(p)) == pii:
            candidates.append(p)
    return max(candidates, key=lambda x: x.stat().st_mtime) if candidates else None

def find_supp_files_by_pii(supp_dir: Path, pii: str, exts: Set[str]) -> List[Path]:
    if not supp_dir.exists():
        return []
    out = []
    for p in supp_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in exts:
            continue
        if extract_pii(str(p)) == pii:
            out.append(p)
    # stable order
    out.sort(key=lambda x: (x.suffix.lower(), x.name.lower()))
    return out

# =============================================================================
# Section iteration / structure serialization
# =============================================================================
def headings_structure(sections: List[Dict[str, Any]], max_items: int = 250) -> str:
    """Return a compact bullet list of section paths/headings (HINT ONLY)."""
    lines: List[str] = []
    for sec in sections[:max_items]:
        h = normalize_text(sec.get("heading", ""))
        p = normalize_text(sec.get("path", ""))
        lvl = sec.get("level", 0)
        if not h and not p:
            continue
        label = p if p else h
        lines.append(f"- L{lvl}: {label}")
    if len(sections) > max_items:
        lines.append(f"... ({len(sections) - max_items} more)")
    return "\n".join(lines) if lines else "(none)"

# =============================================================================
# Section classification
# =============================================================================
def classify_section_llm(
    heading: str,
    paragraphs: List[str],
    args: argparse.Namespace,
    cache: Dict[str, Dict[str, Any]],
    cache_path: Path,
    paper_dir: Path,
    paper_id: str,
    section_key: str,
) -> Dict[str, Any]:
    content, meta = build_section_content(paragraphs, args.max_chars, args.max_paras)

    # min paragraphs gate (precision-first)
    real_paras = [p for p in paragraphs if isinstance(p, str) and p.strip()]
    if len(real_paras) < args.min_paras:
        return {
            "decision": "SKIP",
            "confidence": 0.0,
            "reason": f"Too few paragraphs (<{args.min_paras})",
            "used_paras": 0,
            "total_paras": len(real_paras),
            "truncated": False,
            "prefiltered": False,
            "cache_hit": False,
            "content_excerpt": "",
        }

    # prefilter
    if not args.no_prefilter:
        r_no = is_obvious_no_section(heading, content)
        if r_no:
            return {
                "decision": "NO",
                "confidence": 0.95,
                "reason": f"[PREFILTER] {r_no}",
                "used_paras": meta["used_paras"],
                "total_paras": meta["total_paras"],
                "truncated": meta["truncated"],
                "prefiltered": True,
                "cache_hit": False,
                "content_excerpt": (content[:600] + " ...") if len(content) > 700 else content,
            }
        r_yes = is_obvious_yes_section(heading, content)
        if r_yes and args.prefilter_allow_yes:
            return {
                "decision": "YES",
                "confidence": 0.85,
                "reason": f"[PREFILTER] {r_yes}",
                "used_paras": meta["used_paras"],
                "total_paras": meta["total_paras"],
                "truncated": meta["truncated"],
                "prefiltered": True,
                "cache_hit": False,
                "content_excerpt": (content[:600] + " ...") if len(content) > 700 else content,
            }

    prompt = SECTION_CLASSIFICATION_PROMPT.format(heading=heading, content=content)

    cache_key = sha1_text(f"{PROMPT_VERSION}|SECTION|{heading}\n\n{content}".strip())
    if args.use_cache and cache_key in cache:
        c = cache[cache_key]
        return {
            "decision": c.get("decision", "NO"),
            "confidence": clamp01(c.get("confidence", 0.0), 0.5),
            "reason": str(c.get("reason", "")),
            "used_paras": meta["used_paras"],
            "total_paras": meta["total_paras"],
            "truncated": meta["truncated"],
            "prefiltered": False,
            "cache_hit": True,
            "content_excerpt": (content[:600] + " ...") if len(content) > 700 else content,
        }

    # save prompt artifacts (optional per-section)
    if args.save_prompts:
        sec_dir = paper_dir / "section_prompts"
        safe_mkdir(sec_dir)
        (sec_dir / f"{section_key}_heading.txt").write_text(heading, encoding="utf-8")
        (sec_dir / f"{section_key}_content.txt").write_text(content, encoding="utf-8")
        (sec_dir / f"{section_key}_prompt.txt").write_text(prompt, encoding="utf-8")

    raw, parsed, err = call_llm_with_retries(
        mode=args.ollama_mode,
        ollama_url=args.ollama_url,
        model=args.llm_model,
        prompt=prompt,
        timeout=args.timeout,
        temperature=args.temperature,
        top_p=args.top_p,
        max_retries=args.retries,
        backoff_sec=args.backoff,
    )

    if args.save_prompts:
        sec_dir = paper_dir / "section_prompts"
        (sec_dir / f"{section_key}_output_raw.txt").write_text(raw or "", encoding="utf-8")

    if not parsed or "decision" not in parsed:
        decision, conf, reason = "ERROR", 0.0, (err or "LLM output not parseable as required JSON")
    else:
        decision = str(parsed.get("decision", "")).strip().upper()
        decision = decision if decision in {"YES", "NO"} else "ERROR"
        conf = clamp01(parsed.get("confidence", 0.5), 0.5)
        reason = str(parsed.get("reason", "")).strip()

    if args.use_cache:
        entry = {
            "cache_key": cache_key,
            "kind": "SECTION",
            "paper_id": paper_id,
            "section_key": section_key,
            "decision": decision if decision in {"YES", "NO"} else "NO",
            "confidence": conf,
            "reason": reason,
            "model": args.llm_model,
            "prompt_version": PROMPT_VERSION,
            "created_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
        }
        append_cache(cache_path, entry)
        cache[cache_key] = entry

    return {
        "decision": decision,
        "confidence": conf,
        "reason": reason,
        "used_paras": meta["used_paras"],
        "total_paras": meta["total_paras"],
        "truncated": meta["truncated"],
        "prefiltered": False,
        "cache_hit": False,
        "content_excerpt": (content[:600] + " ...") if len(content) > 700 else content,
    }

# =============================================================================
# Document-level classification (Stage1 + Stage2)
# =============================================================================
def classify_stage1_azmb(
    source_file: str,
    title: str,
    abstract: str,
    journal: str,
    pubtype: str,
    args: argparse.Namespace,
    cache: Dict[str, Dict[str, Any]],
    cache_path: Path,
    paper_dir: Path,
) -> Dict[str, Any]:
    prompt = STAGE1_AZMB_PROMPT.format(
        source_file=source_file,
        journal=journal,
        pubtype=pubtype,
        title=title,
        abstract=abstract,
    )

    cache_key = sha1_text(f"{PROMPT_VERSION}|STAGE1|{source_file}\n{title}\n{abstract}".strip())
    if args.use_cache and cache_key in cache:
        c = cache[cache_key]
        return {
            "is_aqueous_zmb": ensure_yesno(c.get("is_aqueous_zmb")),
            "confidence": clamp01(c.get("confidence", 0.5), 0.5),
            "reason": str(c.get("reason", "")),
            "cache_hit": True,
        }

    if args.save_prompts:
        (paper_dir / "stage1_prompt.txt").write_text(prompt, encoding="utf-8")

    raw, parsed, err = call_llm_with_retries(
        mode=args.ollama_mode,
        ollama_url=args.ollama_url,
        model=args.llm_model,
        prompt=prompt,
        timeout=args.timeout,
        temperature=args.temperature,
        top_p=args.top_p,
        max_retries=args.retries,
        backoff_sec=args.backoff,
    )

    if args.save_prompts:
        (paper_dir / "stage1_output_raw.txt").write_text(raw or "", encoding="utf-8")

    if not parsed:
        out = {"is_aqueous_zmb": "NO", "confidence": 0.0, "reason": err or "Unparseable JSON"}
    else:
        out = {
            "is_aqueous_zmb": ensure_yesno(parsed.get("is_aqueous_zmb")),
            "confidence": clamp01(parsed.get("confidence", 0.5), 0.5),
            "reason": str(parsed.get("reason", "")).strip(),
        }

    if args.use_cache:
        entry = {
            "cache_key": cache_key,
            "kind": "STAGE1",
            "is_aqueous_zmb": out["is_aqueous_zmb"],
            "confidence": out["confidence"],
            "reason": out["reason"],
            "model": args.llm_model,
            "prompt_version": PROMPT_VERSION,
            "created_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
        }
        append_cache(cache_path, entry)
        cache[cache_key] = entry

    out["cache_hit"] = False
    return out

def classify_stage2_exsitu_lab(
    source_file: str,
    title: str,
    abstract: str,
    journal: str,
    pubtype: str,
    body_structure: str,
    supp_structure: str,
    evidence_snippets: str,
    args: argparse.Namespace,
    cache: Dict[str, Dict[str, Any]],
    cache_path: Path,
    paper_dir: Path,
) -> Dict[str, Any]:
    # [수정] 프롬프트 포맷팅에서 불필요한 변수 제거 (source_file, journal, pubtype 삭제)
    # 새로 업데이트된 STAGE2_EXSITU_LAB_PROMPT는 아래 5개 인자만 필요로 합니다.
    prompt = STAGE2_EXSITU_LAB_PROMPT.format(
        title=title,
        abstract=abstract,
        body_structure=body_structure,
        supp_structure=supp_structure,
        evidence_snippets=evidence_snippets,
    )

    # [수정] 캐시 키 생성: 
    # 1. PROMPT_VERSION을 포함하여 프롬프트 변경 시 캐시 무효화
    # 2. source_file을 포함하여 파일별 고유성 보장 (내용이 같아도 파일이 다르면 구분)
    cache_content = f"{PROMPT_VERSION}|STAGE2|{source_file}|{title}\n{abstract}\n{body_structure}\n{supp_structure}\n{evidence_snippets}".strip()
    cache_key = sha1_text(cache_content)
    
    # 캐시 확인
    if args.use_cache and cache_key in cache:
        c = cache[cache_key]
        return {
            "has_exsitu_protective_layer": ensure_yesno(c.get("has_exsitu_protective_layer")),
            "has_lab_scale_experiments": ensure_yesno(c.get("has_lab_scale_experiments")),
            "modification_focus": ensure_focus(c.get("modification_focus")),
            "confidence": clamp01(c.get("confidence", 0.5), 0.5),
            "reason": str(c.get("reason", "")),
            "cache_hit": True,
        }

    # 프롬프트 저장 (디버깅용)
    if args.save_prompts:
        (paper_dir / "stage2_prompt.txt").write_text(prompt, encoding="utf-8")

    # LLM 호출
    raw, parsed, err = call_llm_with_retries(
        mode=args.ollama_mode,
        ollama_url=args.ollama_url,
        model=args.llm_model,
        prompt=prompt,
        timeout=args.timeout,
        temperature=args.temperature,
        top_p=args.top_p,
        max_retries=args.retries,
        backoff_sec=args.backoff,
    )

    # LLM 응답 원본 저장
    if args.save_prompts:
        (paper_dir / "stage2_output_raw.txt").write_text(raw or "", encoding="utf-8")

    # 결과 파싱 처리
    if not parsed:
        out = {
            "has_exsitu_protective_layer": "NO",
            "has_lab_scale_experiments": "NO",
            "modification_focus": "OTHER",
            "confidence": 0.0,
            "reason": err or "Unparseable JSON",
        }
    else:
        out = {
            "has_exsitu_protective_layer": ensure_yesno(parsed.get("has_exsitu_protective_layer")),
            "has_lab_scale_experiments": ensure_yesno(parsed.get("has_lab_scale_experiments")),
            "modification_focus": ensure_focus(parsed.get("modification_focus")),
            "confidence": clamp01(parsed.get("confidence", 0.5), 0.5),
            "reason": str(parsed.get("reason", "")).strip(),
        }

    # 캐시 저장
    if args.use_cache:
        entry = {
            "cache_key": cache_key,
            "kind": "STAGE2",
            **out,
            "model": args.llm_model,
            "prompt_version": PROMPT_VERSION,
            "created_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
        }
        append_cache(cache_path, entry)
        cache[cache_key] = entry

    out["cache_hit"] = False
    return out

# =============================================================================
# Evidence aggregation (YES sections + methodlike captions)
# =============================================================================
def make_evidence_snippets(
    section_rows: List[Dict[str, Any]],
    methodlike_items: List[RemovedItem],
    max_snippets: int = 10,
    max_chars_total: int = 6000,
) -> str:
    """
    Build a compact evidence block (explicit text only).
    Precision-first: prefer high-confidence YES sections with Zn/procedure signals.
    """
    candidates = []
    for r in section_rows:
        if r.get("decision") != "YES":
            continue
        excerpt = normalize_text(r.get("content_excerpt", ""))
        if not excerpt:
            continue
        score = float(r.get("confidence", 0.0))
        # boost if Zn/procedure in excerpt
        if ZN_SIGNAL_RE.search(excerpt):
            score += 0.2
        if METHOD_VERB_RE.search(excerpt) and (UNIT_RE.search(excerpt) or CONDITION_RE.search(excerpt)):
            score += 0.2
        candidates.append((score, r))

    candidates.sort(key=lambda x: x[0], reverse=True)

    lines: List[str] = []
    used = 0
    n = 0

    for _, r in candidates:
        if n >= max_snippets:
            break
        h = normalize_text(r.get("heading", ""))
        p = normalize_text(r.get("path", ""))
        excerpt = normalize_text(r.get("content_excerpt", ""))
        block = f"[SECTION] {p or h}\n{excerpt}\n"
        if used + len(block) > max_chars_total:
            continue
        lines.append(block)
        used += len(block)
        n += 1

    # add method-like SI captions at the end (explicitly labeled)
    for it in methodlike_items[:max(0, max_snippets - n)]:
        if used >= max_chars_total:
            break
        txt = normalize_text(it.text)
        if not txt:
            continue
        block = f"[METHODLIKE_CAPTION] {it.heading or it.source}\n{txt}\n"
        if used + len(block) > max_chars_total:
            break
        lines.append(block)
        used += len(block)

    return "\n".join(lines).strip() if lines else "(no explicit procedural snippets found)"

# =============================================================================
# Pipeline per PII
# =============================================================================
def grobid_process_pdf(
    client: GrobidClient,
    pdf_path: Path,
    out_tei_path: Path,
    segment_sentences: bool = True,
    tei_coordinates: Optional[str] = None,
) -> str:
    tei = client.process_fulltext(pdf_path, segment_sentences=segment_sentences, generate_ids=True, tei_coordinates=tei_coordinates)
    out_tei_path.write_text(tei, encoding="utf-8")
    return tei

def extract_doc_from_tei(tei_xml: str, source_file: str) -> Dict[str, Any]:
    root = parse_tei_xml(tei_xml)
    title = extract_title(root)
    abstract_paras = extract_abstract_paragraphs(root)
    sections = extract_body_sections(root)
    figtab = extract_figure_table_blocks(root)

    # append caption blocks for tracking; will be removed in cleaning if configured
    doc = {
        "source_file": source_file,
        "title": title,
        "abstract_paragraphs": abstract_paras,
        "sections": sections,
        "caption_blocks": figtab,
    }
    return doc

def run_for_pii(pii: str, args: argparse.Namespace, cache: Dict[str, Dict[str, Any]], cache_path: Path) -> Dict[str, Any]:
    paper_dir = Path(args.out_dir) / pii
    safe_mkdir(paper_dir)

    # find files
    main_pdf = find_main_pdf_by_pii(Path(args.pdf_dir), pii)
    supp_files = find_supp_files_by_pii(Path(args.supp_dir), pii, exts={".pdf", ".doc", ".docx"}) if args.supp_dir else []

    if not main_pdf:
        logging.warning(f"[{pii}] main PDF not found in {args.pdf_dir}")
        return {"paper_id": pii, "status": "MISSING_MAIN_PDF"}

    # setup grobid
    base_url = f"http://{args.grobid_host}:{args.grobid_port}"
    client = GrobidClient(base_url=base_url, timeout_sec=args.grobid_timeout)

    if not client.is_alive():
        raise RuntimeError(f"GROBID not reachable at {base_url}. Start server first.")

    # -----------------------------------------------------------------------------
    # 1) Process main PDF
    # -----------------------------------------------------------------------------
    tei_dir = paper_dir / "tei"
    safe_mkdir(tei_dir)

    main_tei_path = tei_dir / "main.tei.xml"
    if args.force_grobid or not main_tei_path.exists():
        logging.info(f"[{pii}] GROBID main: {main_pdf}")
        tei_main = grobid_process_pdf(client, main_pdf, main_tei_path, segment_sentences=True, tei_coordinates=args.tei_coordinates or None)
    else:
        tei_main = main_tei_path.read_text(encoding="utf-8", errors="ignore")

    main_doc_raw = extract_doc_from_tei(tei_main, source_file=str(main_pdf))

    # -----------------------------------------------------------------------------
    # 2) Process supplementary files (0..N)
    # -----------------------------------------------------------------------------
    supp_docs_raw: List[Dict[str, Any]] = []
    supp_tei_paths: List[Path] = []

    max_supp = args.max_supp_files
    for sf in supp_files[:max_supp]:
        ext = sf.suffix.lower()
        pdf_to_process: Optional[Path] = None
        was_converted = False

        if ext == ".pdf":
            pdf_to_process = sf
        else:
            if args.enable_word_convert:
                pdf_to_process = convert_to_pdf_libreoffice(sf)
                was_converted = True
            else:
                pdf_to_process = None

        if not pdf_to_process:
            continue

        tei_path = tei_dir / f"supp_{sha1_text(str(pdf_to_process.resolve()))[:10]}.tei.xml"
        supp_tei_paths.append(tei_path)

        if args.force_grobid or not tei_path.exists():
            logging.info(f"[{pii}] GROBID supp: {pdf_to_process}")
            tei_s = grobid_process_pdf(client, pdf_to_process, tei_path, segment_sentences=True, tei_coordinates=args.tei_coordinates or None)
        else:
            tei_s = tei_path.read_text(encoding="utf-8", errors="ignore")

        d = extract_doc_from_tei(tei_s, source_file=str(sf))
        d["processed_pdf"] = str(pdf_to_process)
        d["original_file_type"] = ext
        d["was_converted_from_word"] = was_converted
        supp_docs_raw.append(d)

    # -----------------------------------------------------------------------------
    # 3) Optional XML metadata (clean title/abstract)
    # -----------------------------------------------------------------------------
    xml_title, xml_abs = parse_xml_metadata(Path(args.xml_dir) if args.xml_dir else None, pii)
    if xml_title and not main_doc_raw.get("title"):
        main_doc_raw["title"] = xml_title
    if xml_abs and (not main_doc_raw.get("abstract_paragraphs")):
        main_doc_raw["abstract_paragraphs"] = [xml_abs]

    # persist extracted raw
    extracted_dir = paper_dir / "extracted"
    safe_mkdir(extracted_dir)
    (extracted_dir / "main_extracted_raw.json").write_text(json.dumps(main_doc_raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (extracted_dir / "supp_extracted_raw.json").write_text(json.dumps(supp_docs_raw, ensure_ascii=False, indent=2), encoding="utf-8")

    # -----------------------------------------------------------------------------
    # 4) Cleaning captions (main + merge supp)
    # -----------------------------------------------------------------------------
    main_clean, main_removed, main_methodlike = clean_captions_in_doc(
        main_doc_raw,
        paper_id=pii,
        save_methodlike=args.save_methodlike,
        drop_caption_blocks_in_kept=args.drop_caption_blocks_in_kept,
    )

    supp_all_sections: List[Dict[str, Any]] = []
    supp_removed_all: List[RemovedItem] = []
    supp_methodlike_all: List[RemovedItem] = []

    for i, sd in enumerate(supp_docs_raw):
        sd_clean, sd_removed, sd_methodlike = clean_captions_in_doc(
            sd,
            paper_id=pii,
            save_methodlike=args.save_methodlike,
            drop_caption_blocks_in_kept=args.drop_caption_blocks_in_kept,
        )
        supp_removed_all.extend(sd_removed)
        supp_methodlike_all.extend(sd_methodlike)

        secs = sd_clean.get("sections", [])
        if isinstance(secs, list):
            # tag source for traceability
            for s in secs:
                if isinstance(s, dict):
                    s["supp_source_file"] = sd_clean.get("source_file", "")
            supp_all_sections.extend([s for s in secs if isinstance(s, dict)])

    cleaned_dir = paper_dir / "cleaned"
    safe_mkdir(cleaned_dir)
    (cleaned_dir / "main_cleaned.json").write_text(json.dumps(main_clean, ensure_ascii=False, indent=2), encoding="utf-8")
    (cleaned_dir / "supp_sections_merged_cleaned.json").write_text(json.dumps({"paper_id": pii, "sections": supp_all_sections}, ensure_ascii=False, indent=2), encoding="utf-8")

    # removed logs
    removed_dir = paper_dir / "removed"
    safe_mkdir(removed_dir)
    (removed_dir / "main_removed.jsonl").write_text("\n".join(json.dumps(x.__dict__, ensure_ascii=False) for x in main_removed), encoding="utf-8")
    (removed_dir / "supp_removed.jsonl").write_text("\n".join(json.dumps(x.__dict__, ensure_ascii=False) for x in supp_removed_all), encoding="utf-8")
    if args.save_methodlike:
        (removed_dir / "main_methodlike.jsonl").write_text("\n".join(json.dumps(x.__dict__, ensure_ascii=False) for x in main_methodlike), encoding="utf-8")
        (removed_dir / "supp_methodlike.jsonl").write_text("\n".join(json.dumps(x.__dict__, ensure_ascii=False) for x in supp_methodlike_all), encoding="utf-8")

    # -----------------------------------------------------------------------------
    # 5) Section-level classification (main + supp)
    # -----------------------------------------------------------------------------
    section_rows: List[Dict[str, Any]] = []

    # main sections
    main_secs = main_clean.get("sections", [])
    if not isinstance(main_secs, list):
        main_secs = []

    for si, sec in enumerate(main_secs):
        if not isinstance(sec, dict):
            continue
        heading = normalize_text(sec.get("heading", ""))
        paras = sec.get("paragraphs", [])
        if not isinstance(paras, list):
            paras = []
        section_key = f"main_{si:04d}"
        res = classify_section_llm(
            heading=heading,
            paragraphs=paras,
            args=args,
            cache=cache,
            cache_path=cache_path,
            paper_dir=paper_dir,
            paper_id=pii,
            section_key=section_key,
        )
        row = {
            "paper_id": pii,
            "source_kind": "main",
            "source_file": str(main_pdf),
            "section_key": section_key,
            "path": normalize_text(sec.get("path", "")),
            "heading": heading,
            **res,
        }
        section_rows.append(row)

    # supp sections (merged)
    for si, sec in enumerate(supp_all_sections):
        if not isinstance(sec, dict):
            continue
        heading = normalize_text(sec.get("heading", ""))
        paras = sec.get("paragraphs", [])
        if not isinstance(paras, list):
            paras = []
        section_key = f"supp_{si:04d}"
        res = classify_section_llm(
            heading=heading,
            paragraphs=paras,
            args=args,
            cache=cache,
            cache_path=cache_path,
            paper_dir=paper_dir,
            paper_id=pii,
            section_key=section_key,
        )
        row = {
            "paper_id": pii,
            "source_kind": "supp",
            "source_file": str(sec.get("supp_source_file", "")),
            "section_key": section_key,
            "path": normalize_text(sec.get("path", "")),
            "heading": heading,
            **res,
        }
        section_rows.append(row)

    # persist section rows
    (paper_dir / "sections_classification.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in section_rows),
        encoding="utf-8"
    )

    # -----------------------------------------------------------------------------
    # 6) Document-level Stage1/Stage2 classification
    # -----------------------------------------------------------------------------
    title = normalize_text(main_clean.get("title", "")) or normalize_text(xml_title)
    abstract = "\n".join([normalize_text(x) for x in (main_clean.get("abstract_paragraphs") or []) if normalize_text(x)]).strip()
    if not abstract and xml_abs:
        abstract = normalize_text(xml_abs)

    # optional journal/pubtype: if you have metadata mapping, plug it here.
    journal = ""
    pubtype = ""

    if args.save_prompts:
        (paper_dir / "doc_title.txt").write_text(title, encoding="utf-8")
        (paper_dir / "doc_abstract.txt").write_text(abstract, encoding="utf-8")

    s1 = classify_stage1_azmb(
        source_file=str(main_pdf.name),
        title=title,
        abstract=abstract,
        journal=journal,
        pubtype=pubtype,
        args=args,
        cache=cache,
        cache_path=cache_path,
        paper_dir=paper_dir,
    )
    (paper_dir / "stage1_result.json").write_text(json.dumps(s1, ensure_ascii=False, indent=2), encoding="utf-8")

    # If Stage1 NO > skip Stage2 (NA-like)
    if s1.get("is_aqueous_zmb") != "YES":
        result = {
            "paper_id": pii,
            "status": "DONE",
            "title": title,
            "abstract": abstract[:4000],
            "S1_is_aqueous_zmb": s1.get("is_aqueous_zmb", "NO"),
            "S1_confidence": s1.get("confidence", 0.0),
            "S1_reason": s1.get("reason", ""),
            "S2_has_exsitu_protective_layer": "NA",
            "S2_has_lab_scale_experiments": "NA",
            "S2_modification_focus": "",
            "S2_confidence": 0.0,
            "S2_reason": "Skipped because Stage1 is not YES",
            "candidate_exsitu_lab": "NO",
            "main_pdf": str(main_pdf),
            "supp_files": [str(x) for x in supp_files],
            "num_sections_total": len(section_rows),
            "num_sections_yes": sum(1 for r in section_rows if r.get("decision") == "YES"),
        }
        return result

    # Stage2 evidence
    body_struct = headings_structure(main_secs, max_items=args.max_structure_items)
    supp_struct = headings_structure(supp_all_sections, max_items=args.max_structure_items)
    evidence = make_evidence_snippets(
        section_rows=section_rows,
        methodlike_items=(main_methodlike + supp_methodlike_all) if args.save_methodlike else [],
        max_snippets=args.max_evidence_snippets,
        max_chars_total=args.max_evidence_chars,
    )
    if args.save_prompts:
        (paper_dir / "stage2_evidence_snippets.txt").write_text(evidence, encoding="utf-8")

    s2 = classify_stage2_exsitu_lab(
        source_file=str(main_pdf.name),
        title=title,
        abstract=abstract,
        journal=journal,
        pubtype=pubtype,
        body_structure=body_struct,
        supp_structure=supp_struct,
        evidence_snippets=evidence,
        args=args,
        cache=cache,
        cache_path=cache_path,
        paper_dir=paper_dir,
    )
    (paper_dir / "stage2_result.json").write_text(json.dumps(s2, ensure_ascii=False, indent=2), encoding="utf-8")

    candidate = "YES" if (s1["is_aqueous_zmb"] == "YES" and s2["has_exsitu_protective_layer"] == "YES" and s2["has_lab_scale_experiments"] == "YES") else "NO"

    result = {
        "paper_id": pii,
        "status": "DONE",
        "title": title,
        "abstract": abstract[:4000],
        "S1_is_aqueous_zmb": s1.get("is_aqueous_zmb", "NO"),
        "S1_confidence": s1.get("confidence", 0.0),
        "S1_reason": s1.get("reason", ""),
        "S2_has_exsitu_protective_layer": s2.get("has_exsitu_protective_layer", "NO"),
        "S2_has_lab_scale_experiments": s2.get("has_lab_scale_experiments", "NO"),
        "S2_modification_focus": s2.get("modification_focus", "OTHER"),
        "S2_confidence": s2.get("confidence", 0.0),
        "S2_reason": s2.get("reason", ""),
        "candidate_exsitu_lab": candidate,
        "main_pdf": str(main_pdf),
        "supp_files": [str(x) for x in supp_files],
        "num_sections_total": len(section_rows),
        "num_sections_yes": sum(1 for r in section_rows if r.get("decision") == "YES"),
    }
    return result

# =============================================================================
# Main
# =============================================================================
def read_pii_list(path_or_inline: str) -> List[str]:
    p = Path(path_or_inline)
    if p.exists():
        items = []
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            # allow lines containing filenames; extract pii
            pii = extract_pii(line) or line
            if pii and PII_PATTERN.fullmatch(pii):
                items.append(pii)
        return sorted(set(items))
    # inline CSV-like
    toks = [x.strip() for x in re.split(r"[,\s]+", path_or_inline.strip()) if x.strip()]
    items = []
    for t in toks:
        pii = extract_pii(t) or t
        if pii and PII_PATTERN.fullmatch(pii):
            items.append(pii)
    return sorted(set(items))

def write_summary(out_dir: Path, rows: List[Dict[str, Any]]) -> Tuple[Path, Path]:
    safe_mkdir(out_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = out_dir / f"summary_{ts}.jsonl"
    csv_path = out_dir / f"summary_{ts}.csv"

    jsonl_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    if pd is not None:
        pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
    else:
        # csv fallback
        fieldnames = sorted({k for r in rows for k in r.keys()})
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})

    return jsonl_path, csv_path

def main():
    parser = argparse.ArgumentParser(description="End-to-end AZMB ex-situ protective layer pipeline (GROBID + Ollama)")

    # Inputs
    parser.add_argument("--pii_list", required=True, help="PII list file (.txt) or inline string")
    parser.add_argument("--pdf_dir", default="../pdfs", help="Directory containing main PDFs")
    parser.add_argument("--supp_dir", default="../supplementary_files/02_supplementary", help="Directory containing supplementary files (recursive)")
    parser.add_argument("--xml_dir", default="../../Elsevier/xmls_meta_abs", help="Optional Elsevier XML META_ABS directory")
    parser.add_argument("--out_dir", default="./09_20260115_full_pipeline_output", help="Output directory")

    # GROBID
    parser.add_argument("--grobid_host", default="localhost")
    parser.add_argument("--grobid_port", default="8080")
    parser.add_argument("--grobid_timeout", type=int, default=300)
    parser.add_argument("--force_grobid", action="store_true", help="Re-run GROBID even if TEI exists")
    parser.add_argument("--tei_coordinates", default="", help="Optional teiCoordinates param, e.g., figure,table,head,p")

    # Supplementary conversion
    parser.add_argument("--enable_word_convert", action="store_true", help="Enable DOC/DOCX->PDF conversion via LibreOffice")
    parser.add_argument("--max_supp_files", type=int, default=50)

    # LLM (Ollama)
    parser.add_argument("--ollama_mode", choices=["http", "cli"], default="http")
    parser.add_argument("--ollama_url", default="http://localhost:11434")
    parser.add_argument("--llm_model", default="qwen2.5:14b-instruct")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=1.5)

    # Section sampling / filtering
    parser.add_argument("--max_chars", type=int, default=12000)
    parser.add_argument("--max_paras", type=int, default=30)
    parser.add_argument("--min_paras", type=int, default=1)

    # Prefilter / captions
    parser.add_argument("--no_prefilter", action="store_true", help="Disable prefilter (not recommended)")
    parser.add_argument("--prefilter_allow_yes", action="store_true", help="Allow prefilter to directly set YES in obvious cases (still conservative)")
    parser.add_argument("--save_methodlike", action="store_true", help="Preserve method-like captions in separate bucket")
    parser.add_argument("--drop_caption_blocks_in_kept", action="store_true", default=True, help="Drop caption_blocks in kept docs (leak prevention)")

    # Evidence/structure for Stage2
    parser.add_argument("--max_structure_items", type=int, default=250)
    parser.add_argument("--max_evidence_snippets", type=int, default=10)
    parser.add_argument("--max_evidence_chars", type=int, default=6000)

    # Cache & artifacts
    parser.add_argument("--use_cache", action="store_true")
    parser.add_argument("--cache_file", default="", help="Cache JSONL path (default: out_dir/cache.jsonl)")
    parser.add_argument("--save_prompts", action="store_true", help="Save prompts/outputs for auditing (more disk usage)")

    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    safe_mkdir(out_dir)
    setup_logging(out_dir)

    # Cache
    cache_path = Path(args.cache_file) if args.cache_file else (out_dir / "cache.jsonl")
    cache: Dict[str, Dict[str, Any]] = load_cache(cache_path) if args.use_cache else {}

    # PII list
    pii_list = read_pii_list(args.pii_list)
    if not pii_list:
        logging.error("No valid PII found. Expected format: S + 16 alphanumeric.")
        return

    logging.info(f"Pipeline version: {PIPELINE_VERSION} | Prompt version: {PROMPT_VERSION}")
    logging.info(f"PIIs: {len(pii_list)}")
    logging.info(f"Main PDF dir: {args.pdf_dir}")
    logging.info(f"Supp dir: {args.supp_dir or '(none)'}")
    logging.info(f"Out dir: {out_dir}")
    logging.info(f"GROBID: http://{args.grobid_host}:{args.grobid_port}")
    logging.info(f"Ollama mode={args.ollama_mode} model={args.llm_model} cache={'ON' if args.use_cache else 'OFF'}")

    results: List[Dict[str, Any]] = []
    fail: List[Dict[str, Any]] = []

    for pii in tqdm(pii_list, desc="Processing PIIs", unit="paper"):
        try:
            r = run_for_pii(pii, args, cache, cache_path)
            results.append(r)
        except Exception as e:
            logging.exception(f"[{pii}] FAILED: {e}")
            fail.append({"paper_id": pii, "status": "ERROR", "error": str(e)})
            results.append({"paper_id": pii, "status": "ERROR", "error": str(e)})

    # Write summary
    summary_dir = out_dir / "summary"
    safe_mkdir(summary_dir)
    jsonl_path, csv_path = write_summary(summary_dir, results)
    logging.info(f"Summary JSONL: {jsonl_path}")
    logging.info(f"Summary CSV : {csv_path}")

    # Also write failures list
    if fail:
        fail_path = summary_dir / "failures.jsonl"
        fail_path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in fail), encoding="utf-8")
        logging.info(f"Failures JSONL: {fail_path}")

    # Quick stats
    done = sum(1 for r in results if r.get("status") == "DONE")
    cand = sum(1 for r in results if r.get("candidate_exsitu_lab") == "YES")
    missing = sum(1 for r in results if r.get("status") == "MISSING_MAIN_PDF")
    err = sum(1 for r in results if r.get("status") == "ERROR")

    logging.info("=" * 72)
    logging.info("DONE")
    logging.info(f"Total: {len(results)} | DONE: {done} | Candidate YES: {cand} | Missing main PDF: {missing} | ERROR: {err}")
    logging.info("=" * 72)

if __name__ == "__main__":
    main()
'''
Copyright (c) 2026, Kyung Hee University
All rights reserved.

@writer: Yongsang An
@writer email: [yongsang.an@khu.ac.kr]
@date: 2026-01-14
@update: 2026-01-17
'''