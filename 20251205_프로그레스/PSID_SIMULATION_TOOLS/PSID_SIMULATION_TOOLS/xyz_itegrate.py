#!/usr/bin/env python3
"""
concat_xyz.py
-------------
쉘에서 전달된 XYZ 파일들을 순서대로 읽어
단일 XYZ trajectory 파일(여러 프레임)로 결합한다.

• 입력  : 여러 XYZ 파일 (쉘 와일드카드 *.xyz 등)
• 출력  : integrated_trj.xyz  (기본)  또는 -o/--output 지정
"""

import argparse
from pathlib import Path
import sys

def main():
    # ---------------- 인자 파싱 ----------------
    parser = argparse.ArgumentParser(
        description="Concatenate multiple XYZ files into a single trajectory.")
    parser.add_argument(
        "xyz_files", nargs="+",
        help="Input XYZ files (use shell wildcards, e.g. *.xyz)")
    parser.add_argument(
        "-o", "--output", default="integrated_trj.xyz",
        help="Output XYZ trajectory filename (default: integrated_trj.xyz)")
    args = parser.parse_args()

    out_path = Path(args.output).resolve()

    # ---------------- 파일 결합 ----------------
    with out_path.open("w") as fout:
        for xyz in args.xyz_files:
            path = Path(xyz)
            if not path.is_file():
                print(f"[WARN] '{xyz}' is not a file → 건너뜀", file=sys.stderr)
                continue

            with path.open() as fin:
                # 한 프레임 전체를 그대로 복사
                fout.write(fin.read().rstrip() + "\n")

    print(f"[OK] {len(args.xyz_files)} files → {out_path.name}")

if __name__ == "__main__":
    main()

