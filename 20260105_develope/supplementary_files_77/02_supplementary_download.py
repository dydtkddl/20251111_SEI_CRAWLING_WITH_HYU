# -*- coding: utf-8 -*-
"""
High-Quality Supplementary Converter (PDF/Word -> Markdown)
- Recursively scans input_dir for .pdf, .doc, .docx
- Converts to Markdown with best-effort high quality:
  * PDF  : Marker (marker_single)  -> best structure + images
          fallback: PyMuPDF (fitz) -> text + image extraction (simpler structure)
  * DOCX : Pandoc -> great heading/section preservation + --extract-media for images
  * DOC  : LibreOffice(soffice) -> DOCX -> Pandoc
          fallback: MarkItDown (if installed)

Install recommendations (Windows / Linux):
1) PDF (best): Marker
   pip install marker-pdf
   (marker_single should be available)  # see Marker docs :contentReference[oaicite:2]{index=2}

2) DOCX (best): Pandoc (system install)
   - Windows: install Pandoc from official installer
   - Ubuntu : sudo apt-get install pandoc
   Pandoc image extraction uses --extract-media  # :contentReference[oaicite:3]{index=3}

3) DOC (optional but recommended): LibreOffice for .doc -> .docx
   - Windows: install LibreOffice (soffice.exe)
   - Ubuntu : sudo apt-get install libreoffice

Optional fallback:
   pip install markitdown

Usage:
  python 02_convert_supplementary_to_md.py --input_dir ./02_supplementary --output_dir ./02_supplementary_md
  python 02_convert_supplementary_to_md.py --input_dir ... --output_dir ... --overwrite
  python 02_convert_supplementary_to_md.py --input_dir ... --output_dir ... --pdf_engine marker
"""

import os
import re
import shutil
import argparse
import logging
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple, List

from tqdm import tqdm


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("convert_status_log.txt", encoding="utf-8"),
        logging.StreamHandler()
    ],
)

# ----------------------------
# Config
# ----------------------------
SUPPORTED_EXTS = {".pdf", ".docx", ".doc"}

IMG_LINK_MD = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')
IMG_LINK_HTML = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


@dataclass
class ConvertResult:
    ok: bool
    message: str
    out_md: Optional[Path] = None


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def run_cmd(cmd: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    """Run a command safely and capture stdout/stderr."""
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
    )
    return p.returncode, p.stdout, p.stderr


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def normalize_img_url(url: str) -> str:
    url = url.strip().strip("<>").strip().strip('"').strip("'")
    url = url.replace("%20", " ")
    return url


def extract_image_urls(md_text: str) -> List[str]:
    urls = []
    urls += [normalize_img_url(u) for u in IMG_LINK_MD.findall(md_text)]
    urls += [normalize_img_url(u) for u in IMG_LINK_HTML.findall(md_text)]
    # de-dup keep order
    seen = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def rewrite_and_copy_images(
    md_src_path: Path,
    md_text: str,
    assets_dir: Path,
    md_dest_dir: Path,
) -> Tuple[str, int]:
    """
    Copy images referenced in md_text (relative paths) into assets_dir and rewrite links
    to "./<assets_dir.name>/<newname>".
    """
    ensure_dir(assets_dir)

    urls = extract_image_urls(md_text)
    if not urls:
        return md_text, 0

    replaced = md_text
    copied = 0
    counter = 1

    # Helper: locate a referenced image file robustly
    def locate_image(url: str) -> Optional[Path]:
        if url.startswith(("http://", "https://", "data:")):
            return None
        cand = (md_src_path.parent / url)
        if cand.exists() and cand.is_file():
            return cand
        # fallback: search by basename under md_src_path.parent
        base = Path(url).name
        hits = list(md_src_path.parent.rglob(base))
        hits = [h for h in hits if h.is_file()]
        return hits[0] if hits else None

    for url in urls:
        src = locate_image(url)
        if not src:
            continue

        ext = src.suffix.lower()
        if ext not in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp", ".svg"]:
            # still copy unknown ext, but keep ext
            pass

        new_name = f"img_{counter:03d}{ext if ext else '.bin'}"
        dst = assets_dir / new_name

        # avoid overwriting within the same doc
        while dst.exists():
            counter += 1
            new_name = f"img_{counter:03d}{ext if ext else '.bin'}"
            dst = assets_dir / new_name

        try:
            shutil.copy2(src, dst)
            copied += 1

            # rewrite both markdown and html src occurrences
            new_rel = f"./{assets_dir.name}/{new_name}"
            # Replace exact url occurrences inside () and inside src=""
            replaced = replaced.replace(f"]({url})", f"]({new_rel})")
            replaced = re.sub(
                r'(<img[^>]+src=["\'])' + re.escape(url) + r'(["\'])',
                r"\1" + new_rel + r"\2",
                replaced,
                flags=re.IGNORECASE
            )
            counter += 1
        except Exception:
            continue

    return replaced, copied


# ----------------------------
# DOCX via Pandoc
# ----------------------------
def convert_docx_pandoc(in_file: Path, out_md: Path, overwrite: bool) -> ConvertResult:
    if out_md.exists() and not overwrite:
        return ConvertResult(True, "SKIP (exists)", out_md)

    if not which("pandoc"):
        return ConvertResult(False, "pandoc not found in PATH. Install pandoc to convert docx with images.", None)

    ensure_dir(out_md.parent)
    assets_name = out_md.stem + "_assets"

    # Run pandoc in out_md.parent so links stay relative
    cmd = [
        "pandoc",
        str(in_file),
        "-t", "gfm",
        "--wrap=none",
        f"--extract-media={assets_name}",
        "-o", str(out_md.name),
    ]
    code, stdout, stderr = run_cmd(cmd, cwd=out_md.parent)
    if code != 0:
        return ConvertResult(False, f"pandoc failed: {stderr.strip()}", None)

    return ConvertResult(True, "OK (pandoc)", out_md)


# ----------------------------
# DOC -> DOCX via LibreOffice
# ----------------------------
def convert_doc_to_docx_soffice(in_file: Path, tmp_dir: Path) -> Optional[Path]:
    soffice = which("soffice") or which("soffice.exe")
    if not soffice:
        return None

    ensure_dir(tmp_dir)

    # LibreOffice conversion: soffice --headless --convert-to docx --outdir <tmp_dir> <in_file>
    cmd = [soffice, "--headless", "--convert-to", "docx", "--outdir", str(tmp_dir), str(in_file)]
    code, stdout, stderr = run_cmd(cmd, cwd=tmp_dir)
    if code != 0:
        return None

    # Find converted docx
    cand = tmp_dir / (in_file.stem + ".docx")
    if cand.exists():
        return cand

    # sometimes LO changes name; fallback search
    hits = list(tmp_dir.glob("*.docx"))
    return hits[0] if hits else None


# ----------------------------
# PDF via Marker
# ----------------------------
def convert_pdf_marker(in_file: Path, out_md: Path, overwrite: bool) -> ConvertResult:
    if out_md.exists() and not overwrite:
        return ConvertResult(True, "SKIP (exists)", out_md)

    marker_single = which("marker_single")
    if not marker_single:
        return ConvertResult(False, "marker_single not found. Install Marker: pip install marker-pdf", None)

    ensure_dir(out_md.parent)
    assets_dir = out_md.parent / (out_md.stem + "_assets")
    tmp_dir = out_md.parent / (out_md.stem + "__marker_tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    ensure_dir(tmp_dir)

    # Marker outputs markdown + images in output_dir (we will rewrite into our assets_dir)
    # Command based on Marker docs :contentReference[oaicite:4]{index=4}
    cmd = [
        marker_single,
        str(in_file),
        "--output_dir", str(tmp_dir),
        "--output_format", "markdown",
    ]
    code, stdout, stderr = run_cmd(cmd, cwd=tmp_dir)
    if code != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return ConvertResult(False, f"marker_single failed: {stderr.strip()}", None)

    # Find produced md (largest .md under tmp)
    mds = list(tmp_dir.rglob("*.md"))
    if not mds:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return ConvertResult(False, "marker produced no .md output", None)

    md_src = max(mds, key=lambda p: p.stat().st_size)

    try:
        md_text = md_src.read_text(encoding="utf-8", errors="replace")
    except Exception:
        md_text = md_src.read_text(encoding="utf-8", errors="ignore")

    # Copy + rewrite images into "<stem>_assets"
    rewritten, copied = rewrite_and_copy_images(
        md_src_path=md_src,
        md_text=md_text,
        assets_dir=assets_dir,
        md_dest_dir=out_md.parent,
    )

    out_md.write_text(rewritten, encoding="utf-8")

    # Cleanup tmp
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return ConvertResult(True, f"OK (marker) | images_copied={copied}", out_md)


# ----------------------------
# PDF fallback via PyMuPDF
# ----------------------------
def convert_pdf_pymupdf(in_file: Path, out_md: Path, overwrite: bool) -> ConvertResult:
    if out_md.exists() and not overwrite:
        return ConvertResult(True, "SKIP (exists)", out_md)

    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ConvertResult(False, "PyMuPDF not installed. pip install pymupdf (fallback for PDFs)", None)

    ensure_dir(out_md.parent)
    assets_dir = out_md.parent / (out_md.stem + "_assets")
    ensure_dir(assets_dir)

    doc = fitz.open(str(in_file))
    lines = []
    img_count = 0

    lines.append(f"# {in_file.stem}\n")

    for i, page in enumerate(doc, start=1):
        lines.append(f"\n\n---\n\n## Page {i}\n")

        # Extract text (basic)
        text = page.get_text("text")
        if text:
            # preserve as-is; minimal cleanup
            lines.append(text.strip() + "\n")

        # Extract images (basic)
        img_list = page.get_images(full=True)
        for j, img in enumerate(img_list, start=1):
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n > 4:  # CMYK etc -> convert to RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_count += 1
                img_name = f"img_{i:03d}_{j:02d}.png"
                img_path = assets_dir / img_name
                pix.save(str(img_path))
                pix = None
                lines.append(f"\n![{img_name}](./{assets_dir.name}/{img_name})\n")
            except Exception:
                continue

    out_md.write_text("\n".join(lines), encoding="utf-8")
    doc.close()

    return ConvertResult(True, f"OK (pymupdf fallback) | images={img_count}", out_md)


# ----------------------------
# DOC/DOCX fallback: MarkItDown
# ----------------------------
def convert_with_markitdown(in_file: Path, out_md: Path, overwrite: bool) -> ConvertResult:
    if out_md.exists() and not overwrite:
        return ConvertResult(True, "SKIP (exists)", out_md)

    try:
        from markitdown import MarkItDown
    except ImportError:
        return ConvertResult(False, "MarkItDown not installed. pip install markitdown", None)

    ensure_dir(out_md.parent)
    md_converter = MarkItDown()

    try:
        res = md_converter.convert(str(in_file))
        text = (res.text_content or "").strip()
        if not text:
            return ConvertResult(False, "MarkItDown extracted empty content", None)
        out_md.write_text(text, encoding="utf-8")
        return ConvertResult(True, "OK (markitdown)", out_md)
    except Exception as e:
        return ConvertResult(False, f"MarkItDown error: {e}", None)


# ----------------------------
# Main dispatcher
# ----------------------------
def convert_one(
    file_path: Path,
    input_root: Path,
    output_root: Path,
    overwrite: bool,
    pdf_engine: str,
) -> ConvertResult:
    rel = file_path.relative_to(input_root)
    out_md = (output_root / rel).with_suffix(".md")

    ext = file_path.suffix.lower()

    # PDF
    if ext == ".pdf":
        if pdf_engine in ("auto", "marker"):
            r = convert_pdf_marker(file_path, out_md, overwrite)
            if r.ok:
                return r
            if pdf_engine == "marker":
                return r  # don't fallback if forced

        # fallback
        return convert_pdf_pymupdf(file_path, out_md, overwrite)

    # DOCX
    if ext == ".docx":
        r = convert_docx_pandoc(file_path, out_md, overwrite)
        if r.ok:
            return r
        # fallback markitdown
        r2 = convert_with_markitdown(file_path, out_md, overwrite)
        return r2

    # DOC
    if ext == ".doc":
        # Try LibreOffice -> docx -> pandoc
        tmp_dir = out_md.parent / (out_md.stem + "__tmp_doc_convert")
        docx = convert_doc_to_docx_soffice(file_path, tmp_dir)
        if docx and docx.exists():
            r = convert_docx_pandoc(docx, out_md, overwrite)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if r.ok:
                return r

        # fallback markitdown
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return convert_with_markitdown(file_path, out_md, overwrite)

    return ConvertResult(False, f"Unsupported extension: {ext}", None)


def main():
    parser = argparse.ArgumentParser(description="High-quality PDF/Word -> Markdown converter (recursive, with images).")
    parser.add_argument("--input_dir", required=True, help="Root directory to scan for files")
    parser.add_argument("--output_dir", required=True, help="Directory to save markdown files")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing markdown files")
    parser.add_argument("--pdf_engine", default="auto", choices=["auto", "marker", "pymupdf"],
                        help="PDF engine: auto=marker then fallback, marker=only marker, pymupdf=only fallback")

    args = parser.parse_args()

    input_root = Path(args.input_dir).resolve()
    output_root = Path(args.output_dir).resolve()

    if not input_root.exists():
        logging.error(f"Input directory does not exist: {input_root}")
        return

    ensure_dir(output_root)

    files = []
    for root, _, fs in os.walk(input_root):
        for f in fs:
            p = Path(root) / f
            if p.suffix.lower() in SUPPORTED_EXTS:
                files.append(p)

    logging.info(f"Found {len(files)} files under: {input_root}")

    ok = 0
    fail = 0
    skipped = 0

    for p in tqdm(files, desc="Converting"):
        try:
            r = convert_one(
                file_path=p,
                input_root=input_root,
                output_root=output_root,
                overwrite=args.overwrite,
                pdf_engine=args.pdf_engine,
            )

            if r.ok and r.message.startswith("SKIP"):
                skipped += 1
                logging.info(f"[SKIP] {p} -> {r.out_md} | {r.message}")
            elif r.ok:
                ok += 1
                logging.info(f"[OK]   {p} -> {r.out_md} | {r.message}")
            else:
                fail += 1
                logging.error(f"[ERR]  {p} | {r.message}")

        except Exception as e:
            fail += 1
            logging.exception(f"[FATAL] {p} | {e}")

    logging.info(f"Summary: OK={ok}, SKIP={skipped}, FAIL={fail}")
    logging.info(f"Output root: {output_root}")


if __name__ == "__main__":
    main()


