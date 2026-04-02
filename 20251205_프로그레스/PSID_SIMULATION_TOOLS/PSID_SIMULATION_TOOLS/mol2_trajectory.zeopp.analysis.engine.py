#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
mol2_trajectory.zeopp.analysis.engine.py

- Multi-frame MOL2 trajectory → per-frame MOL2 → per-frame CIF
- For each frame, run Zeo++ `network` to compute:
    * -res   : pore diameters (Di, Df, Dif)
    * -sa    : accessible surface area
    * -vol   : accessible volume (void fraction etc.)
    * -volpo : probe-occupiable volume
- Aggregate results into a CSV summary.
"""

import argparse
import logging
import subprocess
import sys
import textwrap
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any

import math
import re

import pandas as pd
from tqdm import tqdm

LOGGER_NAME = "md_pore_zeopp_pipeline"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logger(output_root: Path, log_level: str = "INFO") -> logging.Logger:
    """
    Set up logger with both file and console handlers.
    """
    log_dir = output_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "pipeline.log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(log_level.upper())

    # 기존 핸들러 제거 (재실행 시 중복 방지)
    logger.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 파일 핸들러
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(log_level.upper())
    logger.addHandler(fh)

    # 콘솔 핸들러
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(log_level.upper())
    logger.addHandler(ch)

    logger.info("Logger initialized. Log file: %s", str(log_path))
    return logger


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

def run_command(cmd: List[str],
                logger: logging.Logger,
                timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """
    Run a shell command and raise RuntimeError on non-zero return code.
    """
    logger.debug("Running command: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        logger.error("Command timed out: %s", " ".join(cmd))
        raise RuntimeError(f"Command timed out: {' '.join(cmd)}") from e

    if result.returncode != 0:
        # 음수면 보통 signal kill (예: -9 = SIGKILL)
        logger.error("Command failed with return code %s", result.returncode)
        logger.debug("stdout:\n%s", result.stdout)
        logger.debug("stderr:\n%s", result.stderr)
        raise RuntimeError(
            textwrap.dedent(
                f"""\
                Command failed: {' '.join(cmd)}
                return code: {result.returncode}
                stdout:
                {result.stdout}
                stderr:
                {result.stderr}
                """
            ).strip()
        )

    logger.debug("Command succeeded.")
    return result


# ---------------------------------------------------------------------------
# MOL2 → per-frame MOL2
# ---------------------------------------------------------------------------

def split_mol2_frames(mol2_path: Path, frames_dir: Path,
                      logger: logging.Logger) -> List[Path]:
    """
    Split multi-frame MOL2 (Materials Studio trajectory export) into
    single-frame MOL2 files.

    Frames are separated by occurrences of '@<TRIPOS>MOLECULE'.
    """
    frames_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Splitting multi-frame MOL2: %s", mol2_path)

    frame_paths: List[Path] = []
    current_lines: List[str] = []
    frame_idx = 0

    with mol2_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("@<TRIPOS>MOLECULE"):
                # 이전 프레임 저장
                if current_lines:
                    frame_name = f"frame_{frame_idx:05d}.mol2"
                    frame_file = frames_dir / frame_name
                    with frame_file.open("w", encoding="utf-8") as out:
                        out.writelines(current_lines)
                    frame_paths.append(frame_file)
                    frame_idx += 1
                    current_lines = []
            current_lines.append(line)

    # 마지막 프레임
    if current_lines:
        frame_name = f"frame_{frame_idx:05d}.mol2"
        frame_file = frames_dir / frame_name
        with frame_file.open("w", encoding="utf-8") as out:
            out.writelines(current_lines)
        frame_paths.append(frame_file)

    logger.info("Split into %d frames (saved in %s).",
                len(frame_paths), frames_dir)
    return frame_paths


# ---------------------------------------------------------------------------
# MOL2 (single frame) → CIF (single frame)
# ---------------------------------------------------------------------------

def _parse_crysin(mol2_lines: List[str]) -> Optional[Dict[str, float]]:
    """
    Parse @<TRIPOS>CRYSIN section from MOL2 to obtain cell parameters.
    Returns dict with a, b, c, alpha, beta, gamma (floats) or None.
    """
    for i, line in enumerate(mol2_lines):
        if line.startswith("@<TRIPOS>CRYSIN"):
            # 다음 줄: a b c alpha beta gamma ...
            if i + 1 < len(mol2_lines):
                parts = mol2_lines[i + 1].split()
                if len(parts) >= 6:
                    try:
                        a = float(parts[0])
                        b = float(parts[1])
                        c = float(parts[2])
                        alpha = float(parts[3])
                        beta = float(parts[4])
                        gamma = float(parts[5])
                        return {
                            "a": a, "b": b, "c": c,
                            "alpha": alpha, "beta": beta, "gamma": gamma,
                        }
                    except ValueError:
                        return None
    return None


def _parse_atoms_from_mol2(mol2_lines: List[str]) -> List[Dict[str, Any]]:
    """
    Parse @<TRIPOS>ATOM section and return list of dicts:
    {label, element, x, y, z}
    """
    atoms: List[Dict[str, Any]] = []
    in_atom = False
    for line in mol2_lines:
        if line.startswith("@<TRIPOS>ATOM"):
            in_atom = True
            continue
        if line.startswith("@<TRIPOS>") and not line.startswith("@<TRIPOS>ATOM"):
            if in_atom:
                break
        if in_atom:
            parts = line.split()
            if len(parts) < 6:
                continue
            # MOL2 atom line:
            # atom_id, atom_name, x, y, z, atom_type, [subst_id, subst_name, charge]
            atom_name = parts[1]
            try:
                x = float(parts[2])
                y = float(parts[3])
                z = float(parts[4])
            except ValueError:
                continue
            # 원소 추정 (atom_name에서 알파벳 부분만)
            m = re.match(r"([A-Za-z]+)", atom_name)
            element = (m.group(1) if m else atom_name)[0]
            atoms.append({
                "label": atom_name,
                "element": element,
                "x": x,
                "y": y,
                "z": z,
            })
    return atoms


def _cartesian_to_fractional(x, y, z, a, b, c, alpha, beta, gamma):
    """
    Convert Cartesian (Å) to fractional for a general triclinic cell.
    """
    alpha_r = math.radians(alpha)
    beta_r = math.radians(beta)
    gamma_r = math.radians(gamma)

    # a vector
    ax = a
    ay = 0.0
    az = 0.0

    # b vector
    bx = b * math.cos(gamma_r)
    by = b * math.sin(gamma_r)
    bz = 0.0

    # c vector
    cx = c * math.cos(beta_r)
    cy = c * (math.cos(alpha_r) - math.cos(beta_r) * math.cos(gamma_r)) / math.sin(gamma_r)
    cz_sq = c**2 - cx**2 - cy**2
    if cz_sq < 0:
        cz_sq = 0.0
    cz = math.sqrt(cz_sq)

    det = (
        ax * (by * cz - bz * cy)
        - ay * (bx * cz - bz * cx)
        + az * (bx * cy - by * cx)
    )
    if abs(det) < 1e-12:
        raise ValueError("Cell matrix determinant is zero; invalid cell.")

    inv = [
        [(by * cz - bz * cy) / det,
         -(bx * cz - bz * cx) / det,
         (bx * cy - by * cx) / det],
        [-(ay * cz - az * cy) / det,
         (ax * cz - az * cx) / det,
         -(ax * cy - ay * cx) / det],
        [(ay * bz - az * by) / det,
         -(ax * bz - az * bx) / det,
         (ax * by - ay * bx) / det],
    ]

    u = inv[0][0] * x + inv[0][1] * y + inv[0][2] * z
    v = inv[1][0] * x + inv[1][1] * y + inv[1][2] * z
    w = inv[2][0] * x + inv[2][1] * y + inv[2][2] * z

    return u, v, w


def convert_mol2_to_cif_single_frame(mol2_path: Path,
                                     cif_path: Path,
                                     logger: logging.Logger) -> None:
    """
    Very simple MOL2(single frame, with CRYSIN+ATOM) → CIF(P1) converter.

    - Assumes coordinates are Cartesian in Å in the simulation cell.
    - Uses CRYSIN for cell parameters.
    - Converts to fractional coordinates.
    """
    with mol2_path.open("r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    crys = _parse_crysin(lines)
    if crys is None:
        raise RuntimeError(f"CRYSIN section not found in {mol2_path}")

    atoms = _parse_atoms_from_mol2(lines)
    if not atoms:
        raise RuntimeError(f"No atoms parsed from {mol2_path}")

    a = crys["a"]
    b = crys["b"]
    c = crys["c"]
    alpha = crys["alpha"]
    beta = crys["beta"]
    gamma = crys["gamma"]

    logger.debug("CRYSIN for %s: a=%.3f b=%.3f c=%.3f alpha=%.2f beta=%.2f gamma=%.2f",
                 mol2_path.name, a, b, c, alpha, beta, gamma)

    cif_lines: List[str] = []
    data_name = cif_path.stem
    cif_lines.append(f"data_{data_name}\n")
    cif_lines.append(f"_cell_length_a    {a:.6f}\n")
    cif_lines.append(f"_cell_length_b    {b:.6f}\n")
    cif_lines.append(f"_cell_length_c    {c:.6f}\n")
    cif_lines.append(f"_cell_angle_alpha {alpha:.6f}\n")
    cif_lines.append(f"_cell_angle_beta  {beta:.6f}\n")
    cif_lines.append(f"_cell_angle_gamma {gamma:.6f}\n")
    cif_lines.append("_symmetry_space_group_name_H-M    'P1'\n")
    cif_lines.append("_symmetry_Int_Tables_number       1\n\n")
    cif_lines.append("loop_\n")
    cif_lines.append("_symmetry_equiv_pos_as_xyz\n")
    cif_lines.append("  'x, y, z'\n\n")

    cif_lines.append("loop_\n")
    cif_lines.append("_atom_site_label\n")
    cif_lines.append("_atom_site_type_symbol\n")
    cif_lines.append("_atom_site_fract_x\n")
    cif_lines.append("_atom_site_fract_y\n")
    cif_lines.append("_atom_site_fract_z\n")

    for atom in atoms:
        u, v, w = _cartesian_to_fractional(
            atom["x"], atom["y"], atom["z"],
            a, b, c, alpha, beta, gamma
        )
        # [0,1)로 wrap
        u -= math.floor(u)
        v -= math.floor(v)
        w -= math.floor(w)
        cif_lines.append(
            f"{atom['label']} {atom['element']} {u:.6f} {v:.6f} {w:.6f}\n"
        )

    cif_path.parent.mkdir(parents=True, exist_ok=True)
    with cif_path.open("w", encoding="utf-8") as f:
        f.writelines(cif_lines)

    logger.info(
        "Converting MOL2 to CIF (custom parser): %s -> %s",
        mol2_path, cif_path
    )


# ---------------------------------------------------------------------------
# Zeo++ output parsers
# ---------------------------------------------------------------------------

def parse_res_file(res_path: Path) -> Dict[str, Optional[float]]:
    """
    Parse .res file: path Di Df Dif
    """
    if not res_path.exists():
        return {"Di": None, "Df": None, "Dif": None}
    with res_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            tokens = line.split()
            if len(tokens) >= 4:
                try:
                    di, df, dif = map(float, tokens[-3:])
                    return {"Di": di, "Df": df, "Dif": dif}
                except ValueError:
                    continue
    return {"Di": None, "Df": None, "Dif": None}


def _extract_float(pattern: str, text: str) -> Optional[float]:
    m = re.search(pattern, text, flags=re.MULTILINE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_sa_file(sa_path: Path) -> Dict[str, Optional[float]]:
    """
    Parse .sa file from `network -sa`.
    We extract ASA and NASA metrics.
    """
    keys = [
        "ASA_A2",
        "ASA_m2_cm3",
        "ASA_m2_g",
        "NASA_A2",
        "NASA_m2_cm3",
        "NASA_m2_g",
    ]
    result = {k: None for k in keys}
    if not sa_path.exists():
        return result

    text = sa_path.read_text(encoding="utf-8", errors="ignore")

    result["ASA_A2"] = _extract_float(r"ASA_A\^2:\s*([0-9Ee+\-\.]+)", text)
    result["ASA_m2_cm3"] = _extract_float(r"ASA_m\^2/cm\^3:\s*([0-9Ee+\-\.]+)", text)
    result["ASA_m2_g"] = _extract_float(r"ASA_m\^2/g:\s*([0-9Ee+\-\.]+)", text)

    result["NASA_A2"] = _extract_float(r"NASA_A\^2:\s*([0-9Ee+\-\.]+)", text)
    result["NASA_m2_cm3"] = _extract_float(r"NASA_m\^2/cm\^3:\s*([0-9Ee+\-\.]+)", text)
    result["NASA_m2_g"] = _extract_float(r"NASA_m\^2/g:\s*([0-9Ee+\-\.]+)", text)

    return result


def parse_vol_file(vol_path: Path) -> Dict[str, Optional[float]]:
    """
    Parse .vol file from `network -vol`.
    We extract accessible volume (AV) and non-accessible volume (NAV).
    """
    keys = [
        "AV_A3",
        "AV_Volume_fraction",
        "AV_cm3_g",
        "NAV_A3",
        "NAV_Volume_fraction",
        "NAV_cm3_g",
    ]
    result = {k: None for k in keys}
    if not vol_path.exists():
        return result

    text = vol_path.read_text(encoding="utf-8", errors="ignore")

    result["AV_A3"] = _extract_float(r"AV_A\^3:\s*([0-9Ee+\-\.]+)", text)
    result["AV_Volume_fraction"] = _extract_float(
        r"AV_Volume_fraction:\s*([0-9Ee+\-\.]+)", text
    )
    result["AV_cm3_g"] = _extract_float(r"AV_cm\^3/g:\s*([0-9Ee+\-\.]+)", text)

    result["NAV_A3"] = _extract_float(r"NAV_A\^3:\s*([0-9Ee+\-\.]+)", text)
    result["NAV_Volume_fraction"] = _extract_float(
        r"NAV_Volume_fraction:\s*([0-9Ee+\-\.]+)", text
    )
    result["NAV_cm3_g"] = _extract_float(r"NAV_cm\^3/g:\s*([0-9Ee+\-\.]+)", text)

    return result


def parse_volpo_file(volpo_path: Path) -> Dict[str, Optional[float]]:
    """
    Parse .volpo file from `network -volpo`.
    We extract probe-occupiable accessible volume (POAV) and non-accessible (PONAV).
    """
    keys = [
        "POAV_A3",
        "POAV_Volume_fraction",
        "POAV_cm3_g",
        "PONAV_A3",
        "PONAV_Volume_fraction",
        "PONAV_cm3_g",
    ]
    result = {k: None for k in keys}
    if not volpo_path.exists():
        return result

    text = volpo_path.read_text(encoding="utf-8", errors="ignore")

    result["POAV_A3"] = _extract_float(r"POAV_A\^3:\s*([0-9Ee+\-\.]+)", text)
    result["POAV_Volume_fraction"] = _extract_float(
        r"POAV_Volume_fraction:\s*([0-9Ee+\-\.]+)", text
    )
    result["POAV_cm3_g"] = _extract_float(r"POAV_cm\^3/g:\s*([0-9Ee+\-\.]+)", text)

    result["PONAV_A3"] = _extract_float(r"PONAV_A\^3:\s*([0-9Ee+\-\.]+)", text)
    result["PONAV_Volume_fraction"] = _extract_float(
        r"PONAV_Volume_fraction:\s*([0-9Ee+\-\.]+)", text
    )
    result["PONAV_cm3_g"] = _extract_float(r"PONAV_cm\^3/g:\s*([0-9Ee+\-\.]+)", text)

    return result


# ---------------------------------------------------------------------------
# Dataclass for per-frame result
# ---------------------------------------------------------------------------

@dataclass
class FrameResult:
    frame_name: str
    status: str
    error_message: str

    # Zeo++ -res
    Di: Optional[float] = None
    Df: Optional[float] = None
    Dif: Optional[float] = None

    # Zeo++ -sa
    ASA_A2: Optional[float] = None
    ASA_m2_cm3: Optional[float] = None
    ASA_m2_g: Optional[float] = None
    NASA_A2: Optional[float] = None
    NASA_m2_cm3: Optional[float] = None
    NASA_m2_g: Optional[float] = None

    # Zeo++ -vol
    AV_A3: Optional[float] = None
    AV_Volume_fraction: Optional[float] = None
    AV_cm3_g: Optional[float] = None
    NAV_A3: Optional[float] = None
    NAV_Volume_fraction: Optional[float] = None
    NAV_cm3_g: Optional[float] = None

    # Zeo++ -volpo
    POAV_A3: Optional[float] = None
    POAV_Volume_fraction: Optional[float] = None
    POAV_cm3_g: Optional[float] = None
    PONAV_A3: Optional[float] = None
    PONAV_Volume_fraction: Optional[float] = None
    PONAV_cm3_g: Optional[float] = None


# ---------------------------------------------------------------------------
# Run Zeo++ for one frame
# ---------------------------------------------------------------------------

def run_zeopp_for_frame(
    frame_name: str,
    cif_path: Path,
    res_dir: Path,
    sa_dir: Path,
    vol_dir: Path,
    volpo_dir: Path,
    network_exe: str,
    use_ha: bool,
    sa_chan_radius: float,
    sa_probe_radius: float,
    sa_samples: int,
    vol_chan_radius: float,
    vol_probe_radius: float,
    vol_samples: int,
    volpo_chan_radius: float,
    volpo_probe_radius: float,
    volpo_samples: int,
    timeout: Optional[int],
    logger: logging.Logger,
) -> Dict[str, Optional[float]]:
    """
    Run Zeo++ `network` for a single CIF frame, computing:
    -res, -sa, -vol, -volpo and parsing their outputs.
    """
    res_dir.mkdir(parents=True, exist_ok=True)
    sa_dir.mkdir(parents=True, exist_ok=True)
    vol_dir.mkdir(parents=True, exist_ok=True)
    volpo_dir.mkdir(parents=True, exist_ok=True)

    res_path = res_dir / f"{frame_name}.res"
    sa_path = sa_dir / f"{frame_name}.sa"
    vol_path = vol_dir / f"{frame_name}.vol"
    volpo_path = volpo_dir / f"{frame_name}.volpo"

    # Base Zeo++ command (without option)
    base = [network_exe]
    if use_ha:
        base.append("-ha")

    # 1) -res
    cmd_res = base + ["-res", str(res_path), str(cif_path)]
    run_command(cmd_res, logger=logger, timeout=timeout)

    # 2) -sa
    cmd_sa = base + [
        "-sa",
        str(sa_chan_radius),
        str(sa_probe_radius),
        str(sa_samples),
        str(sa_path),
        str(cif_path),
    ]
    run_command(cmd_sa, logger=logger, timeout=timeout)

    # 3) -vol
    cmd_vol = base + [
        "-vol",
        str(vol_chan_radius),
        str(vol_probe_radius),
        str(vol_samples),
        str(vol_path),
        str(cif_path),
    ]
    run_command(cmd_vol, logger=logger, timeout=timeout)

    # 4) -volpo
    cmd_volpo = base + [
        "-volpo",
        str(volpo_chan_radius),
        str(volpo_probe_radius),
        str(volpo_samples),
        str(volpo_path),
        str(cif_path),
    ]
    run_command(cmd_volpo, logger=logger, timeout=timeout)

    # Parse outputs
    out: Dict[str, Optional[float]] = {}
    out.update(parse_res_file(res_path))
    out.update(parse_sa_file(sa_path))
    out.update(parse_vol_file(vol_path))
    out.update(parse_volpo_file(volpo_path))
    return out


# ---------------------------------------------------------------------------
# Per-frame processing
# ---------------------------------------------------------------------------

def process_single_frame(
    frame_mol2_path: Path,
    frames_cif_dir: Path,
    res_dir: Path,
    sa_dir: Path,
    vol_dir: Path,
    volpo_dir: Path,
    network_exe: str,
    use_ha: bool,
    sa_chan_radius: float,
    sa_probe_radius: float,
    sa_samples: int,
    vol_chan_radius: float,
    vol_probe_radius: float,
    vol_samples: int,
    volpo_chan_radius: float,
    volpo_probe_radius: float,
    volpo_samples: int,
    timeout: Optional[int],
    logger: logging.Logger,
) -> FrameResult:
    """
    Convert a single-frame MOL2 to CIF and run Zeo++ analyses.
    """
    frame_name = frame_mol2_path.stem  # e.g. frame_00000
    logger.info("Processing frame %s", frame_name)

    cif_path = frames_cif_dir / f"{frame_name}.cif"

    try:
        # MOL2 → CIF
        convert_mol2_to_cif_single_frame(frame_mol2_path, cif_path, logger)

        # Zeo++
        logger.info("Running Zeo++ for frame %s", frame_name)
        zeopp_vals = run_zeopp_for_frame(
            frame_name=frame_name,
            cif_path=cif_path,
            res_dir=res_dir,
            sa_dir=sa_dir,
            vol_dir=vol_dir,
            volpo_dir=volpo_dir,
            network_exe=network_exe,
            use_ha=use_ha,
            sa_chan_radius=sa_chan_radius,
            sa_probe_radius=sa_probe_radius,
            sa_samples=sa_samples,
            vol_chan_radius=vol_chan_radius,
            vol_probe_radius=vol_probe_radius,
            vol_samples=vol_samples,
            volpo_chan_radius=volpo_chan_radius,
            volpo_probe_radius=volpo_probe_radius,
            volpo_samples=volpo_samples,
            timeout=timeout,
            logger=logger,
        )

        fr = FrameResult(
            frame_name=frame_name,
            status="ok",
            error_message="",
            **zeopp_vals,
        )
    except Exception as e:
        logger.error("Frame %s failed: %s", frame_name, e)
        fr = FrameResult(
            frame_name=frame_name,
            status="failed",
            error_message=str(e),
        )
    return fr


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Analyze multi-frame MOL2 trajectory with Zeo++ (per-frame CIF + -res/-sa/-vol/-volpo)."
    )
    p.add_argument(
        "--mol2",
        "-i",
        type=str,
        required=True,
        help="Input multi-frame MOL2 path.",
    )
    p.add_argument(
        "--output-root",
        "-o",
        type=str,
        required=True,
        help="Root directory for outputs (frames, zeopp_results, summary, logs).",
    )
    p.add_argument(
        "--network-exe",
        type=str,
        default="network",
        help="Zeo++ network executable name or path (default: network).",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    p.add_argument(
        "--use-ha",
        action="store_true",
        default=False,
        help="Use Zeo++ -ha (high accuracy) flag. (Default: OFF to be safer.)",
    )
    p.add_argument(
        "--zeopp-timeout",
        type=int,
        default=None,
        help="Timeout (seconds) for each Zeo++ command. Default: no timeout.",
    )

    # SA parameters
    p.add_argument(
        "--sa-chan-radius",
        type=float,
        default=1.2,
        help="Channel radius (Å) for -sa (default: 1.2).",
    )
    p.add_argument(
        "--sa-probe-radius",
        type=float,
        default=1.2,
        help="Probe radius (Å) for -sa (default: 1.2).",
    )
    p.add_argument(
        "--sa-samples",
        type=int,
        default=2000,
        help="Number of MC samples per atom for -sa (default: 2000).",
    )

    # VOL parameters
    p.add_argument(
        "--vol-chan-radius",
        type=float,
        default=1.2,
        help="Channel radius (Å) for -vol (default: 1.2).",
    )
    p.add_argument(
        "--vol-probe-radius",
        type=float,
        default=1.2,
        help="Probe radius (Å) for -vol (default: 1.2).",
    )
    p.add_argument(
        "--vol-samples",
        type=int,
        default=50000,
        help="Number of MC samples per unit cell for -vol (default: 50000).",
    )

    # VOLPO parameters
    p.add_argument(
        "--volpo-chan-radius",
        type=float,
        default=1.2,
        help="Channel radius (Å) for -volpo (default: 1.2).",
    )
    p.add_argument(
        "--volpo-probe-radius",
        type=float,
        default=1.2,
        help="Probe radius (Å) for -volpo (default: 1.2).",
    )
    p.add_argument(
        "--volpo-samples",
        type=int,
        default=50000,
        help="Number of MC samples per unit cell for -volpo (default: 50000).",
    )

    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    mol2_path = Path(args.mol2).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()

    output_root.mkdir(parents=True, exist_ok=True)

    logger = setup_logger(output_root, log_level=args.log_level)
    logger.info("MOL2 path   : %s", mol2_path)
    logger.info("Output root : %s", output_root)

    frames_mol2_dir = output_root / "frames_mol2"
    frames_cif_dir = output_root / "frames_cif"

    zeopp_root = output_root / "zeopp_results"
    res_dir = zeopp_root / "res"
    sa_dir = zeopp_root / "sa"
    vol_dir = zeopp_root / "vol"
    volpo_dir = zeopp_root / "volpo"

    summary_dir = output_root / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)

    # Split multi-frame MOL2
    frame_mol2_paths = split_mol2_frames(mol2_path, frames_mol2_dir, logger=logger)
    n_frames = len(frame_mol2_paths)
    logger.info("Total frames: %d", n_frames)

    results: List[FrameResult] = []

    for frame_mol2_path in tqdm(
        frame_mol2_paths,
        desc="Processing frames (mol2→cif→Zeo++)",
        unit="frame"
    ):
        fr = process_single_frame(
    frame_mol2_path=frame_mol2_path,
    frames_cif_dir=frames_cif_dir,
    res_dir=res_dir,
    sa_dir=sa_dir,
    vol_dir=vol_dir,
    volpo_dir=volpo_dir,
    network_exe=args.network_exe,
    use_ha=args.use_ha,
    sa_chan_radius=args.sa_chan_radius,
    sa_probe_radius=args.sa_probe_radius,   # ← 여기 수정!!
    sa_samples=args.sa_samples,
    vol_chan_radius=args.vol_chan_radius,
    vol_probe_radius=args.vol_probe_radius,
    vol_samples=args.vol_samples,
    volpo_chan_radius=args.volpo_chan_radius,
    volpo_probe_radius=args.volpo_probe_radius,
    volpo_samples=args.volpo_samples,
    timeout=args.zeopp_timeout,
    logger=logger,
)

        results.append(fr)

    # Save summary CSV
    df = pd.DataFrame([asdict(r) for r in results])
    summary_path = summary_dir / "pore_properties_per_frame.csv"
    df.to_csv(summary_path, index=False)
    logger.info("Summary CSV saved: %s", summary_path)

    # Report
    n_ok = sum(1 for r in results if r.status == "ok")
    n_fail = n_frames - n_ok
    if n_ok == 0:
        logger.error("No successful frames (status != 'ok'). Check error_message column in CSV.")
    else:
        logger.info("Frames succeeded: %d, failed: %d", n_ok, n_fail)


if __name__ == "__main__":
    main()


