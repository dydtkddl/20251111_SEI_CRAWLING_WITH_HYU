#!/usr/bin/env python3
"""
expand_xyz_from_cell.py

셀 정보 파일(CIF, POSCAR 등)과 XYZ 궤적을 받아 슈퍼셀 XYZ를 생성합니다.

Usage:
    python expand_xyz_from_cell.py <cell_file> <in.xyz> <out.xyz> <nx> <ny> <nz>

예)
    python expand_xyz_from_cell.py POSCAR BAMOF-pos.xyz BAMOF-3x3x3.xyz 3 3 3
    python expand_xyz_from_cell.py structure.cif traj.xyz traj-2x2x2.xyz 2 2 2
"""

import sys
import os  # ← 추가
from ase.io import read, write

def expand_xyz(cell_file, input_xyz, output_xyz, nx, ny, nz):
    nx, ny, nz = map(int, (nx, ny, nz))

    unit = read(cell_file)
    cell = unit.get_cell()

    frames = read(input_xyz, index=':')

    expanded = []
    for atoms in frames:
        sc = atoms.copy()
        sc.set_cell(cell)
        sc.set_pbc([True, True, True])

        supercell = sc.repeat((nx, ny, nz))
        expanded.append(supercell)

    # 전체 trajectory 저장
    write(output_xyz, expanded)
    print(f"▶ Wrote {len(expanded)} frame(s) to {output_xyz}")

    # 마지막 프레임만 별도 저장
    final_frame = expanded[-1]
    final_filename = f"final_{nx}x{ny}x{nz}.xyz"  # ← 자동 생성 이름
    write(final_filename, final_frame)
    print(f"▶ Final frame saved as {final_filename}")

def main():
    if len(sys.argv) != 7:
        print(f"Usage: {sys.argv[0]} <cell_file> <in.xyz> <out.xyz> <nx> <ny> <nz>")
        sys.exit(1)
    expand_xyz(*sys.argv[1:])

if __name__ == "__main__":
    main()

