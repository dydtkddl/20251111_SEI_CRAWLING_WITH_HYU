from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import requests
from lxml import etree
from tqdm import tqdm

try:
    import pysbd
except Exception:
    pysbd = None


GROBID_URL = "http://localhost:8080/api/processFulltextDocument"

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}

WHITESPACE_RE = re.compile(r"\s+")
HYPHEN_LINEBREAK_RE = re.compile(r"(\w)-\s+(\w)")  # just in case


# ---------------------------
# Text utilities
# ---------------------------
def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00ad", "")  # soft hyphen
    s = s.replace("\n", " ")
    s = HYPHEN_LINEBREAK_RE.sub(r"\1\2", s)
    s = WHITESPACE_RE.sub(" ", s).strip()
    return s


def element_text_without_banned(elem: etree._Element, banned_tags: set[str]) -> str:
    """
    Extract plain text from an element while skipping text in certain tags.
    e.g. skip <ref type="bibr">, <note>, etc.
    """
    parts: List[str] = []

    def walk(e: etree._Element):
        # if this node is banned, skip its subtree entirely
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


# ---------------------------
# TEI extraction
# ---------------------------
def extract_title(root: etree._Element) -> str:
    # best effort: main title or first title
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


def is_inside_any(elem: etree._Element, ancestor_localnames: set[str]) -> bool:
    cur = elem.getparent()
    while cur is not None:
        if etree.QName(cur).localname in ancestor_localnames:
            return True
        cur = cur.getparent()
    return False


def extract_body_sections(root: etree._Element) -> List[Dict]:
    """
    Return list of sections:
      { "heading": str, "paragraphs": [str], "sentences": [[...], ...] }
    - Excludes reference list (<listBibl> etc.)
    - Excludes figure/table/caption areas (<figure>, <table>, <figDesc> ...)
    """
    # Common “non-body-text we don't want”
    SKIP_ANCESTORS = {
        "figure", "figDesc", "table", "listBibl", "biblStruct", "bibl", "back", "note"
    }

    # Prefer grouping by div (section)
    divs = root.xpath("//tei:text/tei:body//tei:div", namespaces=TEI_NS)
    sections: List[Dict] = []

    for div in divs:
        # heading
        head = div.xpath("./tei:head", namespaces=TEI_NS)
        heading = normalize_text(" ".join(head[0].itertext())) if head else ""

        # paragraphs in this div
        ps = div.xpath(".//tei:p", namespaces=TEI_NS)
        paragraphs: List[str] = []
        sentences_by_paragraph: List[List[str]] = []

        for p in ps:
            if is_inside_any(p, SKIP_ANCESTORS):
                continue

            # Prefer GROBID sentence segmentation if present: <p><s>...</s></p>
            s_nodes = p.xpath("./tei:s", namespaces=TEI_NS)
            if s_nodes:
                sents = []
                for s in s_nodes:
                    stxt = element_text_without_banned(s, banned_tags={"ref", "note"})
                    if stxt:
                        sents.append(stxt)

                # paragraph text = join sentences (keeps punctuation)
                ptxt = normalize_text(" ".join(sents))
                if ptxt:
                    paragraphs.append(ptxt)
                    sentences_by_paragraph.append(sents)
                continue

            # Otherwise: extract paragraph text, then split ourselves (optional)
            ptxt = element_text_without_banned(p, banned_tags={"ref", "note"})
            if not ptxt:
                continue

            paragraphs.append(ptxt)

            if pysbd is not None:
                seg = pysbd.Segmenter(language="en", clean=True)
                sents = [normalize_text(x) for x in seg.segment(ptxt) if normalize_text(x)]
            else:
                # fallback: no sentence split
                sents = [ptxt]

            sentences_by_paragraph.append(sents)

        # Skip empty sections
        if paragraphs:
            sections.append({
                "heading": heading,
                "paragraphs": paragraphs,
                "sentences": sentences_by_paragraph,
            })

    return sections


# ---------------------------
# GROBID call
# ---------------------------
def grobid_pdf_to_tei(pdf_path: Path, segment_sentences: bool = True) -> str:
    with pdf_path.open("rb") as f:
        files = {"input": (pdf_path.name, f, "application/pdf")}
        data = {
            # sentence segmentation in TEI (<s> tags)
            "segmentSentences": "1" if segment_sentences else "0",
            # these are optional; keep minimal
            # "consolidateHeader": "1",
        }
        r = requests.post(GROBID_URL, files=files, data=data, timeout=240)
        r.raise_for_status()
        return r.text


def process_pdf(pdf_path: Path) -> Dict:
    tei_xml = grobid_pdf_to_tei(pdf_path, segment_sentences=True)
    root = etree.fromstring(tei_xml.encode("utf-8"))

    title = extract_title(root)
    abstract_paras = extract_abstract_paragraphs(root)
    sections = extract_body_sections(root)

    return {
        "source_file": str(pdf_path),
        "title": title,
        "abstract_paragraphs": abstract_paras,
        "sections": sections,  # section-wise paragraphs + sentences
    }


def run_batch(pdf_dir: Path, out_jsonl: Path):
    pdfs = sorted(pdf_dir.rglob("*.pdf"))
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    with out_jsonl.open("w", encoding="utf-8") as w:
        for pdf in tqdm(pdfs, desc="GROBID -> paragraphs/sentences"):
            try:
                item = process_pdf(pdf)
                w.write(json.dumps(item, ensure_ascii=False) + "\n")
            except Exception as e:
                # fail-safe logging line
                w.write(json.dumps({
                    "source_file": str(pdf),
                    "error": str(e)
                }, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    run_batch(
        pdf_dir=Path("../pdfs"),
        out_jsonl=Path("./01_run_out/tei_paragraph_sentence.jsonl")
    )
