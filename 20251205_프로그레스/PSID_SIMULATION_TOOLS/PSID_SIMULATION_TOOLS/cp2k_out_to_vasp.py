#!/usr/bin/env python3
"""
cp2k_out_to_poscar.py

Parse a CP2K restart/output file for CELL_OPT and geometry, and generate a VASP POSCAR.
Usage:
    python cp2k_out_to_poscar.py path/to/restart_file [output.vasp]
If output filename is not provided, defaults to <restart_basename>.vasp
"""

import sys
import re
import os
import numpy as np
from ase import Atoms
from ase.io import write

def parse_cp2k_restart(file_path):
    cell = []
    coords = []
    kinds = {}
    current_kind = None
    reading_cell = False
    reading_coord = False

    with open(file_path, 'r') as f:
        for line in f:
            # CELL block
            if re.match(r'\s*&CELL', line):
                reading_cell = True
                continue
            if reading_cell:
                if re.match(r'\s*&END\s+CELL', line):
                    reading_cell = False
                else:
                    parts = line.split()
                    # A, B, C lines contain the three cell vectors
                    if len(parts) >= 4 and parts[0] in ('A', 'B', 'C'):
                        cell.append([float(parts[1]), float(parts[2]), float(parts[3])])
                continue

            # COORD block
            if re.match(r'\s*&COORD', line):
                reading_coord = True
                continue
            if reading_coord:
                if re.match(r'\s*&END\s+COORD', line):
                    reading_coord = False
                else:
                    parts = line.split()
                    if len(parts) >= 4:
                        kind = parts[0]
                        pos = np.array([float(parts[1]), float(parts[2]), float(parts[3])])
                        coords.append((kind, pos))
                continue

            # KIND → ELEMENT mapping
            m = re.match(r'\s*&KIND\s+"?([\w]+)"?', line)
            if m:
                current_kind = m.group(1)
                continue
            m2 = re.match(r'\s*ELEMENT\s+"?([\w]+)"?', line)
            if m2 and current_kind:
                kinds[current_kind] = m2.group(1)
                current_kind = None

    return np.array(cell), coords, kinds

def main():
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: python cp2k_out_to_poscar.py path/to/restart_file [output.vasp]", file=sys.stderr)
        sys.exit(1)

    restart_file = sys.argv[1]
    if not os.path.isfile(restart_file):
        print(f"Error: file not found: {restart_file}", file=sys.stderr)
        sys.exit(1)

    # Determine output filename
    if len(sys.argv) == 3:
        out_fname = sys.argv[2]
    else:
        base = os.path.splitext(os.path.basename(restart_file))[0]
        out_fname = base + ".vasp"

    # Parse cell vectors, coordinates, and kinds
    cell, coords, kinds = parse_cp2k_restart(restart_file)

    # Build ASE Atoms object
    symbols   = [ kinds.get(kind, kind) for kind, _ in coords ]
    positions = [ pos for _, pos in coords ]
    atoms = Atoms(
        symbols   = symbols,
        positions = positions,
        cell      = cell,
        pbc       = True
    )

    # Write VASP POSCAR (DIRECT coordinates)
# direct True면 fractional
#    write(out_fname, atoms, format='vasp', direct=True)
# direct false면 cartesian
    write(out_fname, atoms, format='vasp', direct=False)
    print(f"Wrote POSCAR to {out_fname}")

if __name__ == "__main__":
    main()

