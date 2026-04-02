#!/usr/bin/env python3
"""
cp2k_out_to_cif.py

Parse a CP2K restart/output file for CELL_OPT and geometry, and generate a CIF file.
Usage:
    python cp2k_out_to_cif.py path/to/restart_file [output.cif]
If output filename is not provided, defaults to <restart_basename>.cif
"""

import sys
import re
import os
import numpy as np

def parse_cp2k_restart(file_path):
    cell = []
    coords = []
    kinds = {}
    current_kind = None
    reading_cell = False
    reading_coord = False
    with open(file_path, 'r') as f:
        for line in f:
            if re.match(r'\s*&CELL', line):
                reading_cell = True
                continue
            if reading_cell:
                if re.match(r'\s*&END\s+CELL', line):
                    reading_cell = False
                else:
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] in ('A','B','C'):
                        cell.append([float(parts[1]), float(parts[2]), float(parts[3])])

                continue
	    
            if re.match(r'\s*&COORD', line):
                reading_coord = True
                continue
            if reading_coord:
                if re.match(r'\s*&END\s+COORD', line):
                    reading_coord = False
                else:
                    parts = line.split()
                    if len(parts) >= 4:
                        atom = parts[0]
                        pos = np.array([float(parts[1]), float(parts[2]), float(parts[3])])
                        coords.append((atom, pos))
                continue
            m = re.match(r'\s*&KIND\s+"?([\w]+)"?', line)
            if m:
                current_kind = m.group(1)
                continue
            m2 = re.match(r'\s*ELEMENT\s+"?([\w]+)"?', line)
            if m2 and current_kind:
                kinds[current_kind] = m2.group(1)
                current_kind = None
    return np.array(cell), coords, kinds

def write_cif(cell, coords, kinds, out):
    inv_cell = np.linalg.inv(cell.T)
    a, b, c = cell
    out.write("data_generated_by_cp2k_out_to_cif\n")
    out.write(f"_cell_length_a    {np.linalg.norm(a):.6f}\n")
    out.write(f"_cell_length_b    {np.linalg.norm(b):.6f}\n")
    out.write(f"_cell_length_c    {np.linalg.norm(c):.6f}\n")
    alpha = np.degrees(np.arccos(np.dot(b,c)/np.linalg.norm(b)/np.linalg.norm(c)))
    beta  = np.degrees(np.arccos(np.dot(a,c)/np.linalg.norm(a)/np.linalg.norm(c)))
    gamma = np.degrees(np.arccos(np.dot(a,b)/np.linalg.norm(a)/np.linalg.norm(b)))
    out.write(f"_cell_angle_alpha {alpha:.6f}\n")
    out.write(f"_cell_angle_beta  {beta:.6f}\n")
    out.write(f"_cell_angle_gamma {gamma:.6f}\n\n")
    out.write("loop_\n")
    out.write("  _atom_site_label\n")
    out.write("  _atom_site_type_symbol\n")
    out.write("  _atom_site_fract_x\n")
    out.write("  _atom_site_fract_y\n")
    out.write("  _atom_site_fract_z\n")
    for i,(kind,pos) in enumerate(coords, start=1):
        fract = inv_cell.dot(pos)
        element = kinds.get(kind, kind)
        out.write(f"  {kind}{i:03d} {element} {fract[0]:.6f} {fract[1]:.6f} {fract[2]:.6f}\n")

def main():
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: python cp2k_out_to_cif.py path/to/restart_file [output.cif]", file=sys.stderr)
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
        out_fname = base + ".cif"
    # Parse data
    cell, coords, kinds = parse_cp2k_restart(restart_file)
    print(cell, coords, kinds)
    # Write CIF
    with open(out_fname, 'w') as out:
        write_cif(cell, coords, kinds, out)
    print(f"Wrote CIF to {out_fname}")
    
if __name__ == "__main__":
    main()

