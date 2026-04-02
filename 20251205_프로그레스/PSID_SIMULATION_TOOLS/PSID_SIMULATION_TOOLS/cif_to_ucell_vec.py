#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cif2cellvec.py
  usage: python cif2cellvec.py <input.cif> [out.txt]

  · CIF의 _cell_length_*, _cell_angle_* 를 읽어
    3×3 직교 좌표계 셀 행렬(Å)을 계산한 뒤 텍스트로 저장.
"""

import sys, numpy as np
from ase.io import read

def cell_matrix_from_lengths_angles(a, b, c, alpha, beta, gamma):
    """각도(°)를 받아 3×3 셀 행렬(Å) 반환"""
    α, β, γ = np.deg2rad([alpha, beta, gamma])
    v_x = a
    v_y = b * np.cos(γ)
    v_z = c * np.cos(β)
    v_y2 = b * np.sin(γ)
    v_z2 = c * (np.cos(α) - np.cos(β) * np.cos(γ)) / np.sin(γ)
    v_z3 = c * np.sqrt(1 - np.cos(β)**2 - v_z2**2 / c**2)
    return np.array([[v_x,      0,      0],
                     [v_y,   v_y2,      0],
                     [v_z,   v_z2,  v_z3]])

def main():
    if len(sys.argv) not in (2, 3):
        print("usage: python cif2cellvec.py <input.cif> [out.txt]")
        sys.exit(1)

    cif_path   = sys.argv[1]
    out_path   = sys.argv[2] if len(sys.argv) == 3 else "cell_vectors.txt"
    cell       = read(cif_path).get_cell()          # ASE가 자동 파싱
    a, b, c    = cell.lengths()
    α, β, γ    = cell.angles()
    mat        = cell_matrix_from_lengths_angles(a, b, c, α, β, γ)

    with open(out_path, 'w') as f:
        f.write("# CP2K cell vectors (Å)\n")
        for row in mat:
            f.write("  {:12.6f} {:12.6f} {:12.6f}\n".format(*row))
    print(f"[✓] 3×3 셀 행렬 저장 → {out_path}")

if __name__ == "__main__":
    main()

