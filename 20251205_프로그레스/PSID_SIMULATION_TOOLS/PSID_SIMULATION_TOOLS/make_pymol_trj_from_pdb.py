import os
import subprocess
import sys
import shutil
from pathlib import Path
from multiprocessing import Pool, cpu_count
from tqdm import tqdm

def split_pdb_frames(pdb_path, tmp_dir):
    frame_paths = []
    frame_lines = []
    frame_index = 1

    with open(pdb_path) as f:
        for line in f:
            if line.startswith("MODEL"):
                frame_lines = []
            elif line.startswith("ENDMDL"):
                frame_file = tmp_dir / f"frame_{frame_index:04d}.pdb"
                with open(frame_file, 'w') as out:
                    out.write("".join(frame_lines))
                frame_paths.append(frame_file)
                frame_index += 1
            else:
                if line.startswith("ATOM") or line.startswith("HETATM"):
                    frame_lines.append(line)

    return frame_paths


def convert_xyz_to_pdb_with_obabel(xyz_file: Path):
    mol2_file = xyz_file.with_suffix('.mol2')
    pdb_file = xyz_file.with_suffix('.pdb')
    try:
        subprocess.run(['obabel', str(xyz_file), '-O', str(mol2_file)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['obabel', str(mol2_file), '-O', str(pdb_file)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return pdb_file
    except subprocess.CalledProcessError:
        print(f"❌ Failed to convert {xyz_file.name}")
        return None

def merge_pdbs_to_multimodel(pdb_files, output_file):
    with open(output_file, 'w') as out:
        for i, pdb_file in enumerate(tqdm(pdb_files, desc="[MERGE] Writing frames", unit="frame")):
            if pdb_file and pdb_file.exists():
                out.write(f"MODEL     {i+1}\n")
                with open(pdb_file) as f:
                    for line in f:
                        if not line.startswith("END") and not line.startswith("MODEL"):
                            out.write(line)
                out.write("ENDMDL\n")

def main():
    if len(sys.argv) != 3:
        print("Usage: python xyz_to_multimodel_pdb_with_obabel.py input.xyz output.pdb")
        sys.exit(1)

    pdb_path = Path(sys.argv[1]).resolve()
    output_pdb = Path(sys.argv[2]).resolve()
    tmp_dir = pdb_path.parent / pdb_path.stem

    if not tmp_dir.exists():
        tmp_dir.mkdir()

    print(f"[1] Created temporary folder: {tmp_dir}")
    pdb_frames = split_pdb_frames(pdb_path, tmp_dir)

    print(f"[2] Converting {len(pdb_frames)} frames using up to {min(8, cpu_count())} CPUs...")
    os.environ["OMP_NUM_THREADS"] = "1"

    with Pool(processes=min(8, cpu_count())) as pool:
        pdb_frames = list(tqdm(pool.imap(convert_xyz_to_pdb_with_obabel, pdb_frames),
                               total=len(pdb_frames), desc="[CONVERT] xyz → pdb", unit="frame"))

    print("[3] Merging into multi-model PDB...")
    merge_pdbs_to_multimodel(pdb_frames, output_pdb)

    print("[4] Cleaning up temporary directory...")
    shutil.rmtree(tmp_dir)

    print(f"✅ Done! Multi-model PDB saved to: {output_pdb}")

if __name__ == "__main__":
    main()
