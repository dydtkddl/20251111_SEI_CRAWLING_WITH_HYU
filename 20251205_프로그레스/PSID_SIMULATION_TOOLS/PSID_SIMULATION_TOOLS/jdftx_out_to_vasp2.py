#!/usr/bin/env python3
"""
jdftx_out_to_vasp.py

Parse a JDFTx input (`input.in`) and an `ionpos` file of fractional coordinates
and generate a VASP POSCAR with Angstrom units.

Usage:
    python jdftx_out_to_vasp.py path/to/input.in path/to/test.ionpos [output.vasp]
If `output.vasp` is not provided, defaults to `<input_basename>_POSCAR`.
"""

import sys
import re
import os
import numpy as np
from ase import Atoms
from ase.io import write

# Conversion factor from Bohr to Angstrom
BOHR2ANGSTROM = 0.529177210903

def parse_jdftx_input(file_path):
    """
    Parse the `lattice` directive from a JDFTx input file (in Bohr) and convert to Angstrom.
    Supports:
      - lattice Hexagonal a c
      - lattice Cubic a
      - lattice Tetragonal a c
      - lattice Orthorhombic a b c
      - lattice inline 9 floats → 3×3 matrix
      - lattice Triclinic a b c α β γ  (6-parameter)
      - lattice (or lattice Triclinic without params) + 3 raw-vector lines
    """
    lines = open(file_path).read().splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line.lower().startswith('lattice'):
            continue

        tokens = line.split()
        # 1) inline 9 floats: "lattice  a1 a2 a3  b1 b2 b3  c1 c2 c3"
        if len(tokens) == 10 and all(re.match(r'^-?\d+(\.\d+)?$', t) for t in tokens[1:]):
            vals = list(map(float, tokens[1:]))
            v1, v2, v3 = vals[0:3], vals[3:6], vals[6:9]

        # 2) Triclinic 6-parameter: a, b, c, α, β, γ (degrees)
        elif tokens[1].lower() == 'triclinic' and len(tokens) == 8:
            a, b, c = map(float, tokens[2:5])
            α, β, γ = np.deg2rad(list(map(float, tokens[5:8])))
            # compute cell vectors in Bohr
            v1 = [ a, 0.0, 0.0 ]
            v2 = [ b*np.cos(γ), b*np.sin(γ), 0.0 ]
            # for v3:
            cx = c*np.cos(β)
            cy = c*(np.cos(α) - np.cos(β)*np.cos(γ)) / np.sin(γ)
            cz = c*np.sqrt(1 - np.cos(β)**2 - ((np.cos(α) - np.cos(β)*np.cos(γ)) / np.sin(γ))**2)
            v3 = [ cx, cy, cz ]

        # 3) Named Bravais lattices:
        elif len(tokens) >= 2:
            lattype = tokens[1].lower()
            if lattype == 'hexagonal' and len(tokens) == 4:
                a, c = float(tokens[2]), float(tokens[3])
                v1 = [ a,           0.0,        0.0 ]
                v2 = [ -a/2,  a*np.sqrt(3)/2,   0.0 ]
                v3 = [ 0.0,          0.0,        c   ]
            elif lattype == 'cubic' and len(tokens) == 3:
                a = float(tokens[2])
                v1 = [a, 0.0, 0.0]
                v2 = [0.0, a, 0.0]
                v3 = [0.0, 0.0, a]
            elif lattype == 'tetragonal' and len(tokens) == 4:
                a, c = float(tokens[2]), float(tokens[3])
                v1 = [a,    0.0, 0.0]
                v2 = [0.0,  a,   0.0]
                v3 = [0.0,  0.0, c  ]
            elif lattype == 'orthorhombic' and len(tokens) == 5:
                a, b, c = float(tokens[2]), float(tokens[3]), float(tokens[4])
                v1 = [a,   0.0, 0.0]
                v2 = [0.0, b,   0.0]
                v3 = [0.0, 0.0, c  ]
            else:
                # 4) “bare” lattice or fallback Triclinic: scan 다음 줄들 중 3×3 숫자 블록
                vals = []
                j = i + 1
                while len(vals) < 3 and j < len(lines):
                    parts = lines[j].strip().split()
                    if len(parts) == 3:
                        try:
                            vec = list(map(float, parts))
                            vals.append(vec)
                        except ValueError:
                            pass
                    j += 1
                if len(vals) != 3:
                    raise ValueError("Failed to parse 3 raw lattice vectors after 'lattice'")
                v1, v2, v3 = vals

        else:
            raise NotImplementedError(f"Unsupported lattice directive: '{line}'")

        cell_bohr = np.array([v1, v2, v3])
        return cell_bohr * BOHR2ANGSTROM

    raise ValueError("Could not find a 'lattice' directive in the input file.")

def parse_ionpos(file_path):
    """
    Parse fractional coordinates from a JDFTx `ionpos` file.
    Expects lines of the form:
      ion SYMBOL frac_x frac_y frac_z [other]
    Returns list of (symbol, fractional_coordinates).
    """
    coords = []
    with open(file_path) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 5 and parts[0].lower() == 'ion':
                symbol = parts[1]
                frac = np.array([float(parts[2]), float(parts[3]), float(parts[4])])
                coords.append((symbol, frac))
    if not coords:
        raise ValueError(f"No valid 'ion' entries found in {file_path}.")
    return coords

def main():
    if not (3 <= len(sys.argv) <= 4):
        print("Usage: python jdftx_out_to_vasp.py input.in test.ionpos [output.vasp]", file=sys.stderr)
        sys.exit(1)

    input_file = sys.argv[1]
    ionpos_file = sys.argv[2]
    out_fname = sys.argv[3] if len(sys.argv) == 4 else os.path.splitext(os.path.basename(input_file))[0] + '_POSCAR'

    # Parse cell (in Angstrom) and fractional coordinates
    cell_ang = parse_jdftx_input(input_file)
    coords   = parse_ionpos(ionpos_file)

    # Build ASE Atoms
    symbols        = [sym for sym, _ in coords]
    frac_positions = np.array([frac for _, frac in coords])
    cart_positions = frac_positions.dot(cell_ang)

    atoms = Atoms(symbols, positions=cart_positions, cell=cell_ang, pbc=True)

    # Write VASP POSCAR using DIRECT (fractional) coordinates
    write(out_fname, atoms, format='vasp', direct=True)
    print(f"Wrote POSCAR to {out_fname}")

if __name__ == '__main__':
    main()

