# -*- coding: utf-8 -*-
"""
grobid_extract_clean_sections.py

Goal
- PDF -> (GROBID TEI XML) -> clean JSON
- Keep: title, abstract (paragraphs), body sections (HIERARCHY preserved), paragraphs, sentences
- Drop: references/back matter, figures/figure captions, tables, bibliography, etc.

Key fixes vs your current output
1) Section numbering:
   - GROBID often stores numbering in <label> or @n attribute, not in head text.
   - We build heading as: "<label/@n> <head text>"

2) Methods -> subsections structure:
   - Instead of collecting all divs flat with //div,
     we parse top-level body divs and recursively parse child divs (children).

3) "문맥상 한 문단인데 \n 또는 ." 때문에 쪼개지는 문제:
   - Use GROBID sentence segmentation if available (segmentSentences=1)
   - Otherwise, do robust sentence splitting (spaCy -> pysbd -> nltk -> regex fallback)
   - Paragraphs come from TEI <p> (so random \n won't split paragraphs)

How to run
- Start GROBID server (docker example):
  docker run --rm -p 8070:8070 lfoppiano/grobid:0.8.0

- Extract one PDF:
  python grobid_extract_clean_sections.py --pdf "path/to/file.pdf" --out_json out.json

- Extract a directory of PDFs:
  python grobid_extract_clean_sections.py --pdf_dir "./pdfs" --out_json out.json --save_tei_dir "./tei_out"

Optional dependencies (quality order):
- spaCy + model (best): pip install spacy && python -m spacy download en_core_web_sm
- pysbd (very good): pip install pysbd
- nltk (ok): pip install nltk  (script tries to download punkt once)

Required:
- pip install requests lxml
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from lxml import etree

try:
    from tqdm import tqdm
except Exception:
    tqdm = None

# ----------------------------
# Optional sentence splitters
# ----------------------------
_SPACY_NLP = None
_PYSBD = None
_NLTK_AVAILABLE = False

def _init_sentence_splitters():
    global _SPACY_NLP, _PYSBD, _NLTK_AVAILABLE

    # 1) spaCy (best if model exists)
    try:
        import spacy  # type: ignore
        try:
            _SPACY_NLP = spacy.load("en_core_web_sm", disable=["ner", "tagger", "lemmatizer"])
        except Exception:
            _SPACY_NLP = None
    except Exception:
        _SPACY_NLP = None

    # 2) pysbd
    try:
        import pysbd  # type: ignore
        _PYSBD = pysbd.Segmenter(language="en", clean=True)
    except Exception:
        _PYSBD = None

    # 3) nltk punkt
    try:
        import nltk  # type: ignore
        try:
            nltk.data.find("tokenizers/punkt")
        except Exception:
            try:
                nltk.download("punkt", quiet=True)
            except Exception:
                pass
        try:
            nltk.data.find("tokenizers/punkt")
            _NLTK_AVAILABLE = True
        except Exception:
            _NLTK_AVAILABLE = False
    except Exception:
        _NLTK_AVAILABLE = False


# ----------------------------
# TEI namespace
# ----------------------------
TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}

# ----------------------------
# Cleaning helpers
# ----------------------------
WHITESPACE_RE = re.compile(r"\s+")
MULTISPACE_RE = re.compile(r"[ \t]+")

def normalize_text(s: str) -> str:
    if not s:
        return ""
    # normalize whitespace
    s = s.replace("\u00a0", " ")  # nbsp
    s = s.replace("\u2009", " ")  # thin space
    s = s.replace("\u202f", " ")  # narrow nbsp
    s = WHITESPACE_RE.sub(" ", s).strip()
    return s

def join_with_hyphen_fix(chunks: List[str]) -> str:
    """
    Join text chunks while fixing line-break hyphenation:
    "... electro-" + "chemical" -> "... electrochemical"
    """
    out = ""
    for c in chunks:
        c = c or ""
        if not c:
            continue
        if out.endswith("-") and c and c[:1].isalpha():
            out = out[:-1] + c  # drop hyphen + no space
        else:
            if out and not out.endswith((" ", "\n")):
                out += " "
            out += c
    return normalize_text(out)

def iter_text_chunks_excluding(el: etree._Element, banned_tags: set) -> List[str]:
    """
    Collect text chunks from element while skipping text inside banned tags.
    """
    chunks: List[str] = []

    def rec(node: etree._Element):
        tag = etree.QName(node.tag).localname if isinstance(node.tag, str) else ""
        if tag in banned_tags:
            return

        if node.text:
            chunks.append(node.text)

        for child in node:
            if isinstance(child.tag, str):
                rec(child)
            if child.tail:
                chunks.append(child.tail)

    rec(el)
    return chunks

def element_text_without_banned(el: etree._Element, banned_tags: set) -> str:
    chunks = iter_text_chunks_excluding(el, banned_tags=banned_tags)
    return join_with_hyphen_fix(chunks)

def is_inside_any(el: etree._Element, banned_ancestors: set) -> bool:
    """
    True if element has an ancestor with one of banned_ancestors local tag names.
    """
    p = el.getparent()
    while p is not None:
        if isinstance(p.tag, str):
            lname = etree.QName(p.tag).localname
            if lname in banned_ancestors:
                return True
        p = p.getparent()
    return False

def looks_like_reference_section(title: str) -> bool:
    t = normalize_text(title).lower()
    return t in {
        "references", "reference", "bibliography", "literature", "literature cited", "works cited"
    } or t.startswith("references") or t.startswith("bibliography")


# ----------------------------
# Sentence splitting
# ----------------------------
def split_sentences_best_effort(text: str) -> List[str]:
    text = normalize_text(text)
    if not text:
        return []

    # spaCy
    if _SPACY_NLP is not None:
        try:
            doc = _SPACY_NLP(text)
            out = [normalize_text(s.text) for s in doc.sents]
            return [s for s in out if s]
        except Exception:
            pass

    # pysbd
    if _PYSBD is not None:
        try:
            out = [normalize_text(s) for s in _PYSBD.segment(text)]
            return [s for s in out if s]
        except Exception:
            pass

    # nltk punkt
    if _NLTK_AVAILABLE:
        try:
            import nltk  # type: ignore
            out = [normalize_text(s) for s in nltk.sent_tokenize(text)]
            return [s for s in out if s]
        except Exception:
            pass

    # fallback regex (rough)
    out = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text)
    out = [normalize_text(s) for s in out]
    return [s for s in out if s]


# ----------------------------
# GROBID client
# ----------------------------
def grobid_process_fulltext(
    pdf_path: Path,
    grobid_url: str,
    timeout: int = 180,
    segment_sentences: bool = True,
) -> str:
    """
    Call GROBID /api/processFulltextDocument and return TEI XML string.
    """
    api = grobid_url.rstrip("/") + "/api/processFulltextDocument"

    params = {
        "consolidateHeader": "0",
        "consolidateCitations": "0",
        "includeRawCitations": "0",
        "includeRawAffiliations": "0",
        "generateIDs": "0",
        "segmentSentences": "1" if segment_sentences else "0",
    }

    with pdf_path.open("rb") as f:
        files = {"input": (pdf_path.name, f, "application/pdf")}
        r = requests.post(api, files=files, params=params, timeout=timeout)

    if r.status_code != 200 or not r.text.strip():
        raise RuntimeError(f"GROBID failed ({r.status_code}): {r.text[:300]}")

    return r.text


# ----------------------------
# TEI parsing
# ----------------------------
def parse_tei_root(tei_xml: str) -> etree._Element:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(tei_xml.encode("utf-8", errors="ignore"), parser=parser)
    return root

def extract_title(root: etree._Element) -> str:
    # common: teiHeader/fileDesc/titleStmt/title
    title = root.xpath("string(//tei:teiHeader//tei:fileDesc//tei:titleStmt//tei:title[1])", namespaces=TEI_NS)
    title = normalize_text(title)
    if title:
        return title
    # fallback: first head
    head = root.xpath("string(//tei:text//tei:body//tei:head[1])", namespaces=TEI_NS)
    return normalize_text(head)

def extract_abstract_paragraphs(root: etree._Element) -> List[str]:
    # common patterns
    paras = root.xpath("//tei:text//tei:front//tei:div[@type='abstract']//tei:p", namespaces=TEI_NS)
    if not paras:
        paras = root.xpath("//tei:teiHeader//tei:profileDesc//tei:abstract//tei:p", namespaces=TEI_NS)
    out: List[str] = []
    for p in paras:
        txt = element_text_without_banned(p, banned_tags={"ref", "note"})
        txt = normalize_text(txt)
        if txt:
            out.append(txt)
    return out

def get_div_heading(div: etree._Element) -> str:
    """
    Build heading including numbering:
    - prefer <head><label>...</label> + head text (without label)
    - or use @n on head or div
    """
    head = div.find("tei:head", namespaces=TEI_NS)
    if head is None:
        label = normalize_text(div.get("n") or "")
        return label

    label_txt = head.findtext("tei:label", namespaces=TEI_NS)
    label_txt = normalize_text(label_txt) if label_txt else ""

    if not label_txt:
        label_txt = normalize_text(head.get("n") or div.get("n") or "")

    head_txt = element_text_without_banned(head, banned_tags={"ref", "note"})
    head_txt = normalize_text(head_txt)

    # If label already included in head_txt, avoid duplication
    if label_txt and head_txt.lower().startswith(label_txt.lower()):
        full = head_txt
    else:
        full = f"{label_txt} {head_txt}".strip()

    return normalize_text(full)

def extract_body_sections_tree(root: etree._Element) -> List[Dict]:
    """
    Preserve hierarchy: body/div -> child divs.
    Collect only direct paragraphs of that div (./p), not nested (no duplication).
    """
    SKIP_ANCESTORS = {
        "figure", "figDesc", "table", "listBibl", "biblStruct", "bibl",
        "note", "back"
    }

    def is_skip_paragraph(p: etree._Element) -> bool:
        return is_inside_any(p, SKIP_ANCESTORS)

    def extract_paragraph_sentences(p: etree._Element) -> Tuple[str, List[str]]:
        # If GROBID sentence segmentation exists: <p><s>...</s></p>
        s_nodes = p.xpath("./tei:s", namespaces=TEI_NS)
        if s_nodes:
            sents: List[str] = []
            for s in s_nodes:
                stxt = element_text_without_banned(s, banned_tags={"ref", "note"})
                stxt = normalize_text(stxt)
                if stxt:
                    sents.append(stxt)
            ptxt = normalize_text(" ".join(sents))
            return ptxt, sents

        # Otherwise: whole paragraph then split
        ptxt = element_text_without_banned(p, banned_tags={"ref", "note"})
        ptxt = normalize_text(ptxt)
        if not ptxt:
            return "", []
        return ptxt, split_sentences_best_effort(ptxt)

    def parse_div(div: etree._Element, level: int) -> Optional[Dict]:
        heading = get_div_heading(div)
        if looks_like_reference_section(heading):
            return None

        # direct paragraphs only
        ps = div.xpath("./tei:p", namespaces=TEI_NS)

        paragraphs: List[str] = []
        sentences_by_paragraph: List[List[str]] = []

        for p in ps:
            if is_skip_paragraph(p):
                continue
            ptxt, sents = extract_paragraph_sentences(p)
            if ptxt:
                paragraphs.append(ptxt)
                sentences_by_paragraph.append(sents)

        # children
        child_divs = div.xpath("./tei:div", namespaces=TEI_NS)
        children: List[Dict] = []
        for cd in child_divs:
            node = parse_div(cd, level + 1)
            if node is not None:
                children.append(node)

        # If completely empty and no children, drop it
        if (not heading) and (not paragraphs) and (not children):
            return None

        return {
            "level": level,
            "heading": heading,
            "paragraphs": paragraphs,
            "sentences": sentences_by_paragraph,
            "children": children,
        }

    top_divs = root.xpath("//tei:text/tei:body/tei:div", namespaces=TEI_NS)

    out: List[Dict] = []
    for d in top_divs:
        node = parse_div(d, level=1)
        if node is not None:
            out.append(node)
    return out

def flatten_sections_tree(sections: List[Dict]) -> List[Dict]:
    """
    Flatten tree into list with a 'path' string for easy chunking.
    """
    flat: List[Dict] = []

    def rec(node: Dict, stack: List[str]):
        heading = node.get("heading") or ""
        new_stack = stack + ([heading] if heading else [])
        path = " > ".join([h for h in new_stack if h])

        flat.append({
            "path": path,
            "level": node.get("level", None),
            "heading": heading,
            "paragraphs": node.get("paragraphs", []),
            "sentences": node.get("sentences", []),
        })

        for ch in node.get("children", []) or []:
            rec(ch, new_stack)

    for n in sections:
        rec(n, [])
    return flat


# ----------------------------
# IO / CLI
# ----------------------------
def list_pdfs(pdf: Optional[str], pdf_dir: Optional[str]) -> List[Path]:
    paths: List[Path] = []
    if pdf:
        p = Path(pdf)
        if not p.exists():
            raise FileNotFoundError(str(p))
        paths.append(p)
    if pdf_dir:
        d = Path(pdf_dir)
        if not d.exists():
            raise FileNotFoundError(str(d))
        paths.extend(sorted(d.rglob("*.pdf")))
    # unique
    uniq = []
    seen = set()
    for p in paths:
        rp = str(p.resolve())
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def main():
    _init_sentence_splitters()

    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", type=str, default=None, help="single PDF path")
    ap.add_argument("--pdf_dir", type=str, default="../pdfs", help="directory containing PDFs (recursive)")
    ap.add_argument("--out_json", type=str, required=True, help="output JSON path")
    ap.add_argument("--grobid_url", type=str, default="http://localhost:8070", help="GROBID server URL")
    ap.add_argument("--timeout", type=int, default=180, help="request timeout (seconds)")
    ap.add_argument("--no_segment_sentences", action="store_true", help="disable GROBID sentence segmentation")
    ap.add_argument("--save_tei_dir", type=str, default=None, help="optional dir to save TEI XML files")
    ap.add_argument("--flatten", action="store_true", help="also save flattened sections list")
    args = ap.parse_args()

    pdfs = list_pdfs(args.pdf, args.pdf_dir)
    if not pdfs:
        print("No PDFs found.", file=sys.stderr)
        sys.exit(1)

    tei_dir = Path(args.save_tei_dir) if args.save_tei_dir else None
    if tei_dir:
        ensure_dir(tei_dir)

    iterator = tqdm(pdfs, desc="Processing PDFs") if tqdm else pdfs

    results: List[Dict] = []

    for pdf_path in iterator:
        try:
            tei_xml = grobid_process_fulltext(
                pdf_path=pdf_path,
                grobid_url=args.grobid_url,
                timeout=args.timeout,
                segment_sentences=(not args.no_segment_sentences),
            )

            if tei_dir:
                tei_path = tei_dir / (pdf_path.stem + ".tei.xml")
                tei_path.write_text(tei_xml, encoding="utf-8", errors="ignore")

            root = parse_tei_root(tei_xml)

            title = extract_title(root)
            abstract_paras = extract_abstract_paragraphs(root)
            sections_tree = extract_body_sections_tree(root)

            item = {
                "source_file": str(pdf_path),
                "title": title,
                "abstract_paragraphs": abstract_paras,
                "sections": sections_tree,  # hierarchical
            }

            if args.flatten:
                item["sections_flat"] = flatten_sections_tree(sections_tree)

            results.append(item)

        except Exception as e:
            # keep going, but record error
            results.append({
                "source_file": str(pdf_path),
                "error": str(e),
            })

    out_path = Path(args.out_json)
    ensure_dir(out_path.parent)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {out_path} (items={len(results)})")


if __name__ == "__main__":
    main()
