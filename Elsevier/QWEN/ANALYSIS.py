#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Analysis script for ZMB ex-situ Stage2 pipeline results.

작동 방식:
- results_zmb_exsitu_stage2/ 아래의 모든 폴더를 스캔
- stage1_result.json, stage2_result.json 읽음
- YES/NO/ratio 계산 및 요약 출력

출력:
- 전체 개수
- Stage1 YES 비율
- Stage2까지 통과 비율
- Stage2 필드별 통계
"""

import os
import json
from glob import glob
from collections import Counter
import pandas as pd

ROOT = "results_zmb_exsitu_stage2"

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return None


def main():

    folders = sorted([d for d in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, d))])

    print(f"총 논문 폴더 수: {len(folders)}")

    rows = []

    for folder in folders:
        fdir = os.path.join(ROOT, folder)

        s1_path = os.path.join(fdir, "stage1_result.json")
        s2_path = os.path.join(fdir, "stage2_result.json")

        s1 = load_json(s1_path)
        s2 = load_json(s2_path)

        row = {
            "source_file": folder,
            "S1": None,
            "S2_exsitu": None,
            "S2_lab": None,
            "S2_focus": None
        }

        # ---------------- Stage 1 ----------------
        if s1:
            row["S1"] = s1.get("is_aqueous_zmb")

        # ---------------- Stage 2 ----------------
        if s2:
            row["S2_exsitu"] = s2.get("has_exsitu_protective_layer")
            row["S2_lab"] = s2.get("has_lab_scale_experiments")
            row["S2_focus"] = s2.get("modification_focus")

        rows.append(row)

    df = pd.DataFrame(rows)

    print("\n=== DataFrame Loaded ===")
    print(df.head())

    # ==========================================================
    # Stage 1 분석
    # ==========================================================
    total = len(df)
    s1_yes = (df["S1"] == "YES").sum()
    s1_ratio = s1_yes / total if total>0 else 0

    print("\n=== Stage 1 Summary ===")
    print(f"Total papers: {total}")
    print(f"Stage1 YES: {s1_yes} ({s1_ratio:.2%})")

    # ==========================================================
    # Stage 2 통과 조건
    # ==========================================================
    s2_pass = df[
        (df["S1"] == "YES") &
        (df["S2_exsitu"] == "YES") &
        (df["S2_lab"] == "YES")
    ]
    s2_pass_count = len(s2_pass)
    s2_pass_ratio = s2_pass_count / total if total>0 else 0

    print("\n=== Stage 2 Summary ===")
    print(f"Stage2 PASS (ex-situ + lab): {s2_pass_count} ({s2_pass_ratio:.2%})")

    # ==========================================================
    # Stage2 필드별 통계
    # ==========================================================

    print("\n=== Stage 2 Field Stats ===")
    for col in ["S2_exsitu", "S2_lab", "S2_focus"]:
        print(f"\n-- {col} --")
        print(df[col].value_counts(dropna=False))

    # ==========================================================
    # PASS 리스트 출력
    # ==========================================================

    print("\n=== Stage2 PASS Files ===")
    for x in s2_pass["source_file"].tolist()[:20]:
        print(x)
    if len(s2_pass) > 20:
        print(f"... (+{len(s2_pass)-20}) more")

    # ==========================================================
    # 저장
    # ==========================================================

    df.to_csv("analysis_stage2_summary.csv", index=False, encoding="utf-8-sig")
    s2_pass.to_csv("analysis_stage2_pass.csv", index=False, encoding="utf-8-sig")

    print("\nSaved:")
    print(" - analysis_stage2_summary.csv")
    print(" - analysis_stage2_pass.csv")

    print("\nDone.")


if __name__ == "__main__":
    main()

