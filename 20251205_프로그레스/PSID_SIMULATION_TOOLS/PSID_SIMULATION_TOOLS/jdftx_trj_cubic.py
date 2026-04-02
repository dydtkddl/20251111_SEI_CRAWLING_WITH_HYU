#!/usr/bin/env python3
# extract_trj.py
# 2025-07-14  작성: ChatGPT (수정판)

import sys, re, pathlib

# usage check
if len(sys.argv) not in (2, 3):
    print("Usage: python extract_trj.py <jdftx.out> [cell_param_bohr]")
    sys.exit(1)

out_path = pathlib.Path(sys.argv[1])
if not out_path.is_file():
    print(f"File not found: {out_path}")
    sys.exit(1)

# 셀 파라미터 (Bohr) 및 변환 계수 설정
cell_bohr = float(sys.argv[2]) if len(sys.argv) == 3 else 1.0
BOHR_TO_ANG = 0.52917721092
scale = cell_bohr * BOHR_TO_ANG  # 최종 스케일 팩터

frames = []  # list of list[tuple(symbol,x,y,z)]
pattern = re.compile(r"^\s*ion\s+(\w+)\s+([-\d.eE]+)\s+([-\d.eE]+)\s+([-\d.eE]+)\s+\d+\s*$")

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
                atoms.append((m.group(1), m.group(2), m.group(3), m.group(4)))
            i += 1
        if atoms:
            frames.append(atoms)
    else:
        i += 1

if not frames:
    print("No ionic-position blocks found.")
    sys.exit(0)

xyz_path = out_path.with_name("trj.xyz")
with xyz_path.open("w") as xyz:
    for step, atoms in enumerate(frames, 1):
        xyz.write(f"{len(atoms)}\n")
        xyz.write(f"Frame {step}\n")
        for sym, x, y, z in atoms:
            # 문자열을 float으로 변환한 뒤 스케일 적용
            x_ang = float(x) * scale
            y_ang = float(y) * scale
            z_ang = float(z) * scale
            xyz.write(f"{sym:<2} {x_ang:.6f} {y_ang:.6f} {z_ang:.6f}\n")

print(f"Wrote {len(frames)} frame(s) to {xyz_path}")

