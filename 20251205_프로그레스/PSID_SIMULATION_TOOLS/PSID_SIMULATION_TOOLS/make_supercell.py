#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_periodic_supercell.py

CIF 파일을 a×b×c 만큼 복제(슈퍼셀 생성)하고,
PBC 플래그와 셀 정보를 포함한 주기적 경계조건 CIF를 출력합니다.

Usage:
    python make_periodic_supercell.py input.cif a b c [output.cif]

Arguments:
    input.cif       입력 CIF 파일 경로
    a, b, c         a-, b-, c-축 방향 복제 횟수 (정수)
    output.cif      (선택) 출력 CIF 파일명. 지정 없으면
                    <basename>_<a>x<b>x<c>.cif 으로 생성
"""
import sys, os
import numpy as np
from ase.io import read, write

def make_periodic_supercell(infile: str, a: int, b: int, c: int, outfile: str = None):
    # — 입력 파일 체크 —
    if not os.path.isfile(infile):
        sys.stderr.write(f"Error: 파일을 찾을 수 없습니다: {infile}\n")
        sys.exit(1)

    # — CIF 읽기 —
    atoms = read(infile)

    # — 슈퍼셀 복제 —
    supercell = atoms.repeat((a, b, c))

    # — PBC 활성화 —
    supercell.set_pbc([True, True, True])

    # — fractional 좌표로 변환 및 래핑 (0 ≤ frac < 1) —
    frac = supercell.get_scaled_positions()  # 분수 좌표
    frac_wrapped = frac % 1.0                # 경계 밖 좌표를 0–1 범위로 wrap
    supercell.set_scaled_positions(frac_wrapped)

    # — 출력 파일명 결정 —
    if outfile is None:
        base = os.path.splitext(os.path.basename(infile))[0]
        outfile = f"{base}_{a}x{b}x{c}.cif"

    # — CIF 쓰기 —
    write(outfile, supercell, format='cif')
    print(f"주기적 슈퍼셀 CIF 생성 완료: {outfile}")
    print(f" - 원자 수: {len(supercell)}")
    print(f" - 셀 벡터:\n{supercell.get_cell()}")

def main():
    if len(sys.argv) not in (5, 6):
        sys.stderr.write("Usage: python make_periodic_supercell.py input.cif a b c [output.cif]\n")
        sys.exit(1)

    infile = sys.argv[1]
    try:
        a, b, c = map(int, sys.argv[2:5])
    except ValueError:
        sys.stderr.write("Error: a, b, c는 모두 정수여야 합니다.\n")
        sys.exit(1)

    outfile = sys.argv[5] if len(sys.argv) == 6 else None
    make_periodic_supercell(infile, a, b, c, outfile)

if __name__ == "__main__":
    main()
