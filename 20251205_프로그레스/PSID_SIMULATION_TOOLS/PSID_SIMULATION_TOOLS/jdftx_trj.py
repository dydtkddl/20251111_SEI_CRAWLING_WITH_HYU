#!/usr/bin/env python3
# jdftx_trj.py
# 2025-07-20 작성: ChatGPT (확장 버전: 마지막 프레임 저장 기능 추가)

import sys
import re
import pathlib
import math

# Bohr to Å conversion
BOHR_TO_ANG = 0.52917721092


def parse_lattice(input_file):
    """
    Reads a JDDFTx input file and returns the lattice basis vectors in Å.
    Supports: cubic, orthorhombic, hexagonal, triclinic.
    """
    with open(input_file, 'r') as f:
        for line in f:
            tokens = line.strip().split()
            if len(tokens) >= 2 and tokens[0].lower() == 'lattice':
                lat = tokens[1].lower()
                # Cubic: a
                if lat == 'cubic':
                    if len(tokens) < 3:
                        raise ValueError('Cubic lattice needs 1 parameter: a')
                    a_bohr = float(tokens[2])
                    a = a_bohr * BOHR_TO_ANG
                    return [[a, 0, 0], [0, a, 0], [0, 0, a]]
                # Orthorhombic: a, b, c
                elif lat == 'orthorhombic':
                    if len(tokens) < 5:
                        raise ValueError('Orthorhombic lattice needs 3 parameters: a b c')
                    a, b, c = [float(x) * BOHR_TO_ANG for x in tokens[2:5]]
                    return [[a, 0, 0], [0, b, 0], [0, 0, c]]
                # Hexagonal: a, c
                elif lat == 'hexagonal':
                    if len(tokens) < 4:
                        raise ValueError('Hexagonal lattice needs 2 parameters: a c')
                    a = float(tokens[2]) * BOHR_TO_ANG
                    c = float(tokens[3]) * BOHR_TO_ANG
                    sqrt3 = math.sqrt(3)
                    return [
                        [a, 0.0, 0.0],
                        [-0.5 * a, sqrt3 / 2 * a, 0.0],
                        [0.0, 0.0, c]
                    ]
                # Triclinic: a, b, c, α, β, γ
                elif lat == 'triclinic':
                    if len(tokens) < 8:
                        raise ValueError('Triclinic lattice needs 6 parameters: a b c α β γ')
                    a_bohr, b_bohr, c_bohr = map(float, tokens[2:5])
                    alpha = math.radians(float(tokens[5]))  # between b and c
                    beta = math.radians(float(tokens[6]))   # between a and c
                    gamma = math.radians(float(tokens[7]))  # between a and b
                    a = a_bohr * BOHR_TO_ANG
                    b = b_bohr * BOHR_TO_ANG
                    c = c_bohr * BOHR_TO_ANG
                    a1 = (a, 0.0, 0.0)
                    a2 = (b * math.cos(gamma), b * math.sin(gamma), 0.0)
                    cx = c * math.cos(beta)
                    cy = c * (math.cos(alpha) - math.cos(beta) * math.cos(gamma)) / math.sin(gamma)
                    cz2 = c * c - cx * cx - cy * cy
                    cz = math.sqrt(cz2) if cz2 > 0 else 0.0
                    a3 = (cx, cy, cz)
                    return [list(a1), list(a2), list(a3)]
                else:
                    raise ValueError(f'Unsupported lattice type: {tokens[1]}')
    raise ValueError('No lattice specification found in input file.')


def extract_frames(out_path, lattice):
    """
    Parses jdftx output for ionic positions (fractional coords) and converts to Cartesian (Å).
    Returns list of frames, each a list of (symbol, x, y, z).
    """
    pattern = re.compile(r"^\s*ion\s+(\w+)\s+([\-\d.eE]+)\s+([\-\d.eE]+)\s+([\-\d.eE]+)\s+\d+\s*$")
    lines = out_path.read_text().splitlines()
    frames = []
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
                    sym = m.group(1)
                    fx, fy, fz = map(float, m.group(2, 3, 4))
                    # fractional -> Cartesian
                    x = fx * lattice[0][0] + fy * lattice[1][0] + fz * lattice[2][0]
                    y = fx * lattice[0][1] + fy * lattice[1][1] + fz * lattice[2][1]
                    z = fx * lattice[0][2] + fy * lattice[1][2] + fz * lattice[2][2]
                    atoms.append((sym, x, y, z))
                i += 1
            if atoms:
                frames.append(atoms)
        else:
            i += 1
    return frames


def main():
    if len(sys.argv) != 3:
        print("Usage: python jdftx_trj.py <jdftx.out> <input.in>")
        sys.exit(1)

    out_file = pathlib.Path(sys.argv[1])
    in_file = pathlib.Path(sys.argv[2])
    if not out_file.is_file():
        print(f"JDDFTx output not found: {out_file}")
        sys.exit(1)
    if not in_file.is_file():
        print(f"Input file not found: {in_file}")
        sys.exit(1)

    try:
        lattice = parse_lattice(in_file)
    except Exception as e:
        print(f"Error parsing lattice: {e}")
        sys.exit(1)

    frames = extract_frames(out_file, lattice)
    if not frames:
        print("No ionic-position blocks found.")
        sys.exit(0)

    # Write all frames to trj.xyz
    xyz_path = out_file.with_name("trj.xyz")
    with xyz_path.open("w") as xyz:
        for step, atoms in enumerate(frames, 1):
            xyz.write(f"{len(atoms)}\n")
            xyz.write(f"Frame {step}\n")
            for sym, x, y, z in atoms:
                xyz.write(f"{sym:<2} {x:.6f} {y:.6f} {z:.6f}\n")

    print(f"Wrote {len(frames)} frame(s) to {xyz_path}")

    # Write only the final frame to final.xyz
    final_path = out_file.with_name("final.xyz")
    last_atoms = frames[-1]
    with final_path.open("w") as final_file:
        final_file.write(f"{len(last_atoms)}\n")
        final_file.write("Final frame\n")
        for sym, x, y, z in last_atoms:
            final_file.write(f"{sym:<2} {x:.6f} {y:.6f} {z:.6f}\n")

    print(f"Wrote final frame to {final_path}")


if __name__ == '__main__':
    main()
