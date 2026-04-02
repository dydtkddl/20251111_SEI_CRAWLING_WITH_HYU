#!/usr/bin/env python3
# jdftx_trj_triclinic.py
# Triclinic lattice 전용 trajectory 추출기
import sys, re, pathlib, math

if len(sys.argv) != 8:
    print("Usage: python jdftx_trj_triclinic.py <jdftx.out> <a_bohr> <b_bohr> <c_bohr> <alpha_deg> <beta_deg> <gamma_deg>")
    sys.exit(1)

out_path = pathlib.Path(sys.argv[1])
if not out_path.is_file():
    print(f"File not found: {out_path}")
    sys.exit(1)

# Bohr to Å 변환 상수
BOHR_TO_ANG = 0.52917721092

# 입력값
a_bohr, b_bohr, c_bohr = map(float, sys.argv[2:5])
alpha = math.radians(float(sys.argv[5]))  # between b and c
beta  = math.radians(float(sys.argv[6]))  # between a and c
gamma = math.radians(float(sys.argv[7]))  # between a and b

# 격자 상수 (Å)
a = a_bohr * BOHR_TO_ANG
b = b_bohr * BOHR_TO_ANG
c = c_bohr * BOHR_TO_ANG

# Triclinic 격자 벡터 계산
a1 = ( a,                          0.0,     0.0 )
a2 = ( b * math.cos(gamma),     b * math.sin(gamma), 0.0 )
# a3 구성요소
c_x = c * math.cos(beta)
c_y = c * (math.cos(alpha) - math.cos(beta) * math.cos(gamma)) / math.sin(gamma)
# 음수 방지를 위해 max 사용
tmp = c**2 - c_x**2 - c_y**2
c_z = math.sqrt(tmp) if tmp > 0 else 0.0
a3 = ( c_x, c_y, c_z )

lattice = [a1, a2, a3]

# ionic position 패턴
pattern = re.compile(r"^\s*ion\s+(\w+)\s+([\-\d.eE]+)\s+([\-\d.eE]+)\s+([\-\d.eE]+)\s+\d+\s*$")
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
                sym = m.group(1)
                fx, fy, fz = map(float, m.group(2,3,4))
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

if not frames:
    print("No ionic-position blocks found.")
    sys.exit(0)

xyz_path = out_path.with_name("trj_triclinic.xyz")
with xyz_path.open("w") as xyz:
    for step, atoms in enumerate(frames, 1):
        xyz.write(f"{len(atoms)}\n")
        xyz.write(f"Frame {step}\n")
        for sym, x, y, z in atoms:
            xyz.write(f"{sym:<2} {x:.6f} {y:.6f} {z:.6f}\n")

print(f"Wrote {len(frames)} frame(s) to {xyz_path}")

