#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Recursive Forcite XCD merger (NVT / NPT, restart runs)

- 지정한 root 디렉토리 아래의 .xcd 파일을 재귀적으로 찾고
- 같은 태그(tag) + 물성(property) 조합(예: "Molecule3" + "Temperature")을 그룹으로 묶은 뒤
- 재시작(run)마다 0ps부터 다시 시작한 시간을 "총 누적 ps"로 이어 붙여 하나의 time-series로 만든다.
- 각 그룹(tag, property)에 대해:
    * long-format CSV 저장
    * PNG 시각화 저장
- Energies / Pressure / Temperature / Cell / Density / Stress / etc. 모든 XCD를 자동 인식.

태그(tag) & 물성(property) 규칙(파일명 기준)
------------------------------------------------
예) "Molecule3 Temperature.xcd" → tag="Molecule3", property="Temperature"
예) "case_001 Energies.xcd"    → tag="case_001", property="Energies"

즉, 파일 이름에서 확장자(.xcd)를 제거하고,
맨 마지막 공백 기준으로 나눈 뒤:
    tag = 앞부분 전체, property = 마지막 토큰
으로 정의한다.

사용 예시
---------
python merge_forcite_xcd_recursive.py \\
    --root "/mnt/c/Users/PSID_PC_20/Downloads/20251208_MS/Oligomer_COMPASS_Files/Documents/298K_1ATM_10_PoreA_UFF/Molecule3 Forcite Dynamics" \\
    --max-depth 6 \\
    --out-dir "./forcite_merged" \\
    --log-file "./forcite_merged/merge.log" \\
    --verbose

필요 패키지
-----------
- pandas
- matplotlib
- tqdm
"""

import os
import re
import argparse
import logging
from logging.handlers import RotatingFileHandler
import xml.etree.ElementTree as ET

from typing import Dict, List, Tuple

import pandas as pd
import matplotlib.pyplot as plt
from tqdm import tqdm


# ======================================================================
# 로깅 설정
# ======================================================================

def setup_logger(log_file: str = None, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("forcite_xcd_merger")
    if logger.handlers:
        # 이미 설정된 경우 재사용
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 콘솔 핸들러
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 파일 핸들러 (선택)
    if log_file is not None:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.debug("Logger initialized. log_file=%s, verbose=%s", log_file, verbose)
    return logger


# ======================================================================
# 유틸 함수
# ======================================================================

def slugify(text: str) -> str:
    """
    파일명으로 쓰기 좋은 형태로 변환.
    """
    text = text.strip().replace(" ", "_")
    text = re.sub(r"[^0-9a-zA-Z_\-]+", "", text)
    if not text:
        text = "unknown"
    return text


def extract_tag_property_from_filename(filename: str) -> Tuple[str, str]:
    """
    "Molecule3 Temperature.xcd" -> ("Molecule3", "Temperature")
    "case_001 Energies.xcd"    -> ("case_001", "Energies")
    """
    base = os.path.splitext(os.path.basename(filename))[0]
    parts = base.split()
    if len(parts) == 1:
        # 예: "Energies.xcd" 처럼 prefix가 없는 경우
        tag = "DEFAULT"
        prop = parts[0]
    else:
        tag = " ".join(parts[:-1])
        prop = parts[-1]
    return tag, prop


def walk_xcd_files(root: str, max_depth: int = None, logger: logging.Logger = None) -> List[str]:
    """
    root 아래에서 .xcd 파일을 재귀적으로 찾는다.
    max_depth가 지정되면 root 기준 상대 depth가 그 이하인 디렉토리만 탐색.
    """
    root = os.path.abspath(root)
    root_depth = root.rstrip(os.sep).count(os.sep)
    xcd_files: List[str] = []

    if logger:
        logger.info("Scanning XCD files under root=%s (max_depth=%s)", root, max_depth)

    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        if (max_depth is not None) and (depth > max_depth):
            # 이 디렉토리 아래는 더 내려가지 않도록
            dirnames[:] = []
            continue

        for fname in filenames:
            if fname.lower().endswith(".xcd"):
                full = os.path.join(dirpath, fname)
                xcd_files.append(full)

    if logger:
        logger.info("Found %d XCD files.", len(xcd_files))

    return xcd_files


# ======================================================================
# XCD 파서
# ======================================================================

def parse_xcd_file(path: str, logger: logging.Logger = None) -> Dict[str, Dict[str, List[float]]]:
    """
    하나의 XCD 파일을 파싱해서
    { series_name: {"t": [..], "y": [..]} } 형태로 반환.

    X, Y는 POINT_2D의 XY="x,y" 속성에서 가져온다.
    """
    if logger:
        logger.debug("Parsing XCD: %s", path)

    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as e:
        if logger:
            logger.error("Failed to parse XCD %s: %s", path, e)
        return {}

    series_dict: Dict[str, Dict[str, List[float]]] = {}

    # Forcite XCD는 보통 CHART_2D / DATA_2D / SERIES_2D 구조
    for chart in root.findall(".//CHART_2D"):
        data2d = chart.find("DATA_2D")
        if data2d is None:
            continue

        for series in data2d.findall("SERIES_2D"):
            name = series.get("Name", "Series")
            xs: List[float] = []
            ys: List[float] = []

            for pt in series.findall("POINT_2D"):
                xy = pt.get("XY")
                if not xy:
                    continue
                try:
                    x_str, y_str = xy.split(",")
                    x_val = float(x_str)
                    y_val = float(y_str)
                    xs.append(x_val)
                    ys.append(y_val)
                except Exception:
                    if logger:
                        logger.debug("Failed to parse point '%s' in %s", xy, path)
                    continue

            if xs:
                # 같은 이름의 시리즈가 여러 차트에 있을 가능성까지 고려해 append
                if name not in series_dict:
                    series_dict[name] = {"t": [], "y": []}
                series_dict[name]["t"].extend(xs)
                series_dict[name]["y"].extend(ys)

    if logger:
        logger.debug(
            "Parsed XCD: %s (series: %s)",
            path,
            ", ".join(series_dict.keys()) if series_dict else "NONE",
        )

    return series_dict


# ======================================================================
# 그룹(같은 tag, property)의 여러 run을 이어붙이기
# ======================================================================

def merge_segments_for_group(
    tag: str,
    prop: str,
    files: List[str],
    logger: logging.Logger,
) -> pd.DataFrame:
    """
    같은 (tag, property)에 속하는 여러 XCD 파일을 시계열로 이어붙여
    long-format DataFrame으로 반환.

    컬럼:
        - tag
        - property
        - series_name
        - segment_index
        - file_path
        - time_ps  (누적 ps)
        - value
    """
    files_sorted = sorted(files, key=lambda p: p)  # 경로 기준 정렬 (대략 run 순서)
    all_rows = []

    time_offset = 0.0  # 누적 ps
    segment_index = 0

    for path in tqdm(files_sorted, desc=f"Segments for {tag}/{prop}", unit="seg", leave=False):
        series_dict = parse_xcd_file(path, logger=logger)
        if not series_dict:
            logger.warning("No series parsed from %s, skip.", path)
            continue

        # 기준 시리즈(ref)를 하나 정한다 (첫 번째)
        series_names = sorted(series_dict.keys())
        ref_name = series_names[0]
        t_ref = series_dict[ref_name]["t"]

        if not t_ref:
            logger.warning("Empty time series in %s, skip.", path)
            continue

        t_start = t_ref[0]
        t_end = t_ref[-1]
        duration = t_end - t_start

        if duration <= 0:
            logger.warning(
                "Non-positive duration in %s (start=%.6f, end=%.6f), treat as zero.",
                path,
                t_start,
                t_end,
            )

        # 시간 축: 해당 run의 t를 0 기준으로 바꾼 뒤 누적 offset 추가
        # (즉, 각 run의 처음 시점을 0으로 보고, duration만큼 늘려 붙이는 방식)
        shifted_t_ref = [(t - t_start) + time_offset for t in t_ref]

        # sanity check: 다른 시리즈도 동일 길이/시간을 공유한다고 가정
        for sname in series_names:
            t_list = series_dict[sname]["t"]
            y_list = series_dict[sname]["y"]

            if len(t_list) != len(shifted_t_ref):
                logger.warning(
                    "Time length mismatch for %s in %s: ref=%d, %s=%d",
                    sname,
                    path,
                    len(shifted_t_ref),
                    sname,
                    len(t_list),
                )
                # 길이가 다른 시리즈는 스킵
                continue

            # 각 포인트를 long-format row로 추가
            for t_new, (_, y_val) in zip(shifted_t_ref, zip(t_list, y_list)):
                all_rows.append(
                    {
                        "tag": tag,
                        "property": prop,
                        "series_name": sname,
                        "segment_index": segment_index,
                        "file_path": path,
                        "time_ps": t_new,
                        "value": y_val,
                    }
                )

        time_offset += max(duration, 0.0)
        segment_index += 1

    if not all_rows:
        logger.warning("No data rows for group (tag=%s, prop=%s)", tag, prop)
        return pd.DataFrame(
            columns=[
                "tag",
                "property",
                "series_name",
                "segment_index",
                "file_path",
                "time_ps",
                "value",
            ]
        )

    df = pd.DataFrame(all_rows)
    return df


# ======================================================================
# 시각화
# ======================================================================
def plot_group_timeseries(
    df: pd.DataFrame,
    tag: str,
    prop: str,
    out_dir: str,
    logger: logging.Logger,
):
    """
    하나의 (tag, property) 그룹에 대한 시계열 그래프를 PNG로 저장.
    - 일반 속성: x축 time_ps, y축 value, series_name별로 한 figure.
    - CELL 속성: 
        * Length 계열(Length A/B/C 등)만 따로 한 figure
        * Angle 계열(Angle alpha/beta/gamma 등)만 따로 한 figure
    """
    if df.empty:
        logger.info("Empty DataFrame for (tag=%s, prop=%s), skip plotting.", tag, prop)
        return

    os.makedirs(out_dir, exist_ok=True)

    # wide-format 변환: index=time_ps, columns=series_name
    pivot = df.pivot_table(
        index="time_ps",
        columns="series_name",
        values="value",
        aggfunc="mean",
    )

    tag_slug = slugify(tag)
    prop_slug = slugify(prop)
    prop_lower = prop.lower()

    # ==================================================================
    # CELL 특수 처리: Length vs Angle 따로 그림
    # ==================================================================
    if prop_lower == "cell":
        # 시리즈 이름으로 길이 / 각도 구분
        length_cols = [
            c for c in pivot.columns
            if "length" in str(c).lower()
        ]
        angle_cols = [
            c for c in pivot.columns
            if (
                "angle" in str(c).lower()
                or "alpha" in str(c).lower()
                or "beta" in str(c).lower()
                or "gamma" in str(c).lower()
            )
        ]

        # 1) Cell Length (A, B, C 등)
        if length_cols:
            pivot_len = pivot[length_cols]

            plt.figure(figsize=(10, 6))
            for col in pivot_len.columns:
                plt.plot(pivot_len.index, pivot_len[col], label=str(col))

            plt.xlabel("Time (ps)")
            plt.ylabel("Cell length (Å)")
            plt.title(f"{tag} - Cell Lengths")
            plt.legend(loc="best")
            plt.tight_layout()

            out_path_len = os.path.join(out_dir, f"{tag_slug}__{prop_slug}_lengths.png")
            plt.savefig(out_path_len, dpi=200)
            plt.close()
            logger.info(
                "Saved CELL length plot: %s (cols=%s)",
                out_path_len,
                ", ".join(map(str, length_cols)),
            )

        else:
            logger.info(
                "No CELL length series for (tag=%s, prop=%s).", tag, prop
            )

        # 2) Cell Angles (alpha, beta, gamma 등)
        if angle_cols:
            pivot_ang = pivot[angle_cols]

            plt.figure(figsize=(10, 6))
            for col in pivot_ang.columns:
                plt.plot(pivot_ang.index, pivot_ang[col], label=str(col))

            plt.xlabel("Time (ps)")
            plt.ylabel("Cell angle (deg)")
            plt.title(f"{tag} - Cell Angles")
            plt.legend(loc="best")
            plt.tight_layout()

            out_path_ang = os.path.join(out_dir, f"{tag_slug}__{prop_slug}_angles.png")
            plt.savefig(out_path_ang, dpi=200)
            plt.close()
            logger.info(
                "Saved CELL angle plot: %s (cols=%s)",
                out_path_ang,
                ", ".join(map(str, angle_cols)),
            )

        else:
            logger.info(
                "No CELL angle series for (tag=%s, prop=%s).", tag, prop
            )

        # 길이/각도 둘 다 못 찾은 경우는 fallback으로 generic 그리기
        if not length_cols and not angle_cols:
            logger.warning(
                "CELL series could not be split into length/angle for (tag=%s, prop=%s). "
                "Fallback to generic single figure.",
                tag,
                prop,
            )
        else:
            # CELL에 대해 length/angle 하나라도 그렸으면 여기서 종료
            return

    # ==================================================================
    # 일반 속성: 기존과 동일하게 한 figure에 series_name들 모두 그림
    # ==================================================================
    plt.figure(figsize=(10, 6))
    for col in pivot.columns:
        plt.plot(pivot.index, pivot[col], label=str(col))

    plt.xlabel("Time (ps)")
    plt.ylabel(prop)
    plt.title(f"{tag} - {prop}")
    plt.legend(loc="best")
    plt.tight_layout()

    out_path = os.path.join(out_dir, f"{tag_slug}__{prop_slug}.png")
    plt.savefig(out_path, dpi=200)
    plt.close()

    logger.info("Saved plot: %s", out_path)

# ======================================================================
# 메인
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Merge recursive Forcite XCD (NVT/NPT restarts) into continuous timeseries."
    )
    parser.add_argument(
        "--root",
        type=str,
        required=True,
        help="Root directory to search for .xcd files.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Max depth (relative to root) to recurse. None for unlimited.",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="./forcite_merged",
        help="Output directory for CSV/plots.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Optional log file path.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging to console.",
    )

    args = parser.parse_args()
    logger = setup_logger(log_file=args.log_file, verbose=args.verbose)

    root = args.root
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)

    logger.info("=== Forcite XCD merger started ===")
    logger.info("Root: %s", root)
    logger.info("Max depth: %s", args.max_depth)
    logger.info("Out dir: %s", out_dir)

    # 1) XCD 파일 목록 수집
    xcd_files = walk_xcd_files(root=root, max_depth=args.max_depth, logger=logger)
    if not xcd_files:
        logger.error("No XCD files found under root=%s", root)
        return

    # 2) (tag, property) 그룹핑
    groups: Dict[Tuple[str, str], List[str]] = {}
    for path in tqdm(xcd_files, desc="Grouping XCD files", unit="file"):
        tag, prop = extract_tag_property_from_filename(path)
        key = (tag, prop)
        groups.setdefault(key, []).append(path)

    logger.info("Total groups (tag, property): %d", len(groups))

    all_df_list: List[pd.DataFrame] = []

    # 3) 각 그룹에 대해 merge + CSV + plot
    for (tag, prop), files in tqdm(groups.items(), desc="Merging groups", unit="group"):
        logger.info(
            "Processing group tag=%s, prop=%s (n_files=%d)", tag, prop, len(files)
        )
        df_group = merge_segments_for_group(tag=tag, prop=prop, files=files, logger=logger)

        if df_group.empty:
            continue

        all_df_list.append(df_group)

        # 그룹별 CSV 저장
        tag_slug = slugify(tag)
        prop_slug = slugify(prop)
        csv_path = os.path.join(out_dir, f"{tag_slug}__{prop_slug}.csv")
        df_group.to_csv(csv_path, index=False)
        logger.info("Saved CSV: %s (rows=%d)", csv_path, len(df_group))

        # 그룹별 plot
        plot_group_timeseries(
            df=df_group,
            tag=tag,
            prop=prop,
            out_dir=out_dir,
            logger=logger,
        )

    # 4) 전체 long-format CSV (옵션)
    if all_df_list:
        df_all = pd.concat(all_df_list, ignore_index=True)
        all_csv_path = os.path.join(out_dir, "all_groups_timeseries_long.csv")
        df_all.to_csv(all_csv_path, index=False)
        logger.info("Saved ALL-groups CSV: %s (rows=%d)", all_csv_path, len(df_all))
    else:
        logger.warning("No non-empty groups to merge.")

    logger.info("=== Forcite XCD merger finished ===")


if __name__ == "__main__":
    main()

