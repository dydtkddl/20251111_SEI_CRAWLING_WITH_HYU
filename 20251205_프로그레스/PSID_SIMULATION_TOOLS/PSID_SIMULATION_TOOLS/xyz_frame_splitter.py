
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xyz_frame_splitter.py

Split huge XYZ trajectory files (e.g., xtb.trj) into smaller files
by a fixed number of frames per chunk, with robust parsing.
- Handles missing trailing newline, extra blank lines.
- Verifies natoms-per-frame, counts, and reports malformed frames (optional skip).
- Logging + tqdm progress.
- Supports slicing (start/end/stride), gzip output (.gz), and dry-run.
- Produces an index JSON mapping chunk files to frame ranges.

Usage examples
--------------
# Split every 100 frames into one file
python xyz_frame_splitter.py xtb.trj --outdir ./split --frames-per-file 100 --prefix traj_part_

# Only frames 1..5000, every 2nd frame (stride=2), gzip outputs
python xyz_frame_splitter.py xtb.trj -o ./split -n 200 --start 1 --end 5000 --stride 2 --gzip

# Dry-run (no write) to just count/validate frames
python xyz_frame_splitter.py xtb.trj --dry-run --log-level INFO
"""
import sys
import os
import gzip
from pathlib import Path
from typing import Iterator, Tuple, Optional
import argparse
import logging
from tqdm import tqdm
import json

def setup_logger(level: str) -> logging.Logger:
    logger = logging.getLogger("xyz_splitter")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Clear existing handlers
    if logger.handlers:
        for h in list(logger.handlers):
            logger.removeHandler(h)
    ch = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger

def open_maybe_gzip(path: Path, mode: str):
    if "b" not in mode:
        mode += "t"
    if str(path).endswith(".gz"):
        return gzip.open(path, mode, encoding="utf-8", newline="")
    else:
        return open(path, mode, encoding="utf-8", newline="")

def iter_xyz_frames(xyz_path: Path, logger: logging.Logger) -> Iterator[Tuple[int, str]]:
    """
    Yield (natoms, block_text) for each XYZ frame in a robust manner.
    Accepts blank lines between frames and missing trailing newline.
    """
    with open_maybe_gzip(xyz_path, "r") as f:
        line_no = 0
        while True:
            # skip leading blanks
            line = f.readline()
            line_no += 1
            while line and not line.strip():
                line = f.readline()
                line_no += 1
            if not line:
                break  # EOF
            # natoms
            try:
                natoms = int(line.strip())
            except Exception:
                logger.debug(f"Non-integer line encountered at {xyz_path}:{line_no}: {line.strip()!r}")
                # try to continue scanning for next potential frame
                continue
            # comment
            comment = f.readline()
            line_no += 1
            if comment == "":
                logger.warning(f"Incomplete frame (missing comment) near line {line_no} - stopping.")
                break
            atoms = []
            for i in range(natoms):
                atom_line = f.readline()
                line_no += 1
                if atom_line == "":
                    logger.warning(f"Incomplete frame (expected {natoms} atoms, got {i}) near line {line_no} - stopping.")
                    return
                atoms.append(atom_line.rstrip("\n"))
            # Assemble block (ensure trailing newline)
            block = "\n".join([str(natoms), comment.rstrip("\n")] + atoms) + "\n"
            yield (natoms, block)

def write_chunk(out_path: Path, frames: Iterator[Tuple[int, str]], nframes: int, gzip_out: bool) -> int:
    count = 0
    mode = "wt"
    if gzip_out and not out_path.suffix == ".gz":
        out_path = out_path.with_suffix(out_path.suffix + ".gz")
    with open_maybe_gzip(out_path, "w") as out:
        for _ in range(nframes):
            try:
                nat, block = next(frames)
            except StopIteration:
                break
            out.write(block)
            count += 1
    return count

def main():
    p = argparse.ArgumentParser(description="Split large XYZ trajectory into smaller files by frame count.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("input", type=Path, help="Input XYZ trajectory (supports .gz)")
    p.add_argument("-o", "--outdir", type=Path, default=Path("split_xyz"), help="Output directory")
    p.add_argument("-n", "--frames-per-file", type=int, default=100, help="Frames per output file")
    p.add_argument("--start", type=int, default=1, help="First frame index (1-based, inclusive)")
    p.add_argument("--end", type=int, default=None, help="Last frame index (1-based, inclusive)")
    p.add_argument("--stride", type=int, default=1, help="Take every k-th frame")
    p.add_argument("--prefix", type=str, default="part_", help="Output file prefix")
    p.add_argument("--digits", type=int, default=4, help="Zero-padding for part indices")
    p.add_argument("--gzip", action="store_true", help="Gzip-compress output chunks (.gz)")
    p.add_argument("--dry-run", action="store_true", help="Do not write files; just scan/count/validate")
    p.add_argument("--log-level", type=str, default="INFO", help="Logging level (DEBUG, INFO, WARNING)")
    args = p.parse_args()

    logger = setup_logger(args.log_level)
    args.outdir.mkdir(parents=True, exist_ok=True)

    # Scan and optionally slice frames
    logger.info(f"Scanning frames in: {args.input}")
    all_frames = []
    total = 0
    for idx, (nat, block) in enumerate(iter_xyz_frames(args.input, logger), start=1):
        total += 1
        # slicing logic
        if idx < args.start:
            continue
        if args.end is not None and idx > args.end:
            break
        if (idx - args.start) % args.stride != 0:
            continue
        all_frames.append(block)

    sel = len(all_frames)
    logger.info(f"Total frames detected: {total}")
    logger.info(f"Selected frames (after slicing): {sel}")
    if args.dry_run or sel == 0:

    #if args.dry-run or sel == 0:
        logger.info("Dry-run or no frames selected. Exiting.")
        return 0

    # Create chunk files
    part = 0
    written = 0
    index = []  # for JSON index
    it = iter(all_frames)
    with tqdm(total=sel, desc="Writing chunks", unit="frm") as bar:
        while True:
            part += 1
            name = f"{args.prefix}{str(part).zfill(args.digits)}.xyz"
            out_path = args.outdir / name
            if args.gzip and not str(out_path).endswith(".gz"):
                out_path = out_path.with_suffix(out_path.suffix + ".gz")
            # write up to n frames
            count = 0
            with open_maybe_gzip(out_path, "w") as out:
                for _ in range(args.frames_per_file):
                    try:
                        block = next(it)
                    except StopIteration:
                        break
                    out.write(block)
                    count += 1
                    written += 1
                    bar.update(1)
            if count == 0:
                # remove empty file if created
                try:
                    out_path.unlink(missing_ok=True)
                except TypeError:
                    # Py<3.8 compat
                    if out_path.exists():
                        out_path.unlink()
                part -= 1
                break
            # record index
            start_frame = (part-1)*args.frames_per_file + 1
            end_frame = start_frame + count - 1
            index.append({
                "file": str(out_path),
                "frames_in_file": count,
                "selected_frame_range": [start_frame, end_frame]
            })
    # Write index json
    idx_path = args.outdir / "split_index.json"
    with open(idx_path, "w", encoding="utf-8") as jf:
        json.dump({
            "input": str(args.input),
            "total_frames": total,
            "selected_frames": sel,
            "frames_per_file": args.frames_per_file,
            "start": args.start,
            "end": args.end,
            "stride": args.stride,
            "gzip": args.gzip,
            "parts": index
        }, jf, indent=2, ensure_ascii=False)

    logger.info(f"Done. Wrote {part} part files to {args.outdir}")
    return 0

if __name__ == "__main__":
    sys.exit(main())

