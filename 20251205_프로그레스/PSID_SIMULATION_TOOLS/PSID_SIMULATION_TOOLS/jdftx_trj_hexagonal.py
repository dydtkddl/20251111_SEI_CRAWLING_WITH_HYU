#!/usr/bin/env python3
# extract_trj_hex.py
# Hexagonal lattice 전용 trajectory 추출기
import sys, re, pathlib

if len(sys.argv) != 4:
    print("Usage: python extract_trj_hex.py <jdftx.out> <a_bohr> <c_bohr>")
    sys.exit(1)

out_path = pathlib.Path(sys.argv[1])
if not out_path.is_file():
    print(f"File not found: {out_path}")
    sys.exit(1)

a_bohr = float(sys.argv[2])
c_bohr = float(sys.argv[3])
BOHR_TO_ANG = 0.52917721092

a = a_bohr * BOHR_TO_ANG
c = c_bohr * BOHR_TO_ANG

# Hexagonal lattice basis vectors (in Å)
# a1 = a * (1, 0, 0)
# a2 = a * (-1/2, sqrt(3)/2, 0)
# a3 = c * (0, 0, 1)

import math
sqrt3 = math.sqrt(3)
lattice = [
    [a,         0,        0],                   # a1
    [-0.5*a,  sqrt3/2*a,  0],                   # a2
    [0,         0,        c],                   # a3
]

# Regex pattern for ion lines
pattern = re.compile(r"^\s*ion\s+(\w+)\s+([-\d.eE]+)\s+([-\d.eE]+)\s+([-\d.eE]+)\s+\d+\s*$")

frames = []

with out_path.open() as fh:
    lines = fh.readlines()

i = 0
while i < len(lines):
    if lines[i].lstrip().startswith("# Ionic positions in"):
        i += 1
        atoms = []
        while i < len(lines):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#"):
                break
            m = pattern.match(line)
            if m:
                sym, fx, fy, fz = m.group(1), float(m.group(2)), float(m.group(3)), float(m.group(4))
                # Convert fractional to Cartesian (Å)
                x = fx * lattice[0][0] + fy * lattice[1][0] + fz * lattice[2][0]
                y = fx * lattice[0][1] + fy * lattice[1][1] + fz * lattice[2][1]
                z = fx * lattice[0][2] + fy * lattice[1][2] + fz * lattice[2][2]
                atoms.append((sym, x, y, z))
            i += 1
        if atoms:
            frames.append(atoms)
    else:
        i += 1

if not frames:
    print("No ionic-position blocks found.")
    sys.exit(0)

xyz_path = out_path.with_name("trj_hex.xyz")
with xyz_path.open("w") as xyz:
    for step, atoms in enumerate(frames, 1):
        xyz.write(f"{len(atoms)}\n")
        xyz.write(f"Frame {step}\n")
        for sym, x, y, z in atoms:
            xyz.write(f"{sym:<2} {x:.6f} {y:.6f} {z:.6f}\n")

print(f"Wrote {len(frames)} frame(s) to {xyz_path}")

