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
    Supports `Hexagonal a c` format:
      lattice Hexagonal a c
    Constructs cell vectors in Bohr, then converts to Angstrom.
    """
    cell_bohr = None
    pattern = re.compile(r"^\s*lattice\s+(\w+)\s+([0-9eE\+\-\.]+)\s+([0-9eE\+\-\.]+)")
    with open(file_path) as f:
        for line in f:
            m = pattern.match(line)
            if not m:
                continue
            lattype, p1, p2 = m.group(1), float(m.group(2)), float(m.group(3))
            if lattype.lower() == 'hexagonal':
                a, c = p1, p2
                v1 = [a, 0.0, 0.0]
                v2 = [-a/2, a * np.sqrt(3)/2, 0.0]
                v3 = [0.0, 0.0, c]
                cell_bohr = np.array([v1, v2, v3])
            else:
                raise NotImplementedError(f"Lattice type '{lattype}' not yet implemented.")
            break

    if cell_bohr is None:
        raise ValueError("Could not find a 'lattice' directive in the input file.")

    # Convert to Angstrom
    cell_ang = cell_bohr * BOHR2ANGSTROM
    return cell_ang


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
    coords = parse_ionpos(ionpos_file)

    # Build ASE Atoms:
    # - Convert fractional coords to Cartesian (Angstrom)
    symbols = [sym for sym, _ in coords]
    frac_positions = np.array([frac for _, frac in coords])
    cart_positions = frac_positions.dot(cell_ang)

    atoms = Atoms(symbols, positions=cart_positions, cell=cell_ang, pbc=True)

    # Write VASP POSCAR using DIRECT (= fractional) coordinates
    write(out_fname, atoms, format='vasp', direct=True)
    print(f"Wrote POSCAR to {out_fname}")


if __name__ == '__main__':
    main()
