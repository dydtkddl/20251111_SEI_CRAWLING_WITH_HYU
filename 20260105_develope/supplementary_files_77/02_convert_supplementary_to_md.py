# -*- coding: utf-8 -*-
"""
Supplementary Data Converter (PDF / DOC / DOCX -> Markdown)

Features
--------
- Recursively scans input_dir
- PDF  : Marker (best) → PyMuPDF fallback
- DOCX : Pandoc (best, images preserved)
- DOC  : LibreOffice (.doc → .docx) → Pandoc
- Images extracted into <filename>_assets/
- Folder structure preserved
- Robust logging

Usage
-----
python 02_convert_supplementary_to_md.py \
    --input_dir ./02_supplementary \
    --output_dir ./02_supplementary_md \
    --overwrite
"""

import os
import argparse
import logging
import subprocess
import shutil
from pathlib import Path
from tqdm import tqdm

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("convert_status_log.txt", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------
def which(cmd):
    return shutil.which(cmd)

def run(cmd, cwd=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------
# DOC → DOCX (LibreOffice)
# ------------------------------------------------------------------
def convert_doc_to_docx(doc_path: Path, tmp_dir: Path) -> Path | None:
    ensure_dir(tmp_dir)
    if not which("soffice"):
        return None

    cmd = [
        "soffice",
        "--headless",
        "--convert-to", "docx",
        "--outdir", str(tmp_dir),
        str(doc_path)
    ]

    r = run(cmd)
    out = tmp_dir / f"{doc_path.stem}.docx"
    return out if out.exists() else None

# ------------------------------------------------------------------
# DOCX → Markdown (Pandoc)
# ------------------------------------------------------------------
def convert_docx_pandoc(docx: Path, out_md: Path, overwrite: bool) -> bool:
    if out_md.exists() and not overwrite:
        logging.info(f"[SKIP] {out_md}")
        return True

    if not which("pandoc"):
        logging.error("pandoc not found")
        return False

    ensure_dir(out_md.parent)
    assets = out_md.stem + "_assets"

    cmd = [
        "pandoc",
        str(docx.resolve()),   # 🔥 절대경로
        "-t", "gfm",
        "--wrap=none",
        f"--extract-media={assets}",
        "-o", out_md.name
    ]

    r = run(cmd, cwd=out_md.parent)
    if r.returncode != 0:
        logging.error(r.stderr)
        return False

    return True

# ------------------------------------------------------------------
# PDF → Markdown (Marker)
# ------------------------------------------------------------------
def convert_pdf_marker(pdf: Path, out_md: Path, overwrite: bool) -> bool:
    if out_md.exists() and not overwrite:
        logging.info(f"[SKIP] {out_md}")
        return True

    if not which("marker_single"):
        return False

    ensure_dir(out_md.parent)
    tmp = out_md.parent / f"{out_md.stem}__marker_tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    ensure_dir(tmp)

    cmd = [
        "marker_single",
        str(pdf),
        "--output_dir", str(tmp),
        "--output_format", "markdown"
    ]

    r = run(cmd)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    mds = list(tmp.rglob("*.md"))
    if not mds:
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    md_src = max(mds, key=lambda p: p.stat().st_size)
    shutil.move(md_src, out_md)

    assets_src = tmp / "images"
    if assets_src.exists():
        shutil.move(assets_src, out_md.parent / f"{out_md.stem}_assets")

    shutil.rmtree(tmp, ignore_errors=True)
    return True

# ------------------------------------------------------------------
# PDF → Markdown (PyMuPDF fallback)
# ------------------------------------------------------------------
def convert_pdf_pymupdf(pdf: Path, out_md: Path, overwrite: bool) -> bool:
    if out_md.exists() and not overwrite:
        logging.info(f"[SKIP] {out_md}")
        return True

    try:
        import fitz
    except ImportError:
        logging.error("PyMuPDF not installed")
        return False

    ensure_dir(out_md.parent)
    assets = out_md.parent / f"{out_md.stem}_assets"
    ensure_dir(assets)

    doc = fitz.open(pdf)
    lines = [f"# {pdf.stem}\n"]

    img_id = 1
    for i, page in enumerate(doc, start=1):
        lines.append(f"\n## Page {i}\n")
        text = page.get_text("text")
        if text:
            lines.append(text)

        for img in page.get_images(full=True):
            xref = img[0]
            pix = fitz.Pixmap(doc, xref)
            if pix.n > 4:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            name = f"img_{img_id:03d}.png"
            pix.save(assets / name)
            lines.append(f"![{name}](./{assets.name}/{name})\n")
            img_id += 1

    out_md.write_text("\n".join(lines), encoding="utf-8")
    doc.close()
    return True

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    input_root = Path(args.input_dir)
    output_root = Path(args.output_dir)

    files = [
        p for p in input_root.rglob("*")
        if p.suffix.lower() in {".pdf", ".doc", ".docx"}
    ]

    logging.info(f"Found {len(files)} files")

    ok, fail = 0, 0

    for f in tqdm(files, desc="Converting"):
        try:
            rel = f.relative_to(input_root)
            out_md = (output_root / rel).with_suffix(".md")

            if f.suffix.lower() == ".doc":
                tmp = output_root / "_tmp_docx"
                docx = convert_doc_to_docx(f, tmp)
                if not docx:
                    raise RuntimeError("DOC → DOCX failed")
                success = convert_docx_pandoc(docx, out_md, args.overwrite)

            elif f.suffix.lower() == ".docx":
                success = convert_docx_pandoc(f, out_md, args.overwrite)

            elif f.suffix.lower() == ".pdf":
                success = convert_pdf_marker(f, out_md, args.overwrite)
                if not success:
                    success = convert_pdf_pymupdf(f, out_md, args.overwrite)

            else:
                success = False

            if success:
                logging.info(f"[OK] {f} -> {out_md}")
                ok += 1
            else:
                logging.error(f"[FAIL] {f}")
                fail += 1

        except Exception as e:
            logging.error(f"[ERROR] {f} : {e}")
            fail += 1

    logging.info(f"Summary: OK={ok}, FAIL={fail}")

if __name__ == "__main__":
    main()
