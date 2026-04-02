#!/usr/bin/env python3
import sys
import numpy as np
from ase.io import read

def read_unit_cell(cif_path):
    """
    CIF 파일을 읽어 단위 셀 벡터를 3x3 NumPy 배열로 반환합니다.
    """
    # ASE 로 CIF 읽기 (첫 번째 구조만)
    atoms = read(cif_path, format='cif')
    # 셀 벡터 가져오기 (3×3 array)
    cell = atoms.get_cell()  
    return np.array(cell)

def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <input.cif>")
        sys.exit(1)

    cif_file = sys.argv[1]
    cell_matrix = read_unit_cell(cif_file)
    print("Unit cell 3×3 matrix (Å):")
    print(cell_matrix)

if __name__ == "__main__":
    main()
