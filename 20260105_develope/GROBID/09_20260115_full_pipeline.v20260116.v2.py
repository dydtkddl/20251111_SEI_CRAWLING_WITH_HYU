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

===============================================================================
END-TO-END Ollama + GROBID Pipeline (Single File, Forest Version)
===============================================================================
PII list 입력
  -> (Step 0) XML(META_ABS)에서 Title/Abstract 로드
  -> (Step 1) Stage1(AZMB Gate) [Title+Abstract only]  --> NO면 즉시 종료 (GROBID/섹션채굴/Stage2 스킵)
  -> (Step 2) Stage1=YES인 경우에만 main PDF + supplementary 탐색(PII 매칭)
  -> (Step 3) GROBID TEI 생성(캐시) + TEI 추출(섹션 구조 / 캡션 블록)
  -> (Step 4) SI 캡션 누수 방지 클리닝(figure/table + body 캡션 + mid-paragraph split + leak prevention)
  -> (Step 5) 섹션 단위 LLM 분류(heading+content sampling, prefilter+cache+retry)
            + Early Stopping(고품질 YES 증거 N개 모이면 채굴 중단)
  -> (Step 6) Stage2(최종 판정) [Title+Abstract + body 구조 + supp 구조 + evidence snippets]
  -> (Output) per-PII 아티팩트 + 전체 summary CSV/JSONL + evidence 저장

설계 원칙 (precision-first)
- 모호하면 NO (reason에 왜 모호한지 짧게 기록)
- 캡션/결과/측정-only/메타 섹션을 recipe로 오인하지 않게 HARD-NO 강화
- 섹션 구조는 "힌트"일 뿐이며 내용 환각 금지(프롬프트에 강하게 명시)
- 운영 안정성: cache/retry/logging/robust JSON parse/값 강제(allowed values)

사전 준비
- GROBID 서버 실행:
    docker run --rm -p 8070:8070 lfoppiano/grobid:0.8.0
- Ollama 실행 + 모델 pull:
    ollama pull qwen2.5:14b-instruct

예시 실행
python pipeline_azmb_exsitu_full.py \
  --pii_list pii_list.txt \
  --xml_dir D:/.../xmls_meta_abs \
  --pdf_dir D:/.../pdfs \
  --supp_dir D:/.../supplementary_files \
  --out_dir D:/.../out \
  --grobid_host localhost --grobid_port 8070 \
  --ollama_mode http --ollama_url http://localhost:11434 \
  --llm_model qwen2.5:14b-instruct \
  --use_cache \
  --save_prompts \
  --save_methodlike \
  --early_stop_yes_n 4 \
  --early_stop_min_conf 0.75

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
    import pandas as pd  # optional
except Exception:
    pd = None

# =============================================================================
# Versioning (bump to invalidate cache deterministically)
# =============================================================================
PIPELINE_VERSION = "v1.1.0-forest"
PROMPT_VERSION = "2026-01-17.master_v3"  # bump whenever prompt/rules change
# =============================================================================
# Regex & Heuristics (Dual-Layer Strategy)
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

# Boilerplate/meta sections (heading or path) - Always exclude
META_SECTION_RE = re.compile(
    r"\b("
    r"introduction|background|related work|literature review|"
    r"conclusion(s)?|summary|outlook|perspective|"
    r"declaration of competing interest(s)?|competing interest(s)?|conflict(s)? of interest(s)?|"
    r"credit authorship|author contribution(s)?|contributions|"
    r"acknowledg(e)?ment(s)?|funding|grant|"
    r"data availability|code availability|materials availability|resource availability|lead contact|"
    r"ethics|consent|"
    r"references|bibliography|supporting information|supplementary information|"
    r"abbreviations|nomenclature"
    r")\b",
    re.IGNORECASE
)

# Measurement/characterization-only signals
CHAR_OR_MEASURE_RE = re.compile(
    r"\b(characterization(s)?|electrochemical measurement(s)?|electrochemical testing|measurement methods?)\b|"
    r"\b(XRD|SEM|TEM|XPS|Raman|FTIR|BET|AFM|EIS|CV|LSV|GCD|EDS|STEM)\b",
    re.IGNORECASE,
)

# -----------------------------------------------------------------------------
# [A] BROAD Patterns (For Prefiltering - 안전하게 거르기용)
# -----------------------------------------------------------------------------
METHOD_VERB_RE_BROAD = re.compile(
    r"\b(prepar|synthes|fabricat|construct|assembl|coat|deposit|modif|treat|form|produc|measur|characteriz|perform|conduct)\w*\b",
    re.IGNORECASE,
)

CONDITION_RE_BROAD = re.compile(
    r"\b(°C|K|h|min|s|vacuum|atmosphere|temperature|room temperature|overnight|pressure|flow)\b",
    re.IGNORECASE,
)

UNIT_RE_BROAD = re.compile(
    r"\b(\d+(\.\d+)?)\s*(mg|g|ml|l|mm|cm|h|min|s|v|a|ma|mah|wh|k|c)\b",
    re.IGNORECASE,
)

RESULTSISH_RE_BROAD = re.compile(
    r"\b(result|discussion|conclusion|summary|performance|characterization)\b",
    re.IGNORECASE,
)

# -----------------------------------------------------------------------------
# [B] STRICT/AUGMENTED Patterns (For Scoring - 정밀 타격용)
# -----------------------------------------------------------------------------

# [Optimized] Method Verbs: Zn Ex-situ Layer 제조 공정 정밀 타격
METHOD_VERB_RE = re.compile(
    r"\b("
    r"polish|sand|abras|clean|rins|wash|wip|etch|pickling|"
    r"coat|cast|drop[- ]cast|spin[- ]coat|dip[- ]coat|blade|doctor[- ]blade|spray|paint|print|"
    r"immers|soak|treat|passivat|graft|grow|self[- ]assembl|layer[- ]by[- ]layer|"
    r"deposit|electrodeposit|plat|galvanostat|potentiostat|sputter|evaporat|CVD|PVD|ALD|"
    r"stirr|mix|dissolv|dispers|sonicat|ultrasonicat|homogeniz|suspend|"
    r"dry|dried|cur|heat|bak|vacuum|"
    r"prepar|fabricat|construct|synthes" 
    r")\w*\b",
    re.IGNORECASE,
)

# [Optimized] Units: 배터리 실험 특화
UNIT_RE = re.compile(
    r"\b\d+(\.\d+)?\s*("
    r"mg|g|kg|mL|L|ml|l|µL|uL|vol%|wt%|"
    r"M|mol|mmol|mM|N|ppm|"
    r"h|min|s|sec|°C|K|"
    r"mA|A|mV|V|mAh|Ah|C[- ]rate|mS|S|Ω|Ohm|kΩ|"
    r"nm|µm|um|mm|cm|cm[- ]?2|cm\^2|"
    r"Pa|kPa|MPa|bar|Torr|psi|rpm"
    r")\b",
    re.IGNORECASE,
)

# [Optimized] Conditions: Zn Ex-situ Layer 제조 특화
CONDITION_RE = re.compile(
    r"\b("
    r"room temperature|RT|ambient temperature|"
    r"vacuum|vacuum[- ]oven|drying|dried|oven|hot[- ]?plate|infrared lamp|"
    r"argon|Ar|nitrogen|N2|inert atmosphere|glove[- ]?box|air|"
    r"stirring|magnetic stirring|sonication|ultrasonication|dispersed"
    r")\b",
    re.IGNORECASE,
)

# [Optimized] Zn Signals: 음극 기재 및 보호층 관련
ZN_SIGNAL_RE = re.compile(
    r"\b("
    r"zn foil|zn plate|zn sheet|zn disc|zn strip|zinc foil|zinc plate|"
    r"metallic zn|zn metal|bare zn|bare zinc|polished zn|"
    r"coated zn|modified zn|treated zn|protected zn|stabilized zn|"
    r"zn anode|zinc anode|composite anode|"
    r"protective layer|protective coating|surface coating|coating layer|"
    r"artificial (sei|layer|interphase)|artificial solid electrolyte interphase|"
    r"interfacial layer|interface layer|surface modification|surface treatment|"
    r"functional layer|barrier layer"
    r")\b",
    re.IGNORECASE,
)

# [Augmented] Simulation Penalty: 계산화학 키워드 대폭 강화
SIMULATION_PENALTY_RE = re.compile(
    r"\b("
    r"simulation|computational|theoretical|modeling|modelling|numerical|"
    r"calculation details|computational details|theoretical analysis|"
    r"computer[- ]aided|in[- ]silico|"
    r"DFT|density functional theory|time[- ]dependent DFT|TD[- ]?DFT|"
    r"MD|molecular dynamics|AIMD|ab[- ]initio molecular dynamics|"
    r"GCMC|grand canonical monte carlo|monte carlo|MC simulation|"
    r"first[- ]principle(s)?|ab[- ]initio|first[- ]principles calculation|"
    r"finite element|FEM|finite volume|FVM|phase[- ]field|PFM|"
    r"force[- ]field|classical dynamics|coarse[- ]grained|"
    r"NEB|nudged elastic band|climbing image|transition state search|"
    r"VASP|Vienna Ab initio|Gaussian|GAMESS|ORCA|Q-Chem|NWChem|"
    r"LAMMPS|GROMACS|AMBER|CHARMM|NAMD|DL_POLY|CP2K|"
    r"Quantum ESPRESSO|CASTEP|Siesta|Wien2k|FHI-aims|TURBOMOLE|"
    r"Materials Studio|BIOVIA|COMSOL|Multiphysics|ANSYS|Abaqus|"
    r"PBE|Perdew[- ]Burke[- ]Ernzerhof|B3LYP|HSE06|HSE|SCAN|"
    r"GGA|generalized gradient approximation|LDA|local density approximation|"
    r"meta-GGA|hybrid functional|Hubbard U|DFT\+U|U correction|"
    r"Grimme|DFT-D2|DFT-D3|DFT-D4|vdW correction|Tkatchenko-Scheffler|"
    r"basis[- ]set|6-31G|STO-3G|plane[- ]wave|pseudopotential|PAW|projector augmented wave|"
    r"ultrasoft|norm-conserving|reciprocal space|k-point|k-mesh|Monkhorst-Pack|"
    r"cutoff energy|energy cutoff|convergence criteria|self-consistent field|SCF|"
    r"density of states|DOS|PDOS|band structure|band gap calculation|"
    r"adsorption energy|binding energy|diffusion barrier|migration barrier|activation barrier|"
    r"charge density difference|electron localization function|ELF|Bader charge|"
    r"Gibbs free energy|reaction coordinate|energy profile|minimum energy path"
    r")\b",
    re.IGNORECASE,
)

# [New] Figure/Table Reference Signals (Penalty)
FIGURE_REF_RE = re.compile(
    r"\b("
    r"fig(?:\.|ure)?|figs(?:\.|ures)?|"   # Fig, Fig., Figs., Figure, Figures
    r"table|tables|"                       # Table, Tables
    r"scheme|schemes|"                     # Scheme
    r"chart|charts"                        # Chart
    r")\s*"                                # 공백 (있을 수도 없을 수도)
    r"(S?\d+[a-z]?|[IVX]+|[A-Z])\b",       # 번호 (1, S1, 2a, IV, A 등)
    re.IGNORECASE,
)

# Results/Analysis Language (Penalty)
RESULTSISH_RE = re.compile(
    r"\b(conclusion|summary|findings|analysis|performance|mechanism|regulation|evolution|behavior|"
    r"kinetics|dynamics|cycling|rate capability|capacity|reversibility|nucleation|deposition behavior|exhibited|showed)\b",
    re.IGNORECASE,
)

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

def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


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
                sentences_by_paragraph.append([ptxt])

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
# Caption split/detach from body paragraphs (flattened SI captions)
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
    if CONDITION_RE.search(t):
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
            path = normalize_text(sec.get("path", ""))

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
                                paper_id, f"section_{si}", heading or path, pi, txt,
                                f"Caption section dropped ({kind}) but method-like kept separately",
                                "methodlike_kept"
                            ))
                        else:
                            removed.append(RemovedItem(
                                paper_id, f"section_{si}", heading or path, pi, txt,
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
                            paper_id, f"section_{si}", heading or path, pi, suffix,
                            f"Mid-paragraph caption split (label={lab}) -> methodlike",
                            "methodlike_kept"
                        ))
                    else:
                        removed.append(RemovedItem(
                            paper_id, f"section_{si}", heading or path, pi, suffix,
                            f"Mid-paragraph caption split (label={lab})",
                            "removed"
                        ))
                    continue

                if looks_like_caption_start(txt):
                    if save_methodlike and is_method_like_caption(txt):
                        methodlike.append(RemovedItem(
                            paper_id, f"section_{si}", heading or path, pi, txt,
                            "Caption-like paragraph but method-like kept separately",
                            "methodlike_kept"
                        ))
                    else:
                        removed.append(RemovedItem(
                            paper_id, f"section_{si}", heading or path, pi, txt,
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
# 2. Enhanced Scoring Logic (Augmented)
# =============================================================================

def paragraph_score(p: str) -> int:
    """
    [Content Scoring Engine]
    Evaluates a single paragraph to determine if it describes an experimental recipe.
    Range: -10 to +15
    Uses STRICT/OPTIMIZED regexes for precision.
    """
    if not p:
        return 0
    
    score = 0
    # 정규식들이 이미 re.IGNORECASE 옵션을 달고 있으므로 text_lower 변환은 불필요합니다.
    # 바로 p를 사용합니다.
    
    # =========================================================
    # [1] Positive Signals: The Recipe "DNA" (Total Max: +10)
    # =========================================================
    
    # 1. Action Verbs (Most important: +3)
    # "coated", "dried", "stirred" -> 행위가 있어야 레시피임
    if METHOD_VERB_RE.search(p):
        score += 3
        
    # 2. Precision/Units (+3)
    # "5 mg", "10 h", "60 °C" -> 정량적 수치가 있어야 재현 가능함
    # (주의: UNIT_RE가 활성화되어 있어야 합니다!)
    if UNIT_RE.search(p):
        score += 0.1
        
    # 3. Conditions (+2)
    # "vacuum", "argon", "room temperature" -> 환경 설정
    if CONDITION_RE.search(p):
        score += 2
        
    # 4. Target Material (+2)
    # "Zn foil", "Protective layer" -> 엉뚱한 소재가 아닌지 확인
    if ZN_SIGNAL_RE.search(p):
        score += 2

    # =========================================================
    # [2] Negative Signals: Filtering Noise
    # =========================================================
    
    # 1. Simulation/Theory (The "Nuclear" Penalty: -10)
    # DFT, VASP 등이 나오면 실험 레시피일 확률 0%에 수렴
    if SIMULATION_PENALTY_RE.search(p):
        score -= 10
        
    # 2. Captions (Metadata: -5)
    # "Fig. 1" 로 시작하는 캡션 텍스트 제거
    if CAPTION_START_RE.match(p.strip()) or S_ONLY_START_RE.match(p.strip()):
        score -= 5
        
    # 3. Figure/Table Reference in Text (Penalty: -2) [New]
    # "As shown in Fig. 1..." -> 결과 설명일 확률 높음
    if FIGURE_REF_RE.search(p):
        score -= 3
        
    # 4. Results/Analysis Language (Soft Penalty: -2)
    # "Exhibited", "Showed", "Performance" -> 결과 자랑
    if RESULTSISH_RE.search(p):
        score -= 2

    # 5. Citation/Reference (Soft Penalty: -1) [Added]
    # "Kim et al. [15] reported..." -> 남의 연구 인용 (리뷰/서론)
    if re.search(r"(et al\.|\[\d+\]|\(ref\.?\s*\d+\))", p, re.IGNORECASE):
        score -= 2

    return score

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

    # add some anchors for context (front/back)
    try_add(0); try_add(1)
    try_add(len(paras) - 2); try_add(len(paras) - 1)

    selected.sort(key=lambda x: x[0])
    content = "\n\n".join(p for _, p in selected)
    meta["used_paras"] = len(selected)
    meta["used_chars"] = len(content)
    meta["truncated"] = (meta["used_paras"] < meta["total_paras"]) or (meta["used_chars"] < meta["total_chars"])
    return content, meta


# =============================================================================
# Prefilter (precision-first, uses heading + path)
# =============================================================================
def is_obvious_no_section(heading: str, path: str, content: str) -> Optional[str]:
    """
    [Prefilter]
    Decides if a section should be DISCARDED immediately.
    Uses BROAD regexes to avoid false negatives (accidentally dropping good sections).
    """
    h = normalize_text(heading or "")
    p = normalize_text(path or "")
    hp = f"{h} {p}".lower()
    c = normalize_text(content or "")

    # 0) Boilerplate/Meta sections (Always discard based on heading)
    if META_SECTION_RE.search(hp):
        return "Meta/boilerplate section (intro/conclusion/conflict/funding/references etc.)."

    # 1) Caption-like heading/path (Always discard)
    if looks_like_caption_start(h) or looks_like_caption_start(p):
        return "Caption-like heading/path (Figure/Table/Scheme)."

    # 2) Characterization/Measurement-only heading
    # [Optimized] Use BROAD patterns. If NO experimental signals found even broadly, discard.
    if CHAR_OR_MEASURE_RE.search(hp):
        # If content lacks even broad method verbs AND broad conditions AND units -> It's likely pure analysis text.
        if not (METHOD_VERB_RE_BROAD.search(c) or CONDITION_RE_BROAD.search(c) or UNIT_RE.search(c)):
            return "Characterization/measurement-only heading/path and no clear procedural recipe in content."

    # 3) Results-ish heading
    # [Optimized] Use BROAD patterns.
    if RESULTSISH_RE_BROAD.search(hp):
        # If content lacks even broad method verbs AND broad conditions AND units -> It's likely pure results discussion.
        if not (METHOD_VERB_RE_BROAD.search(c) or CONDITION_RE_BROAD.search(c) or UNIT_RE.search(c)):
            return "Results/analysis-oriented heading/path with no clear procedural recipe in content."

    return None

def is_obvious_yes_section(heading: str, path: str, content: str) -> Optional[str]:
    """
    [Prefilter]
    Decides if a section is DEFINITELY relevant (Auto-Pass).
    Uses STRICT regexes to ensure high precision.
    """
    h = normalize_text(heading or "")
    p = normalize_text(path or "")
    hp = f"{h} {p}".lower()
    c = normalize_text(content or "")

    # Check for strong method headings (Experimental, Preparation, etc.)
    if re.search(r"\b(materials and methods|materials & methods|methods|experimental|experimental section|"
                 r"experimental details|methodology|preparation|fabrication|synthesis)\b", hp, flags=re.IGNORECASE):
        # Must NOT be a results section disguise
        if not RESULTSISH_RE.search(hp):
            # Must have STRICT method signals in content
            if METHOD_VERB_RE.search(c) and (UNIT_RE.search(c) or CONDITION_RE.search(c)):
                return "Methods/Experimental umbrella section with clear procedural signals."

    # Check for explicit prep/fabrication in heading + strict procedural signals in content
    if METHOD_VERB_RE.search(hp) and (METHOD_VERB_RE.search(c) or UNIT_RE.search(c) or CONDITION_RE.search(c)):
        return "Preparation/fabrication cue in heading/path with procedural signals in content."

    return None

def paragraph_score(p: str) -> int:
    """
    [Content Scoring Engine]
    Evaluates a single paragraph to determine if it describes an experimental recipe.
    Range: -10 to +15
    Uses STRICT/OPTIMIZED regexes for precision.
    """
    if not p:
        return 0
    
    score = 0
    text_lower = p.lower()
    
    # [1] Positive Signals: The Recipe "DNA" (Strict)
    if METHOD_VERB_RE.search(p):
        score += 3
        
    if UNIT_RE.search(p):
        score += 0
        
    if CONDITION_RE.search(p):
        score += 2
        
    if ZN_SIGNAL_RE.search(p):
        score += 5

    # [2] Negative Signals: Filtering Noise
    
    # Simulation/Theory (The "Nuclear" Penalty: -10)
    # If it contains DFT keywords, it is almost certainly NOT a fabrication recipe.
    if SIMULATION_PENALTY_RE.search(p):
        score -= 4
        
    # Captions (Metadata: -5)
    # 문장 시작이 "Fig. 1" 등인 경우 (캡션 그 자체)
    if CAPTION_START_RE.match(p.strip()) or S_ONLY_START_RE.match(p.strip()):
        score -= 5
        
    # [New] Figure/Table Reference in Text (Penalty: -2)
    # 문장 중간에 "shown in Fig. 1", "see Table S1" 등이 나오는 경우 -> 결과 설명일 확률 높음
    if FIGURE_REF_RE.search(p):
        score -= 2
        
    # Results/Analysis Language (Soft Penalty: -2)
    # "Conclusion", "Exhibited", "Showed" 등
    if RESULTSISH_RE.search(p):
        score -= 2

    # Reference/Citation (Soft Penalty: -1)
    if re.search(r"et al\.|\[\d+\]", text_lower):
        score -= 1

    return score



# =============================================================================
# Robust JSON parse + Ollama client (HTTP/CLI)
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
# Prompt Templates (Master Set, Single Model)
# =============================================================================
# =============================================================================
# Prompt Templates (Master Set, Single Model) — Final (Precision-First)
# - 강화점:
#   (1) Stage1/Stage2 모두 "Metallic Zn anode definition"을 더 강하게 고정
#       -> "Zn-ion battery" 용어 함정(금속 Zn 없이 cathode host에 Zn2+만 넣는 경우) 차단
#   (2) Stage2에서 "electrodeposition"은 반드시 "separate cell" 또는 "before assembly" 명시 요구
#       -> ex-situ(제조공정) vs in-situ(현상) 경계 모호성 차단
# =============================================================================

SECTION_CLASSIFICATION_PROMPT = """You are an expert in mining METHODS/RECIPE evidence in aqueous zinc metal batteries (AZMB/AZIB),
with special focus on Zn METAL ANODE EX-SITU protective layers (pre-cycling coatings / artificial interphases / engineered interfaces).

You will receive:
1) Section heading/path (structure clue)
2) Section content (paragraphs; may be truncated)

Your task:
Decide if this section is LIKELY to contain PROCEDURAL experimental details (materials, steps, conditions) relevant to:
- Ex-situ Zn anode surface treatment / coating / protective layer fabrication, OR
- Cell/battery assembly / electrolyte preparation / protocol-like methods in AZMB/AZIB.

STRICT RULES (precision-first)
==============================
HARD-NO (override) ❌
Return NO if the section is mainly:
- results/discussion/analysis/performance/mechanism/evolution, OR
- characterization-only (XRD/SEM/TEM/XPS/Raman/FTIR/BET/AFM etc.) without fabrication/protocol steps, OR
- electrochemical measurement/testing-only (CV/EIS/LSV/GCD etc.) without fabrication/protocol steps, OR
- figure/table/scheme/caption-only content,
- boilerplate/meta (intro/conclusion/conflict/funding/references etc.)

Do NOT answer YES just because you see characterization or electrochemical terms.

HARD-YES (override) ✅
Return YES if the content includes any CLEAR procedure signals such as:
- preparation/fabrication/synthesis/coating/deposition/growth/dip-coating/soaking/drying/annealing steps
- units/conditions (mg, mL, mmol, °C, h, rpm, vacuum, etc.) in a procedural context
- Zn foil/anode treatment steps, coated/modified Zn specimen preparation
- electrolyte preparation or cell assembly protocols

ANTI-HALLUCINATION
==================
- Use ONLY what is explicitly supported by the provided heading/path and content.
- Section structure is NOT evidence by itself; it is only a hint.

OUTPUT (STRICT JSON ONLY)
=========================
Return ONLY this JSON:
{{"decision":"YES/NO","confidence":0.0-1.0,"reason":"brief, grounded in heading/path/content"}}

INPUT
=====
Section heading/path: "{heading_path}"

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



STAGE2_EXSITU_LAB_PROMPT = """You are a STRICT technical auditor for aqueous zinc metal battery research.

Goal
----
Based ONLY on the provided text, determine two independent criteria:

(A) **has_lab_scale_experiments**: Did the authors perform and report physical electrochemical experiments?
(B) **has_exsitu_protective_layer**: Did the authors fabricate a specific target coating on the Zn anode?

*** CRITICAL INSTRUCTION: HIERARCHY OF EVIDENCE ***
1. **SNIPPETS > TITLE:** Always prioritize the 'Evidence snippets' over the Title or Abstract.
   - If the Title says "In-situ" but the snippets describe a pre-treatment step (e.g., immersion, soaking, drying) BEFORE cell assembly, you MUST conclude it is **EX-SITU (YES)**.
2. **IGNORE ADJECTIVES:** Do not be fooled by terms like "In-situ", "Self-assembled", or "Spontaneous". Focus on the **PHYSICAL ACTIONS**:
   - Did they soak/dip the foil? -> **YES (Ex-situ)**
   - Did they dry/anneal it? -> **YES (Ex-situ)**
   - Did they use this modified foil to build a cell? -> **YES (Ex-situ)**

*** CRITICAL INSTRUCTION: INDEPENDENT EVALUATION ***
- Evaluate (A) and (B) completely separately.
- A paper can have valid experiments (A=YES) even if the technology is rejected (B=NO). Do not let one decision influence the other.

===========================================================
[Step 2-1] Criterion A — LAB-SCALE EXPERIMENTS (YES/NO)
*Judge ONLY whether physical experimental results are reported.*

[YES Criteria]
- The paper contains ANY experimental electrochemical data or implies tests were performed.
- Keywords: "cycling", "plating/stripping", "full cell", "symmetric cell", "capacity", "efficiency", "rate performance", "voltage profile", "tested", "evaluated".
- **Rule:** If they built a cell and ran it -> **YES**.

[NO Criteria]
- Pure Theory / DFT / Simulation / Modeling-only papers (No physical experiments).
- Review / Perspective / Roadmap / Discussion-only papers.

If uncertain -> NO.

===========================================================
[Step 2-2] Criterion B — EX-SITU PROTECTIVE LAYER on Zn (YES/NO)
*Judge if the technology is a **Target Ex-Situ Coating** for a **Standard LIQUID AZMB**.*

[YES Criteria - Definition] (ALL must be true)
1) **SUBSTRATE:** Metallic Zn substrate is explicitly involved (Zn foil, plate, sheet).
2) **ACTION:** A distinct protective layer/interface/artificial SEI is fabricated on the Zn surface.
3) **TIMING:** The modification is completed **BEFORE** the final battery is assembled.

*Special Cases (CRITICAL OVERRIDES):*
1. **"In-situ" Wording Trap:**
   - **IGNORE** "In-situ" in the Title if the method involves a physical pre-treatment.
   - **RULE:** If Zn foil is immersed/soaked/etched in a solution **OUTSIDE** the final cell and then rinsed/dried **BEFORE** assembly -> **MUST be YES (EX-SITU)**.
   - *Reasoning:* Trust the recipe (steps), not the name (adjective).

2. **Electrodeposition / Pre-deposition:**
   - **RULE:** Electrodeposition is a valid Ex-situ coating method **IF** performed in a **SEPARATE** setup (e.g., 3-electrode cell, plating bath, beaker) prior to final assembly.
   - **CRITICAL:** Even if the text uses the phrase **"In-situ electrodeposition"**, if it happened in a separate setup BEFORE the final cell was built, treat it as **EX-SITU (YES)**.
   - Only reject (NO) if the deposition refers strictly to the plating/stripping process *inside* the final battery during cycling.

[NO Criteria - Exclusions] (Reject if ANY of below is true)
1. **Wrong System (System Mismatch):**
   - **Zinc-Air:** Focus on Zinc-Air Batteries (ZAB) or Fuel Cells.
   - **Wearable/Gel/Solid-state:** Focus on hydrogel, polymer, or solid electrolytes.
2. **3D Host Strategy:**
   - **RULE:** Reject ONLY if the **base substrate** is NOT metallic Zn (e.g., Cu foam, Carbon cloth, Carbon felt, 3D printing mesh) into which Zn is deposited.
   - **EXCEPTION (Accept as YES):** If the starting material is **Zn Foil/Plate** and a carbon/graphene/host-like material is coated **ON TOP** of it as a layer, treat this as **ZN_EX_SITU_LAYER (YES)**. Do not classify "Zn foil + Carbon coating" as a 3D host.
3. **Separator/Interlayer:**
   - Exclude freestanding membranes or functional separators physically inserted between electrodes.
   - Reject if the layer is not directly bonded/coated onto the Zn surface as an integrated part of the electrode.
4. **Electrolyte/Additive Only:(REAL Insitu SEI layer)**
   - Exclude protective layers derived solely from electrolyte components (salts, additives) that form dynamically *after* cell assembly.
   - Reject if the Zn anode is inserted as pristine/bare metal.
5. **Hybrid Strategy:**
   - Exclude research that explicitly relies on combining an ex-situ layer WITH a functional electrolyte additive for synergistic effects ("dual-protection").
   - The ex-situ layer must be tested in a standard, additive-free electrolyte to qualify.

If ambiguous between Coating on Foil vs Host Strategy -> **NO**.

===========================================================
[Part C] modification_focus (choose ONE)
- ZN_EX_SITU_LAYER (Target)
- ZN_IN_SITU_SEI
- ZN_3D_HOST
- SEPARATOR_INTERLAYER
- ELECTROLYTE_ONLY (includes Gel/Solid)
- HYBRID_STRATEGY
- OTHER (includes Zinc-Air, Review, Theory)

===========================================================
OUTPUT (STRICT JSON ONLY)
Return ONLY this JSON object:
{{
  "has_exsitu_protective_layer": "YES" or "NO",
  "has_lab_scale_experiments": "YES" or "NO",
  "modification_focus": "ZN_EX_SITU_LAYER/ZN_IN_SITU_SEI/ZN_3D_HOST/SEPARATOR_INTERLAYER/ELECTROLYTE_ONLY/HYBRID_STRATEGY/OTHER",
  "confidence": 0.0-1.0,
  "reason": "One brief sentence. If NO for Criterion B, state exactly which exclusion rule was triggered."
}}

INPUT
-----
Title:
{title}

Abstract:
{abstract}

Body structure clues (HINT ONLY):
{body_structure}

Supplementary structure clues (HINT ONLY):
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

def ensure_focus_stage2(v: Any) -> str:
    allowed = {
        "ZN_EX_SITU_LAYER",
        "ZN_3D_HOST",
        "ZN_IN_SITU_SEI",
        "ELECTROLYTE",
        "SEPARATOR_INTERLAYER",
        "OTHER",
    }
    s = str(v or "").strip()
    return s if s in allowed else "OTHER"


# =============================================================================
# XML metadata (Elsevier META_ABS) - Stage1 gate input
# =============================================================================
def parse_xml_metadata(xml_dir: Optional[Path], pii: str) -> Tuple[str, str, str]:
    """
    Returns (title, abstract, xml_path_str).
    We keep this robust because Elsevier META_ABS variants exist.
    """
    if not xml_dir:
        return "", "", ""
    xml_path = xml_dir / f"{pii}__META_ABS.xml"
    if not xml_path.exists():
        return "", "", str(xml_path)

    title = ""
    abstract = ""
    try:
        tree = etree.parse(str(xml_path))
        root = tree.getroot()

        # Common namespaces
        ns = {
            "dc": "http://purl.org/dc/elements/1.1/",
            "ce": "http://www.elsevier.com/xml/common/dtd",
            "ja": "http://www.elsevier.com/xml/ja/dtd",
        }

        # Title candidates
        tnode = root.find(".//dc:title", ns)
        if tnode is None:
            tnode = root.find(".//{http://purl.org/dc/elements/1.1/}title")
        if tnode is not None and tnode.text:
            title = normalize_text(tnode.text)

        # Abstract candidates 1: dc:description
        dnode = root.find(".//dc:description", ns)
        if dnode is not None and dnode.text:
            abstract = normalize_text(dnode.text)

        # Abstract candidates 2: Elsevier ce:abstract/ce:para
        if not abstract:
            paras = root.findall(".//ce:abstract//ce:para", ns)
            if paras:
                abstract = normalize_text(" ".join([p.text or "" for p in paras if (p.text or "").strip()]))

        # Abstract candidates 3: any <abstract> text
        if not abstract:
            abs_nodes = root.findall(".//abstract")
            if abs_nodes:
                abstract = normalize_text(" ".join([" ".join(n.itertext()) for n in abs_nodes]))

    except Exception as e:
        logging.warning(f"[XML parse] {pii} | {type(e).__name__}: {str(e)[:120]}")

    return title, abstract, str(xml_path)


# =============================================================================
# Supplementary conversion (DOC/DOCX > PDF) optional
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
# File discovery (Stage1=YES 이후에만 수행)
# =============================================================================
def find_main_pdf_by_pii(pdf_dir: Path, pii: str) -> Optional[Path]:
    if not pdf_dir.exists():
        return None
    # prioritize direct filename match in top-level
    candidates = []
    for p in pdf_dir.glob("*.pdf"):
        if extract_pii(p.name) == pii:
            candidates.append(p)
    if candidates:
        return max(candidates, key=lambda x: x.stat().st_mtime)

    # fallback recursive
    candidates = []
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
    out.sort(key=lambda x: (x.suffix.lower(), x.name.lower()))
    return out


# =============================================================================
# Structure serialization for Stage2
# =============================================================================
def headings_structure(sections: List[Dict[str, Any]], max_items: int = 250) -> str:
    lines: List[str] = []
    for sec in sections[:max_items]:
        h = normalize_text(sec.get("heading", ""))
        p = normalize_text(sec.get("path", ""))
        lvl = sec.get("level", 0)
        label = p if p else h
        if not label:
            continue
        lines.append(f"- L{lvl}: {label}")
    if len(sections) > max_items:
        lines.append(f"... ({len(sections) - max_items} more)")
    return "\n".join(lines) if lines else "(none)"


# =============================================================================
# GROBID runner + TEI -> doc
# =============================================================================
def grobid_process_pdf(
    client: GrobidClient,
    pdf_path: Path,
    out_tei_path: Path,
    segment_sentences: bool = True,
    tei_coordinates: Optional[str] = None,
) -> str:
    tei = client.process_fulltext(pdf_path, segment_sentences=segment_sentences,
                                 generate_ids=True, tei_coordinates=tei_coordinates)
    out_tei_path.write_text(tei, encoding="utf-8")
    return tei

def extract_doc_from_tei(tei_xml: str, source_file: str) -> Dict[str, Any]:
    root = parse_tei_xml(tei_xml)
    title = extract_title(root)
    abstract_paras = extract_abstract_paragraphs(root)
    sections = extract_body_sections(root)
    figtab = extract_figure_table_blocks(root)

    return {
        "source_file": source_file,
        "title": title,
        "abstract_paragraphs": abstract_paras,
        "sections": sections,
        "caption_blocks": figtab,
    }


# =============================================================================
# Cache keys (deterministic)
# =============================================================================
def cache_key_stage1(title: str, abstract: str) -> str:
    return sha1_text(f"{PIPELINE_VERSION}|{PROMPT_VERSION}|STAGE1|{title}\n{abstract}".strip())

def cache_key_section(heading_path: str, content: str) -> str:
    return sha1_text(f"{PIPELINE_VERSION}|{PROMPT_VERSION}|SECTION|{heading_path}\n\n{content}".strip())

def cache_key_stage2(title: str, abstract: str, body_structure: str, supp_structure: str, evidence: str) -> str:
    return sha1_text(f"{PIPELINE_VERSION}|{PROMPT_VERSION}|STAGE2|{title}\n{abstract}\n{body_structure}\n{supp_structure}\n{evidence}".strip())


# =============================================================================
# Stage1 / Stage2 classifiers (single model)
# =============================================================================
def classify_stage1_azmb(
    title: str,
    abstract: str,
    args: argparse.Namespace,
    cache: Dict[str, Dict[str, Any]],
    cache_path: Path,
    paper_dir: Path,
) -> Dict[str, Any]:
    title = normalize_text(title)
    abstract = normalize_text(abstract)

    if not title and not abstract:
        return {"is_aqueous_zmb": "NO", "confidence": 0.0, "reason": "Missing Title+Abstract (XML).", "cache_hit": False}

    prompt = STAGE1_AZMB_PROMPT.format(title=title, abstract=abstract)
    ck = cache_key_stage1(title, abstract)

    if args.use_cache and ck in cache:
        c = cache[ck]
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
        temperature=args.stage1_temperature,
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
            "cache_key": ck,
            "kind": "STAGE1",
            **out,
            "model": args.llm_model,
            "pipeline_version": PIPELINE_VERSION,
            "prompt_version": PROMPT_VERSION,
            "created_at": now_ts(),
        }
        append_cache(cache_path, entry)
        cache[ck] = entry

    out["cache_hit"] = False
    return out

def classify_stage2_exsitu_lab(
    title: str,
    abstract: str,
    body_structure: str,
    supp_structure: str,
    evidence_snippets: str,
    args: argparse.Namespace,
    cache: Dict[str, Dict[str, Any]],
    cache_path: Path,
    paper_dir: Path,
) -> Dict[str, Any]:
    title = normalize_text(title)
    abstract = normalize_text(abstract)

    prompt = STAGE2_EXSITU_LAB_PROMPT.format(
        title=title,
        abstract=abstract,
        body_structure=body_structure,
        supp_structure=supp_structure,
        evidence_snippets=evidence_snippets,
    )
    ck = cache_key_stage2(title, abstract, body_structure, supp_structure, evidence_snippets)

    if args.use_cache and ck in cache:
        c = cache[ck]
        return {
            "has_exsitu_protective_layer": ensure_yesno(c.get("has_exsitu_protective_layer")),
            "has_lab_scale_experiments": ensure_yesno(c.get("has_lab_scale_experiments")),
            "modification_focus": ensure_focus_stage2(c.get("modification_focus")),
            "confidence": clamp01(c.get("confidence", 0.5), 0.5),
            "reason": str(c.get("reason", "")),
            "cache_hit": True,
        }

    if args.save_prompts:
        (paper_dir / "stage2_prompt.txt").write_text(prompt, encoding="utf-8")

    raw, parsed, err = call_llm_with_retries(
        mode=args.ollama_mode,
        ollama_url=args.ollama_url,
        model=args.llm_model,
        prompt=prompt,
        timeout=args.timeout,
        temperature=args.stage2_temperature,
        top_p=args.top_p,
        max_retries=args.retries,
        backoff_sec=args.backoff,
    )

    if args.save_prompts:
        (paper_dir / "stage2_output_raw.txt").write_text(raw or "", encoding="utf-8")

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
            "modification_focus": ensure_focus_stage2(parsed.get("modification_focus")),
            "confidence": clamp01(parsed.get("confidence", 0.5), 0.5),
            "reason": str(parsed.get("reason", "")).strip(),
        }

    if args.use_cache:
        entry = {
            "cache_key": ck,
            "kind": "STAGE2",
            **out,
            "model": args.llm_model,
            "pipeline_version": PIPELINE_VERSION,
            "prompt_version": PROMPT_VERSION,
            "created_at": now_ts(),
        }
        append_cache(cache_path, entry)
        cache[ck] = entry

    out["cache_hit"] = False
    return out


# =============================================================================
# Section-level classification (with prefilter + cache + early stop)
# =============================================================================
def classify_section_llm(
    heading: str,
    path: str,
    paragraphs: List[str],
    args: argparse.Namespace,
    cache: Dict[str, Dict[str, Any]],
    cache_path: Path,
    paper_dir: Path,
    paper_id: str,
    section_key: str,
) -> Dict[str, Any]:
    content, meta = build_section_content(paragraphs, args.max_chars, args.max_paras)
    heading = normalize_text(heading)
    path = normalize_text(path)
    heading_path = (path or heading) if (path or heading) else "(no heading)"

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
        r_no = is_obvious_no_section(heading, path, content)
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
        r_yes = is_obvious_yes_section(heading, path, content)
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

    prompt = SECTION_CLASSIFICATION_PROMPT.format(heading_path=heading_path, content=content)
    ck = cache_key_section(heading_path, content)

    if args.use_cache and ck in cache:
        c = cache[ck]
        return {
            "decision": c.get("decision", "NO"),
            "confidence": clamp01(c.get("confidence", 0.5), 0.5),
            "reason": str(c.get("reason", "")),
            "used_paras": meta["used_paras"],
            "total_paras": meta["total_paras"],
            "truncated": meta["truncated"],
            "prefiltered": False,
            "cache_hit": True,
            "content_excerpt": (content[:600] + " ...") if len(content) > 700 else content,
        }

    if args.save_prompts and args.save_section_prompts:
        sec_dir = paper_dir / "section_prompts"
        safe_mkdir(sec_dir)
        (sec_dir / f"{section_key}_heading_path.txt").write_text(heading_path, encoding="utf-8")
        (sec_dir / f"{section_key}_content.txt").write_text(content, encoding="utf-8")
        (sec_dir / f"{section_key}_prompt.txt").write_text(prompt, encoding="utf-8")

    raw, parsed, err = call_llm_with_retries(
        mode=args.ollama_mode,
        ollama_url=args.ollama_url,
        model=args.llm_model,
        prompt=prompt,
        timeout=args.timeout,
        temperature=args.section_temperature,
        top_p=args.top_p,
        max_retries=args.retries,
        backoff_sec=args.backoff,
    )

    if args.save_prompts and args.save_section_prompts:
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
            "cache_key": ck,
            "kind": "SECTION",
            "paper_id": paper_id,
            "section_key": section_key,
            "decision": decision if decision in {"YES", "NO"} else "NO",
            "confidence": conf,
            "reason": reason,
            "model": args.llm_model,
            "pipeline_version": PIPELINE_VERSION,
            "prompt_version": PROMPT_VERSION,
            "created_at": now_ts(),
        }
        append_cache(cache_path, entry)
        cache[ck] = entry

    return {
        "decision": decision if decision in {"YES", "NO"} else "NO",
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
# Evidence aggregation + quality scoring + early stop
# =============================================================================
def evidence_quality_score(excerpt: str) -> float:
    """
    Conservative scoring: require explicit procedural cues.
    """
    e = normalize_text(excerpt)
    score = 0.0
    if METHOD_VERB_RE.search(e):
        score += 0.4
    if  CONDITION_RE.search(e):
        score += 0.3
    if ZN_SIGNAL_RE.search(e):
        score += 0.2
    if looks_like_caption_start(e):
        score -= 0.5
    if RESULTSISH_RE.search(e) and not (METHOD_VERB_RE.search(e) and CONDITION_RE.search(e)):
        score -= 0.3
    return score

def make_evidence_snippets(
    section_rows: List[Dict[str, Any]],
    methodlike_items: List[RemovedItem],
    max_snippets: int,
    max_chars_total: int,
) -> str:
    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for r in section_rows:
        if r.get("decision") != "YES":
            continue
        excerpt = normalize_text(r.get("content_excerpt", ""))
        if not excerpt:
            continue
        base = float(r.get("confidence", 0.0))
        boost = evidence_quality_score(excerpt)
        score = base + boost
        candidates.append((score, r))

    candidates.sort(key=lambda x: x[0], reverse=True)

    lines: List[str] = []
    used = 0
    n = 0

    for _, r in candidates:
        if n >= max_snippets:
            break
        hp = normalize_text(r.get("path", "")) or normalize_text(r.get("heading", ""))
        excerpt = normalize_text(r.get("content_excerpt", ""))
        block = f"[SECTION] {hp}\n{excerpt}\n"
        if used + len(block) > max_chars_total:
            continue
        lines.append(block)
        used += len(block)
        n += 1

    # add method-like captions as supplementary evidence (explicitly labeled)
    for it in methodlike_items:
        if n >= max_snippets:
            break
        txt = normalize_text(it.text)
        if not txt:
            continue
        block = f"[METHODLIKE_CAPTION] {normalize_text(it.heading) or it.source}\n{txt}\n"
        if used + len(block) > max_chars_total:
            break
        lines.append(block)
        used += len(block)
        n += 1

    return "\n".join(lines).strip() if lines else "(no explicit procedural snippets found)"

# =============================================================================
# Section prioritization for mining
# =============================================================================
def section_priority_score(heading: str, path: str, paragraphs: List[str]) -> float:
    """
    [Commercial-Grade Heuristic]
    Calculates mining priority based on Heading (Structure) + Content (Text Signals).
    """
    h = normalize_text(heading)
    p = normalize_text(path)
    hp = f"{h} {p}".lower()

    score = 0.0

    # =========================================================================
    # [STEP 1] Heading Analysis (The Hint)
    # =========================================================================
    
    # [방어 로직] 제목에 '결과/분석' 키워드가 섞여 있으면 Tier 1 자격 박탈
    is_pure_prep = not re.search(r"\b(characteri[sz]ation|measurement|performance|result|discussion|analys|propert|mechanism)\b", hp)

    # [Tier 1] Explicit Target: "Zn Anode Preparation" (+18.0)
    # 조건: Zn + Prep 키워드 존재 AND 결과/분석 키워드 없음(Pure)
    if (re.search(r"\b(zn|zinc|anode|electrode)\b", hp) and 
        re.search(r"\b(prepar|synthes|fabricat|construct|assembl|coat|deposit|modif)\w*\b", hp) and
        is_pure_prep):  # <--- [핵심 수정] 섞인 제목은 Tier 1 진입 금지
        
        score += 18.0
    
    # [Tier 2] General Method: "Experimental", "Methods" (+4.0)
    elif re.search(r"\b(materials and methods|experimental|methodology)\b", hp):
        score += 4.0
    
    # [Tier 3] Generic Process: "Synthesis", "Assembly" (+2.0)
    # Tier 1에서 탈락한 "Preparation of ... Analysis"도 여기로 내려와서 +2.0만 받게 됨 (합리적)
    elif re.search(r"\b(preparation|fabrication|synthesis|assembly|procedure)\b", hp):
        score += 2.0

    # [Tier 4] Supplementary: "Note S1", "Supplementary Methods" (+2.0)
    if re.search(r"\b(supporting|supplementary|additional) (methods|experimental)\b", hp):
        score += 2.0

    # -------------------------------------------------------------------------
    # Penalties (Filtering out Noise)
    # -------------------------------------------------------------------------
    # Cathode/Electrolyte focus -> Downgrade (-2.0)
    if re.search(r"\b(cathode|positive|electrolyte|separator)\s+(prepar|synthes|fabricat)", hp):
        score -= 4.0

    # Results/Characterization focus -> Downgrade (-1.5)
    if re.search(r"\b(characteri[sz]ation|measurement|performance|result|discussion|analys|propert)", hp):
        # Mercy rule: "Preparation and Characterization" is okay
        # 위에서 Tier 1은 막았지만, Tier 3(+2.0) - Penalty(-1.5) = +0.5 점으로 
        # 아예 버려지지는 않게 살려둠 (내용 점수로 부활 가능)
        if not re.search(r"\b(prepar|synthes|fabricat)\w*\b", hp):
            score -= 1.5

    # Meta sections (Intro/Ref) -> Bury (-10.0)
    if META_SECTION_RE.search(hp):
        score -= 10.0
    
    # Caption-like Headings -> Bury (-5.0)
    if looks_like_caption_start(h) or looks_like_caption_start(p):
        score -= 5.0

    # =========================================================================
    # [STEP 2] Content-Based Boosting (The Reality Check)
    # =========================================================================
    # Even if the heading is vague (e.g., "2.2."), if the text screams "Recipe",
    # we must boost it.
    
    valid_paras = [normalize_text(x) for x in paragraphs[:5] if isinstance(x, str) and x.strip()]
    
    if not valid_paras:
        score -= 15.0 # Empty section penalty
    else:
        content_score_sum = 0.0
        for txt in valid_paras:
            # Call the granular text scoring engine
            p_score = paragraph_score(txt)
            content_score_sum += p_score
        
        # Scale logic:
        # 5 paragraphs * avg 4 points (very high) * 0.2 scaling = +4.0 boost
        # This allows a "No Heading" section (0.0) to jump to (4.0), 
        # beating a "General Method" (3.0) that has empty text.
        boost = content_score_sum * 0.2
        
        # Safety Cap: Don't let one super-long paragraph distort everything too much
        # But allow enough to save valid sections.
        score += max(-3.0, min(6.0, boost))

    return score


def run_for_pii(
    pii: str,
    args: argparse.Namespace,
    grobid_client: GrobidClient,
    cache: Dict[str, Dict[str, Any]],
    cache_path: Path,
) -> Dict[str, Any]:
    paper_dir = Path(args.out_dir) / pii
    safe_mkdir(paper_dir)

    # -------------------------------------------------------------------------
    # Step 0) Load Title/Abstract from XML (META_ABS)
    # -------------------------------------------------------------------------
    xml_title, xml_abs, xml_path = parse_xml_metadata(Path(args.xml_dir) if args.xml_dir else None, pii)
    if args.save_prompts:
        (paper_dir / "meta_xml_path.txt").write_text(xml_path, encoding="utf-8")
        (paper_dir / "meta_title.txt").write_text(xml_title or "", encoding="utf-8")
        (paper_dir / "meta_abstract.txt").write_text(xml_abs or "", encoding="utf-8")

    # -------------------------------------------------------------------------
    # Step 1) Stage1 Gate (Title+Abstract only) -> if NO, skip everything heavy
    # -------------------------------------------------------------------------
    s1 = classify_stage1_azmb(
        title=xml_title,
        abstract=xml_abs,
        args=args,
        cache=cache,
        cache_path=cache_path,
        paper_dir=paper_dir,
    )
    (paper_dir / "stage1_result.json").write_text(json.dumps(s1, ensure_ascii=False, indent=2), encoding="utf-8")

    if s1.get("is_aqueous_zmb") != "YES":
        return {
            "paper_id": pii,
            "status": "DONE_STAGE1_NO",
            "xml_title": normalize_text(xml_title),
            "xml_abstract": normalize_text(xml_abs)[:4000],
            "S1_is_aqueous_zmb": s1.get("is_aqueous_zmb", "NO"),
            "S1_confidence": s1.get("confidence", 0.0),
            "S1_reason": s1.get("reason", ""),
            "S2_has_exsitu_protective_layer": "NA",
            "S2_has_lab_scale_experiments": "NA",
            "S2_modification_focus": "NA",
            "S2_confidence": 0.0,
            "S2_reason": "Skipped (Stage1 NO)",
            "candidate_exsitu_lab": "NO",
            "main_pdf": "",
            "supp_files": [],
            "num_sections_total": 0,
            "num_sections_processed": 0,
            "num_sections_yes": 0,
            "high_quality_yes": 0,
            "early_stopped": False,
        }

    # -------------------------------------------------------------------------
    # Step 2) Stage1=YES -> now discover PDFs
    # -------------------------------------------------------------------------
    main_pdf = find_main_pdf_by_pii(Path(args.pdf_dir), pii)
    supp_files = find_supp_files_by_pii(Path(args.supp_dir), pii, exts={".pdf", ".doc", ".docx"}) if args.supp_dir else []

    if not main_pdf:
        return {
            "paper_id": pii,
            "status": "MISSING_MAIN_PDF",
            "xml_title": normalize_text(xml_title),
            "xml_abstract": normalize_text(xml_abs)[:4000],
            "S1_is_aqueous_zmb": "YES",
            "S1_confidence": s1.get("confidence", 0.0),
            "S1_reason": s1.get("reason", ""),
            "S2_has_exsitu_protective_layer": "NA",
            "S2_has_lab_scale_experiments": "NA",
            "S2_modification_focus": "NA",
            "S2_confidence": 0.0,
            "S2_reason": "Skipped (missing main PDF)",
            "candidate_exsitu_lab": "NO",
            "main_pdf": "",
            "supp_files": [str(x) for x in supp_files],
            "num_sections_total": 0,
            "num_sections_processed": 0,
            "num_sections_yes": 0,
            "high_quality_yes": 0,
            "early_stopped": False,
        }

    # -------------------------------------------------------------------------
    # Step 3) GROBID (lazy check alive)
    # -------------------------------------------------------------------------
    if not grobid_client.is_alive():
        raise RuntimeError(f"GROBID not reachable at {grobid_client.base_url}. Start server first.")

    tei_dir = paper_dir / "tei"
    safe_mkdir(tei_dir)

    main_tei_path = tei_dir / "main.tei.xml"
    if args.force_grobid or not main_tei_path.exists():
        logging.info(f"[{pii}] GROBID main: {main_pdf}")
        tei_main = grobid_process_pdf(
            grobid_client, main_pdf, main_tei_path,
            segment_sentences=True,
            tei_coordinates=args.tei_coordinates or None
        )
    else:
        tei_main = main_tei_path.read_text(encoding="utf-8", errors="ignore")

    main_doc_raw = extract_doc_from_tei(tei_main, source_file=str(main_pdf))

    supp_docs_raw: List[Dict[str, Any]] = []
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
        if args.force_grobid or not tei_path.exists():
            logging.info(f"[{pii}] GROBID supp: {pdf_to_process}")
            tei_s = grobid_process_pdf(
                grobid_client, pdf_to_process, tei_path,
                segment_sentences=True,
                tei_coordinates=args.tei_coordinates or None
            )
        else:
            tei_s = tei_path.read_text(encoding="utf-8", errors="ignore")

        d = extract_doc_from_tei(tei_s, source_file=str(sf))
        d["processed_pdf"] = str(pdf_to_process)
        d["original_file_type"] = ext
        d["was_converted_from_word"] = was_converted
        supp_docs_raw.append(d)

    extracted_dir = paper_dir / "extracted"
    safe_mkdir(extracted_dir)
    (extracted_dir / "main_extracted_raw.json").write_text(json.dumps(main_doc_raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (extracted_dir / "supp_extracted_raw.json").write_text(json.dumps(supp_docs_raw, ensure_ascii=False, indent=2), encoding="utf-8")

    # -------------------------------------------------------------------------
    # Step 4) Cleaning captions (main + merged supp sections)
    # -------------------------------------------------------------------------
    main_clean, main_removed, main_methodlike = clean_captions_in_doc(
        main_doc_raw,
        paper_id=pii,
        save_methodlike=args.save_methodlike,
        drop_caption_blocks_in_kept=(not args.keep_caption_blocks),
    )

    supp_all_sections: List[Dict[str, Any]] = []
    supp_removed_all: List[RemovedItem] = []
    supp_methodlike_all: List[RemovedItem] = []

    for sd in supp_docs_raw:
        sd_clean, sd_removed, sd_methodlike = clean_captions_in_doc(
            sd,
            paper_id=pii,
            save_methodlike=args.save_methodlike,
            drop_caption_blocks_in_kept=(not args.keep_caption_blocks),
        )
        supp_removed_all.extend(sd_removed)
        supp_methodlike_all.extend(sd_methodlike)
        secs = sd_clean.get("sections", [])
        if isinstance(secs, list):
            for s in secs:
                if isinstance(s, dict):
                    s["supp_source_file"] = sd_clean.get("source_file", "")
            supp_all_sections.extend([s for s in secs if isinstance(s, dict)])

    cleaned_dir = paper_dir / "cleaned"
    safe_mkdir(cleaned_dir)
    (cleaned_dir / "main_cleaned.json").write_text(json.dumps(main_clean, ensure_ascii=False, indent=2), encoding="utf-8")
    (cleaned_dir / "supp_sections_merged_cleaned.json").write_text(json.dumps({"paper_id": pii, "sections": supp_all_sections}, ensure_ascii=False, indent=2), encoding="utf-8")

    removed_dir = paper_dir / "removed"
    safe_mkdir(removed_dir)
    (removed_dir / "main_removed.jsonl").write_text("\n".join(json.dumps(x.__dict__, ensure_ascii=False) for x in main_removed), encoding="utf-8")
    (removed_dir / "supp_removed.jsonl").write_text("\n".join(json.dumps(x.__dict__, ensure_ascii=False) for x in supp_removed_all), encoding="utf-8")
    if args.save_methodlike:
        (removed_dir / "main_methodlike.jsonl").write_text("\n".join(json.dumps(x.__dict__, ensure_ascii=False) for x in main_methodlike), encoding="utf-8")
        (removed_dir / "supp_methodlike.jsonl").write_text("\n".join(json.dumps(x.__dict__, ensure_ascii=False) for x in supp_methodlike_all), encoding="utf-8")

    # -------------------------------------------------------------------------
    # Step 5) Section-level mining [OPTIMIZED: Prefilter -> Sort -> Slice -> LLM]
    # -------------------------------------------------------------------------
    main_secs = main_clean.get("sections", [])
    if not isinstance(main_secs, list):
        main_secs = []

    # 1. Collect all sections into raw_pool
    raw_pool: List[Dict[str, Any]] = []
    for i, sec in enumerate(main_secs):
        if isinstance(sec, dict):
            raw_pool.append({"origin": "main", "idx": i, "sec": sec})

    for i, sec in enumerate(supp_all_sections):
        if isinstance(sec, dict):
            raw_pool.append({"origin": "supp", "idx": i, "sec": sec})

    # 2. Prefilter (Hard-NO) FIRST
    filtered_pool: List[Dict[str, Any]] = []
    if args.no_prefilter:
        filtered_pool = raw_pool
    else:
        for item in raw_pool:
            sec = item["sec"]
            h = normalize_text(sec.get("heading", ""))
            p = normalize_text(sec.get("path", ""))
            
            # Sampling for prefilter check (avoid huge string concat)
            paras = sec.get("paragraphs", [])
            if not isinstance(paras, list): paras = []
            content_sample = "\n".join([normalize_text(x) for x in paras[:5] if isinstance(x, str)])

            r_no = is_obvious_no_section(h, p, content_sample)
            if not r_no:
                filtered_pool.append(item)
            # Else: dropped silently (could log if needed)

    # 3. Sort remaining candidates by priority score
    filtered_pool.sort(
        key=lambda x: section_priority_score(
            x["sec"].get("heading", ""),
            x["sec"].get("path", ""),
            x["sec"].get("paragraphs", []) if isinstance(x["sec"].get("paragraphs", []), list) else [],
        ),
        reverse=True
    )

    # 4. Slice top N candidates
    # If args.max_sections_to_process is 0, keep all filtered items.
    max_sections = args.max_sections_to_process if args.max_sections_to_process > 0 else len(filtered_pool)
    mining_pool = filtered_pool[:max_sections]

    # =========================================================================
    # [Debugging] Save the mining pool candidates (Two Files: Selected & All)
    # =========================================================================
    if args.save_prompts:
        base_debug_data = {
            "paper_id": pii,
            "settings": {
                "max_sections_to_process": args.max_sections_to_process,
                "no_prefilter": args.no_prefilter
            },
            "counts": {
                "total_raw": len(raw_pool),
                "after_prefilter": len(filtered_pool),
                "final_mining_pool": len(mining_pool)
            }
        }

        # (A) Save ALL candidates with 'selected' flag
        all_debug_data = base_debug_data.copy()
        all_debug_data["sections"] = []
        
        for rank, item in enumerate(filtered_pool):
            sec = item["sec"]
            paras = sec.get("paragraphs", [])
            if not isinstance(paras, list): paras = []
            
            # Reconstruct content used score calculation (lightweight)
            content_full, _ = build_section_content(paras, args.max_chars, args.max_paras)
            
            score = section_priority_score(
                sec.get("heading", ""), 
                sec.get("path", ""), 
                paras
            )
            
            is_selected = rank < len(mining_pool)

            all_debug_data["sections"].append({
                "rank": rank + 1,
                "selected": is_selected,  # Flag for visual check
                "origin": item["origin"],
                "heading": normalize_text(sec.get("heading", "")),
                "path": normalize_text(sec.get("path", "")),
                "priority_score": score,
                "content_preview": content_full[:500] + "..." # truncated
            })
        
        (paper_dir / "debug_mining_pool.all.json").write_text(
            json.dumps(all_debug_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # (B) Save ONLY selected mining pool (Classic behavior)
        selected_debug_data = base_debug_data.copy()
        selected_debug_data["sections"] = [
            s for s in all_debug_data["sections"] if s["selected"]
        ]
        
        (paper_dir / "debug_mining_pool.json").write_text(
            json.dumps(selected_debug_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    # =========================================================================

    section_rows: List[Dict[str, Any]] = []
    early_stopped = False
    high_quality_yes = 0
    processed_sections = 0

    # 5. LLM Loop
    for item in mining_pool:
        sec = item["sec"]
        origin = item["origin"]

        heading = normalize_text(sec.get("heading", ""))
        path = normalize_text(sec.get("path", ""))
        paras = sec.get("paragraphs", [])
        if not isinstance(paras, list):
            paras = []

        section_key = f"{origin}_{item['idx']:04d}"
        
        # LLM Classification
        res = classify_section_llm(
            heading=heading,
            path=path,
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
            "source_kind": origin,
            "source_file": str(main_pdf) if origin == "main" else str(sec.get("supp_source_file", "")),
            "section_key": section_key,
            "path": path,
            "heading": heading,
            **res,
        }
        section_rows.append(row)
        processed_sections += 1

        # Early stop check
        if row.get("decision") == "YES":
            excerpt = normalize_text(row.get("content_excerpt", ""))
            q = evidence_quality_score(excerpt)
            if (row.get("confidence", 0.0) >= args.early_stop_min_conf) and (q >= args.early_stop_min_quality):
                high_quality_yes += 1

        if args.early_stop and high_quality_yes >= args.early_stop_yes_n:
            early_stopped = True
            break

    (paper_dir / "sections_classification.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in section_rows),
        encoding="utf-8"
    )

    # -------------------------------------------------------------------------
    # Step 6) Stage2 final judge (title+abstract + body+supp structure + evidence)
    # -------------------------------------------------------------------------
    title = normalize_text(xml_title) or normalize_text(main_clean.get("title", ""))
    abstract = normalize_text(xml_abs)
    if not abstract:
        # fallback: grobid abstract if xml missing unexpectedly
        abstract = "\n".join([normalize_text(x) for x in (main_clean.get("abstract_paragraphs") or []) if normalize_text(x)]).strip()

    if args.save_prompts:
        (paper_dir / "doc_title.txt").write_text(title or "", encoding="utf-8")
        (paper_dir / "doc_abstract.txt").write_text(abstract or "", encoding="utf-8")

    body_struct = headings_structure(main_secs, max_items=args.max_structure_items)
    supp_struct = headings_structure(supp_all_sections, max_items=args.max_structure_items)

    evidence = make_evidence_snippets(
        section_rows=section_rows,
        methodlike_items=(main_methodlike + supp_methodlike_all) if args.save_methodlike else [],
        max_snippets=args.max_evidence_snippets,
        max_chars_total=args.max_evidence_chars,
    )
    (paper_dir / "stage2_evidence_snippets.txt").write_text(evidence, encoding="utf-8")

    s2 = classify_stage2_exsitu_lab(
        title=title,
        abstract=abstract,
        body_structure=body_struct,
        supp_structure=supp_struct,
        evidence_snippets=evidence,
        args=args,
        cache=cache,
        cache_path=cache_path,
        paper_dir=paper_dir,
    )
    (paper_dir / "stage2_result.json").write_text(json.dumps(s2, ensure_ascii=False, indent=2), encoding="utf-8")

    candidate = "YES" if (s1["is_aqueous_zmb"] == "YES"
                          and s2["has_exsitu_protective_layer"] == "YES"
                          and s2["has_lab_scale_experiments"] == "YES") else "NO"

    return {
        "paper_id": pii,
        "status": "DONE",
        "xml_title": title,
        "xml_abstract": (abstract or "")[:4000],
        "S1_is_aqueous_zmb": "YES",
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
        "num_sections_total": len(mining_pool),
        "num_sections_processed": processed_sections,
        "num_sections_yes": sum(1 for r in section_rows if r.get("decision") == "YES"),
        "high_quality_yes": high_quality_yes,
        "early_stopped": early_stopped,
    }
# =============================================================================
# I/O helpers
# =============================================================================
def read_pii_list(path_or_inline: str) -> List[str]:
    p = Path(path_or_inline)
    if p.exists():
        items = []
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            pii = extract_pii(line) or line
            if pii and PII_PATTERN.fullmatch(pii):
                items.append(pii)
        return sorted(set(items))
    toks = [x.strip() for x in re.split(r"[,\s]+", path_or_inline.strip()) if x.strip()]
    items = []
    for t in toks:
        pii = extract_pii(t) or t
        if pii and PII_PATTERN.fullmatch(pii):
            items.append(pii)
    return sorted(set(items))

def write_summary(out_dir: Path, rows: List[Dict[str, Any]]) -> Tuple[Path, Path]:
    safe_mkdir(out_dir)
    ts = now_ts()
    jsonl_path = out_dir / f"summary_{ts}.jsonl"
    csv_path = out_dir / f"summary_{ts}.csv"

    jsonl_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    if pd is not None:
        pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
    else:
        fieldnames = sorted({k for r in rows for k in r.keys()})
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})

    return jsonl_path, csv_path


# =============================================================================
# Main
# =============================================================================
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forest-version AZMB ex-situ pipeline (GROBID + Ollama), single file")

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

    # Ollama
    parser.add_argument("--ollama_mode", choices=["http", "cli"], default="http")
    parser.add_argument("--ollama_url", default="http://localhost:11434")
    parser.add_argument("--llm_model", default="qwen2.5:14b-instruct")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=1.5)

    # Temperatures (single model, but different tasks)
    parser.add_argument("--stage1_temperature", type=float, default=0.05)
    parser.add_argument("--section_temperature", type=float, default=0.05)
    parser.add_argument("--stage2_temperature", type=float, default=0.05)

    # Section sampling / filtering
    parser.add_argument("--max_chars", type=int, default=12000)
    parser.add_argument("--max_paras", type=int, default=30)
    parser.add_argument("--min_paras", type=int, default=1)
    parser.add_argument("--max_sections_to_process", type=int, default=0,
                        help="0 means process all; otherwise cap the number of sections considered for mining")

    # Prefilter / captions
    parser.add_argument("--no_prefilter", action="store_true", help="Disable prefilter (not recommended)")
    parser.add_argument("--prefilter_allow_yes", action="store_true",
                        help="Allow prefilter to directly set YES in obvious cases (still conservative)")
    parser.add_argument("--save_methodlike", action="store_true", help="Preserve method-like captions in separate bucket")
    parser.add_argument("--keep_caption_blocks", action="store_true", help="Keep caption_blocks in cleaned docs (NOT recommended)")

    # Evidence/structure
    parser.add_argument("--max_structure_items", type=int, default=250)
    parser.add_argument("--max_evidence_snippets", type=int, default=10)
    parser.add_argument("--max_evidence_chars", type=int, default=6000)

    # Early stopping
    parser.add_argument("--early_stop", action="store_true", help="Enable early stopping during section mining")
    parser.add_argument("--early_stop_yes_n", type=int, default=4, help="Stop if high-quality YES evidence >= N")
    parser.add_argument("--early_stop_min_conf", type=float, default=0.75, help="Min confidence for YES to count")
    parser.add_argument("--early_stop_min_quality", type=float, default=0.55,
                        help="Min evidence_quality_score(excerpt) to count YES as high-quality")

    # Cache & artifacts
    parser.add_argument("--use_cache", action="store_true")
    parser.add_argument("--cache_file", default="", help="Cache JSONL path (default: out_dir/cache.jsonl)")
    parser.add_argument("--save_prompts", action="store_true", help="Save prompts/outputs for auditing (disk usage)")
    parser.add_argument("--save_section_prompts", action="store_true", help="Also save per-section prompts/outputs")

    return parser

def main():
    parser = build_arg_parser()
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

    # GROBID client (lazy availability checked per-paper when needed)
    base_url = f"http://{args.grobid_host}:{args.grobid_port}"
    grobid_client = GrobidClient(base_url=base_url, timeout_sec=args.grobid_timeout)

    logging.info(f"Pipeline version: {PIPELINE_VERSION} | Prompt version: {PROMPT_VERSION}")
    logging.info(f"PIIs: {len(pii_list)}")
    logging.info(f"XML dir: {args.xml_dir}")
    logging.info(f"Main PDF dir: {args.pdf_dir}")
    logging.info(f"Supp dir: {args.supp_dir or '(none)'}")
    logging.info(f"Out dir: {out_dir}")
    logging.info(f"GROBID base: {base_url}")
    logging.info(f"Ollama mode={args.ollama_mode} model={args.llm_model} cache={'ON' if args.use_cache else 'OFF'}")
    logging.info(f"EarlyStop={'ON' if args.early_stop else 'OFF'} yes_n={args.early_stop_yes_n} min_conf={args.early_stop_min_conf}")

    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for pii in tqdm(pii_list, desc="Processing PIIs", unit="paper"):
        try:
            r = run_for_pii(
                pii=pii,
                args=args,
                grobid_client=grobid_client,
                cache=cache,
                cache_path=cache_path,
            )
            results.append(r)
        except Exception as e:
            logging.exception(f"[{pii}] FAILED: {e}")
            failures.append({"paper_id": pii, "status": "ERROR", "error": str(e)})
            results.append({"paper_id": pii, "status": "ERROR", "error": str(e)})

    # Write summary
    summary_dir = out_dir / "summary"
    safe_mkdir(summary_dir)
    jsonl_path, csv_path = write_summary(summary_dir, results)
    logging.info(f"Summary JSONL: {jsonl_path}")
    logging.info(f"Summary CSV : {csv_path}")

    if failures:
        fail_path = summary_dir / "failures.jsonl"
        fail_path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in failures), encoding="utf-8")
        logging.info(f"Failures JSONL: {fail_path}")

    # Quick stats
    done = sum(1 for r in results if r.get("status") == "DONE")
    done_s1no = sum(1 for r in results if r.get("status") == "DONE_STAGE1_NO")
    cand = sum(1 for r in results if r.get("candidate_exsitu_lab") == "YES")
    missing = sum(1 for r in results if r.get("status") == "MISSING_MAIN_PDF")
    err = sum(1 for r in results if r.get("status") == "ERROR")

    logging.info("=" * 72)
    logging.info("DONE")
    logging.info(f"Total: {len(results)} | DONE: {done} | Stage1 NO: {done_s1no} | Candidate YES: {cand} | Missing main PDF: {missing} | ERROR: {err}")
    logging.info("=" * 72)

if __name__ == "__main__":
    main()
