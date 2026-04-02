# -*- coding: utf-8 -*-
"""
GROBID Processing for Supplementary Files (SI-friendly)
- Recursive search: PDF, DOC, DOCX
- DOC/DOCX -> PDF via LibreOffice
- GROBID fulltext -> TEI
- Extract:
  1) regular body sections (div/head + paragraphs)
  2) figure/table caption blocks as standalone "heading" items
  3) postprocess: split/detach caption-like paragraphs in body text

Output: JSONL
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from lxml import etree
from tqdm import tqdm

try:
    import pysbd
except ImportError:
    pysbd = None


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
DEFAULT_GROBID_HOST = "localhost"
DEFAULT_GROBID_PORT = "8070"  # docker mapping often uses -p 8070:8070
DEFAULT_TIMEOUT_SEC = 300

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}

WHITESPACE_RE = re.compile(r"\s+")
HYPHEN_LINEBREAK_RE = re.compile(r"(\w)-\s+(\w)")

# SI PDFs often include page-header artifacts like "S-2" at line start
SI_PAGE_MARKER_RE = re.compile(r"^\s*S-\d+\b\s*", re.IGNORECASE)

# Caption label patterns (robust for SI)
# Examples:
#   Fig. S3: ...
#   Figure S10. ...
#   Table S1 ...
#   Fig. 2 ...
CAPTION_LABEL_RE = re.compile(
    r"(?P<label>\b(?:Fig(?:ure)?|Table)\.?\s*(?:S?\d+[A-Za-z]?)\b)\s*[:\.]?",
    re.IGNORECASE,
)

# Some SI captions appear as "S2. ..." without "Figure"
S_ONLY_CAPTION_RE = re.compile(r"(?P<label>\bS\d+\b)\s*[:\.]\s+", re.IGNORECASE)

# For captions without a number sometimes:
#   Fig. The cross-sectional SEM image ...
CAPTION_NONUM_RE = re.compile(r"^\s*(?P<label>Fig(?:ure)?|Table)\.?\s*[:\.]?\s+", re.IGNORECASE)

# Results-ish headings to avoid treating as caption blocks by mistake (optional use)
RESULTSISH_RE = re.compile(
    r"\b(results?|discussion|performance|mechanism|regulation|evolution|behavior|kinetics|dynamics)\b",
    re.IGNORECASE
)


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00ad", "")  # soft hyphen
    s = s.replace("\n", " ")
    s = HYPHEN_LINEBREAK_RE.sub(r"\1\2", s)
    s = WHITESPACE_RE.sub(" ", s).strip()
    return s


def strip_si_page_marker(s: str) -> str:
    # Remove leading "S-2" like page markers (common in SI)
    return SI_PAGE_MARKER_RE.sub("", s or "")


def element_text_without_banned(elem: etree._Element, banned_tags: Set[str]) -> str:
    """Extract plain text from an element while skipping certain tags."""
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


def make_segmenter():
    if pysbd is None:
        return None
    return pysbd.Segmenter(language="en", clean=True)


def sentencize(text: str, seg) -> List[str]:
    text = normalize_text(text)
    if not text:
        return []
    if seg is None:
        return [text]
    return [normalize_text(x) for x in seg.segment(text) if normalize_text(x)]


# -----------------------------------------------------------------------------
# TEI Extractors
# -----------------------------------------------------------------------------
def extract_title(root: etree._Element) -> str:
    t = root.xpath("//tei:teiHeader//tei:titleStmt/tei:title[@type='main']/text()", namespaces=TEI_NS)
    if not t:
        t = root.xpath("//tei:teiHeader//tei:titleStmt/tei:title/text()", namespaces=TEI_NS)
    return normalize_text(t[0]) if t else ""


def extract_abstract_paragraphs(root: etree._Element) -> List[str]:
    ps = root.xpath("//tei:teiHeader//tei:profileDesc//tei:abstract//tei:p", namespaces=TEI_NS)
    out = []
    for p in ps:
        txt = element_text_without_banned(p, banned_tags={"ref", "note"})
        if txt:
            out.append(txt)
    return out


def nearest_div_is_self(p: etree._Element, div: etree._Element) -> bool:
    """Return True if the nearest ancestor tei:div of p is exactly div (avoid duplication across nested divs)."""
    anc = p.xpath("ancestor::tei:div[1]", namespaces=TEI_NS)
    return bool(anc) and (anc[0] is div)


def is_inside_any(elem: etree._Element, ancestor_localnames: Set[str]) -> bool:
    cur = elem.getparent()
    while cur is not None:
        if etree.QName(cur).localname in ancestor_localnames:
            return True
        cur = cur.getparent()
    return False


def extract_body_sections(root: etree._Element) -> List[Dict]:
    """
    Extract body sections (tei:div) with heading, paragraphs, sentences.
    NOTE: We intentionally do NOT pull figure/table captions here (handled separately),
    but we still need to handle cases where captions are flattened into <p>.
    """
    SKIP_ANCESTORS = {"listBibl", "biblStruct", "bibl", "back", "note", "figure", "figDesc", "table"}

    divs = root.xpath("//tei:text/tei:body//tei:div", namespaces=TEI_NS)
    sections: List[Dict] = []
    seg = make_segmenter()

    for div in divs:
        # Heading
        head = div.xpath("./tei:head", namespaces=TEI_NS)
        heading = normalize_text(" ".join(head[0].itertext())) if head else ""
        heading = strip_si_page_marker(heading)

        # Collect paragraphs whose nearest div ancestor is this div
        ps = div.xpath(".//tei:p", namespaces=TEI_NS)

        paragraphs: List[str] = []
        sentences_by_paragraph: List[List[str]] = []

        for p in ps:
            if not nearest_div_is_self(p, div):
                continue
            if is_inside_any(p, SKIP_ANCESTORS):
                continue

            # If GROBID sentence tags <s> exist
            s_nodes = p.xpath("./tei:s", namespaces=TEI_NS)
            if s_nodes:
                sents = []
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

            # Fallback: plain p text
            ptxt = element_text_without_banned(p, banned_tags={"ref", "note"})
            ptxt = strip_si_page_marker(ptxt)
            if not ptxt:
                continue

            paragraphs.append(ptxt)
            sentences_by_paragraph.append(sentencize(ptxt, seg))

        if paragraphs:
            # section level = number of div ancestors inside body
            level = len(div.xpath("ancestor::tei:div", namespaces=TEI_NS)) + 1
            sections.append({
                "kind": "section",
                "level": level,
                "heading": heading,
                "paragraphs": paragraphs,
                "sentences": sentences_by_paragraph,
            })

    return sections


def table_elem_to_text(table_elem: etree._Element) -> str:
    """Try to convert TEI table into TSV-like text (best-effort)."""
    rows = table_elem.xpath(".//tei:row", namespaces=TEI_NS)
    if not rows:
        # fallback: plain text
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


def extract_figure_table_blocks(root: etree._Element) -> List[Dict]:
    """
    Extract figure/table captions directly from TEI <figure> and <table>.
    Output blocks have:
      - kind: "figure_caption" or "table_caption"
      - heading: label (e.g., "Fig. S3", "Table S1")
      - paragraphs/sentences: caption text
    """
    blocks: List[Dict] = []
    seg = make_segmenter()

    # 1) <figure> (including <figure type="table">)
    figures = root.xpath("//tei:text/tei:body//tei:figure", namespaces=TEI_NS)
    for fig in figures:
        ftype = (fig.get("type") or "").lower()
        is_table = (ftype == "table")

        label = normalize_text(" ".join(fig.xpath("./tei:label//text()", namespaces=TEI_NS)))
        head = normalize_text(" ".join(fig.xpath("./tei:head//text()", namespaces=TEI_NS)))
        desc = normalize_text(" ".join(fig.xpath(".//tei:figDesc//text()", namespaces=TEI_NS)))

        # Sometimes caption is in <p> inside figure
        p_texts = []
        for p in fig.xpath(".//tei:p", namespaces=TEI_NS):
            ptxt = element_text_without_banned(p, banned_tags={"ref", "note"})
            if ptxt:
                p_texts.append(ptxt)
        caption = normalize_text(" ".join([x for x in [desc] + p_texts if x]))

        # Determine heading
        heading = ""
        if label:
            heading = label
        elif head:
            heading = head
        else:
            heading = "Table" if is_table else "Figure"

        heading = strip_si_page_marker(heading)
        caption = strip_si_page_marker(caption)

        if not caption:
            continue

        kind = "table_caption" if is_table else "figure_caption"
        blocks.append({
            "kind": kind,
            "heading": heading,
            "paragraphs": [caption],
            "sentences": [sentencize(caption, seg)],
        })

    # 2) standalone <table> elements (best-effort)
    tables = root.xpath("//tei:text/tei:body//tei:table", namespaces=TEI_NS)
    for tb in tables:
        # Skip tables already inside <figure type="table"> (avoid duplicates)
        if tb.xpath("ancestor::tei:figure[@type='table']", namespaces=TEI_NS):
            continue

        txt = table_elem_to_text(tb)
        txt = strip_si_page_marker(txt)
        if not txt:
            continue

        blocks.append({
            "kind": "table_content",
            "heading": "Table",
            "paragraphs": [txt],
            "sentences": [sentencize(txt, seg)],
        })

    return blocks


# -----------------------------------------------------------------------------
# Caption post-processing from loose paragraphs
# -----------------------------------------------------------------------------
def _split_caption_chunks(text: str) -> List[Tuple[str, str]]:
    """
    Split a text into caption chunks when multiple Fig/Table labels appear.
    Returns list of (heading_label, caption_text).
    Only used when it looks caption-like (labels appear).
    """
    if not text:
        return []

    t = strip_si_page_marker(normalize_text(text))
    if not t:
        return []

    matches = list(CAPTION_LABEL_RE.finditer(t))
    # Also allow "S2." style (weak)
    s_only = list(S_ONLY_CAPTION_RE.finditer(t))

    # If there are multiple Fig/Table labels, split by them.
    if len(matches) >= 2:
        chunks = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
            chunk = t[start:end].strip()
            lab = normalize_text(m.group("label"))
            # Remove label token from caption body if it repeats
            body = chunk
            # remove leading label + punctuation
            body = re.sub(r"^\s*" + re.escape(lab) + r"\s*[:\.]?\s*", "", body, flags=re.IGNORECASE)
            body = body.strip()
            if body:
                chunks.append((lab, body))
        return chunks

    # If the paragraph starts with a Fig/Table label -> single caption chunk
    m0 = CAPTION_LABEL_RE.match(t)
    if m0:
        lab = normalize_text(m0.group("label"))
        body = re.sub(r"^\s*" + re.escape(lab) + r"\s*[:\.]?\s*", "", t, flags=re.IGNORECASE).strip()
        if body:
            return [(lab, body)]

    # If it starts with "S2." style and looks like a caption
    mS = S_ONLY_CAPTION_RE.match(t)
    if mS:
        lab = normalize_text(mS.group("label"))
        body = t[mS.end():].strip()
        # heuristic: treat as "Figure Sx"
        heading = f"Figure {lab}"
        if body:
            return [(heading, body)]

    # Non-number caption like "Fig. The ..."
    mN = CAPTION_NONUM_RE.match(t)
    if mN:
        lab = normalize_text(mN.group("label"))
        body = CAPTION_NONUM_RE.sub("", t).strip()
        if body:
            heading = "Figure" if lab.lower().startswith("fig") else "Table"
            return [(heading, body)]

    return []


def detach_caption_like_paragraphs(sections: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Scan normal section paragraphs and detach caption-like paragraphs into caption_blocks.
    - If a paragraph starts with "Fig./Figure/Table ..." or contains multiple labels, split it.
    - Remove those paragraphs from the original section to avoid duplication.
    """
    seg = make_segmenter()
    new_sections: List[Dict] = []
    caption_blocks: List[Dict] = []

    for sec in sections:
        paras = sec.get("paragraphs", [])
        sents = sec.get("sentences", [])
        if not paras:
            continue

        kept_paras: List[str] = []
        kept_sents: List[List[str]] = []

        for ptxt, psents in zip(paras, sents):
            chunks = _split_caption_chunks(ptxt)

            if chunks:
                for heading, body in chunks:
                    caption_blocks.append({
                        "kind": "caption_from_body",
                        "heading": heading,
                        "paragraphs": [body],
                        "sentences": [sentencize(body, seg)],
                    })
                # drop this paragraph from the original section
                continue

            kept_paras.append(ptxt)
            kept_sents.append(psents)

        if kept_paras:
            new_sec = dict(sec)
            new_sec["paragraphs"] = kept_paras
            new_sec["sentences"] = kept_sents
            new_sections.append(new_sec)

    return new_sections, caption_blocks


def dedupe_blocks(blocks: List[Dict]) -> List[Dict]:
    """Deduplicate caption blocks by normalized (heading + caption text)."""
    seen = set()
    out = []
    for b in blocks:
        h = normalize_text(b.get("heading", ""))
        p = normalize_text(" ".join(b.get("paragraphs", [])))
        key = (h.lower(), p.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


# -----------------------------------------------------------------------------
# GROBID client
# -----------------------------------------------------------------------------
@dataclass
class GrobidClient:
    base_url: str
    timeout_sec: int = DEFAULT_TIMEOUT_SEC

    def __post_init__(self):
        self.session = requests.Session()

    def is_alive(self) -> bool:
        # GROBID provides /api/isalive
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
                # Example: "figure,table,head,p" (depends on grobid version; safe to omit if unsure)
                data["teiCoordinates"] = tei_coordinates

            r = self.session.post(url, files=files, data=data, timeout=self.timeout_sec)
            r.raise_for_status()
            return r.text


def parse_grobid_tei(tei_xml: str) -> etree._Element:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(tei_xml.encode("utf-8"), parser=parser)


# -----------------------------------------------------------------------------
# File conversion
# -----------------------------------------------------------------------------
def get_libreoffice_cmd() -> Optional[str]:
    for cmd in ["soffice", "libreoffice"]:
        if shutil.which(cmd):
            return cmd

    if sys.platform == "win32":
        possible_paths = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for p in possible_paths:
            if Path(p).exists():
                return p
    return None


def convert_to_pdf_libreoffice(source_file: Path) -> Optional[Path]:
    target_pdf = source_file.with_suffix(".pdf")
    if target_pdf.exists():
        return target_pdf

    lo_cmd = get_libreoffice_cmd()
    if not lo_cmd:
        print(f"[Warning] LibreOffice not found. Cannot convert: {source_file}")
        return None

    try:
        cmd = [
            lo_cmd,
            "--headless",
            "--convert-to", "pdf",
            "--outdir", str(source_file.parent),
            str(source_file)
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return target_pdf if target_pdf.exists() else None
    except subprocess.CalledProcessError as e:
        print(f"[Warning] LibreOffice conversion failed for {source_file.name}: {e.stderr.decode(errors='ignore').strip()}")
        return None
    except Exception as e:
        print(f"[Warning] Unexpected error converting {source_file.name}: {e}")
        return None


# -----------------------------------------------------------------------------
# Main pipeline
# -----------------------------------------------------------------------------
def find_candidates(input_root: Path) -> List[Path]:
    out = []
    for p in input_root.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in {".pdf", ".doc", ".docx"}:
            out.append(p)
    return sorted(out)


def run_supplementary_pipeline(
    input_root: Path,
    output_jsonl: Path,
    grobid_host: str = DEFAULT_GROBID_HOST,
    grobid_port: str = DEFAULT_GROBID_PORT,
    include_captions_in_sections: bool = True,
    segment_sentences: bool = True,
    tei_coordinates: Optional[str] = None,
):
    base_url = f"http://{grobid_host}:{grobid_port}"
    client = GrobidClient(base_url=base_url, timeout_sec=DEFAULT_TIMEOUT_SEC)

    print(f"Checking GROBID server at {base_url} ...")
    if not client.is_alive():
        print("\n" + "!" * 60)
        print(f"ERROR: GROBID is not reachable at {base_url}")
        print(f"Example Docker run:\n  docker run --rm -p {grobid_port}:8070 lfoppiano/grobid:0.8.0")
        print("!" * 60 + "\n")
        sys.exit(1)

    if not input_root.exists():
        print(f"[Error] Directory not found: {input_root}")
        return

    candidates = find_candidates(input_root)
    if not candidates:
        print("No PDF/DOC/DOCX files found.")
        return

    print(f"Found {len(candidates)} files.")
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    processed_pdfs: Set[str] = set()

    with output_jsonl.open("w", encoding="utf-8") as f_out:
        for src in tqdm(candidates, desc="Processing"):
            ext = src.suffix.lower()
            pdf_to_process: Optional[Path] = None
            was_converted = False

            try:
                if ext == ".pdf":
                    pdf_to_process = src
                else:
                    pdf_to_process = convert_to_pdf_libreoffice(src)
                    was_converted = True

                if not pdf_to_process:
                    continue

                # avoid double processing if both DOCX->PDF and an existing PDF share same path
                pdf_key = str(pdf_to_process.resolve())
                if pdf_key in processed_pdfs:
                    continue
                processed_pdfs.add(pdf_key)

                tei_xml = client.process_fulltext(
                    pdf_to_process,
                    segment_sentences=segment_sentences,
                    generate_ids=True,
                    tei_coordinates=tei_coordinates,
                )

                root = parse_grobid_tei(tei_xml)

                title = extract_title(root)
                abstract_paras = extract_abstract_paragraphs(root)

                # 1) regular sections
                sections = extract_body_sections(root)

                # 2) figure/table blocks from TEI
                figtab_blocks = extract_figure_table_blocks(root)

                # 3) detach caption-like paragraphs from body text (handles flattened SI captions)
                sections, caption_from_body = detach_caption_like_paragraphs(sections)

                # 4) merge/dedupe caption blocks
                caption_blocks = dedupe_blocks(figtab_blocks + caption_from_body)

                # optionally append captions into sections for downstream "heading" access
                if include_captions_in_sections and caption_blocks:
                    merged = list(sections)
                    for b in caption_blocks:
                        merged.append({
                            "kind": b.get("kind", "caption"),
                            "level": 0,
                            "heading": b.get("heading", ""),
                            "paragraphs": b.get("paragraphs", []),
                            "sentences": b.get("sentences", []),
                        })
                    sections_out = merged
                else:
                    sections_out = sections

                out = {
                    "source_file": str(src),
                    "processed_pdf": str(pdf_to_process),
                    "original_file_type": ext,
                    "was_converted_from_word": was_converted,
                    "title": title,
                    "abstract_paragraphs": abstract_paras,
                    "sections": sections_out,
                    "caption_blocks": caption_blocks,  # keep separately too (debug/analysis)
                }

                f_out.write(json.dumps(out, ensure_ascii=False) + "\n")
                f_out.flush()

            except Exception as e:
                err = {
                    "source_file": str(src),
                    "processed_pdf": str(pdf_to_process) if pdf_to_process else "",
                    "original_file_type": ext,
                    "error": str(e),
                }
                f_out.write(json.dumps(err, ensure_ascii=False) + "\n")
                f_out.flush()


# -----------------------------------------------------------------------------
# Entrypoint (keep your original default paths)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    target_supp_dir = base_dir.parent / "supplementary_files" / "02_supplementary"
    output_jsonl = base_dir / "06_run_supplementary_out" / "supplementary_grobid_results.jsonl"

    run_supplementary_pipeline(
        input_root=target_supp_dir,
        output_jsonl=output_jsonl,
        grobid_host=DEFAULT_GROBID_HOST,
        grobid_port=DEFAULT_GROBID_PORT,
        include_captions_in_sections=True,
        segment_sentences=True,
        # tei_coordinates="figure,table,head,p"  # 필요하면 켜기(버전에 따라 미지원이면 None)
        tei_coordinates=None,
    )
