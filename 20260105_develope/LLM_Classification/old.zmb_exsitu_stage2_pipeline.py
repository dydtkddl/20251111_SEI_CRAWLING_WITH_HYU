#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
2-Stage LLM Screening Pipeline for Aqueous Zn Metal Battery Ex-situ Protective Layer Papers
(Title + Abstract only, no Intro/Conclusion)

- 한 논문(row)을 Stage1 → Stage2 순서로 처리한 뒤 다음 논문으로 진행
- 각 Stage는 Ollama를 한 번씩 호출
- Stage1/2에서 탈락하면 이후 Stage는 스킵 (CSV에는 NA로 채움)
- logging + tqdm 사용

입력 CSV 예시 컬럼:
  - source_file
  - dc_title
  - abstract
  - publicationName
  - pubType
  ...

결과:
  1) per-paper 폴더에 stage별 input.txt / prompt.txt / output_raw.txt / result.json 저장
  2) 전체 요약 CSV (Stage1/2 결과 + reason + flags 등)
  3) S2_candidate_exsitu: Stage1=YES & Stage2(ex-situ+lab)=YES 인 경우 YES
"""

import argparse
import logging
import subprocess
import os
import sys
import json
import re
import pandas as pd
from tqdm import tqdm

# ============================================================
# Logging 설정
# ============================================================
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/zmb_exsitu_pipeline_stage2_all.log", mode="w", encoding="utf-8"),
    ],
)

fail_logger = logging.getLogger("fail_logger")
fail_handler = logging.FileHandler("logs/zmb_exsitu_pipeline_stage2_fail.log", mode="w", encoding="utf-8")
fail_logger.addHandler(fail_handler)
fail_logger.setLevel(logging.INFO)


# ============================================================
# Ollama runner
# ============================================================
def run_ollama(model: str, full_prompt: str) -> str:
    """
    Ollama를 subprocess로 호출하고 stdout을 문자열로 반환.
    --format json 옵션 사용.
    """
    cmd = [
        "ollama",
        "run",
        model,
        "--format",
        "json",
    ]

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    out, err = process.communicate(full_prompt)

    if err:
        err_str = err.strip()
        if err_str:
            logging.warning(f"Ollama STDERR: {err_str}")

    return out.strip() if out else ""


# ============================================================
# JSON 파서 (느슨한 모드)
# ============================================================
def parse_json_loose(text: str):
    """
    LLM이 JSON만 출력하도록 시켜도 앞뒤에 노이즈가 붙을 수 있으므로,
    첫 '{'와 마지막 '}' 사이를 잘라서 파싱 시도.
    """
    if not text:
        raise ValueError("Empty output from model")

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in text")

    json_str = text[start : end + 1]
    return json.loads(json_str)


# ============================================================
# Safe folder name
# ============================================================
def safe_folder_name(name: str) -> str:
    name = str(name).strip()
    return re.sub(r"[^\w\-.]", "_", name)


# ============================================================
# Stage별 프롬프트 템플릿 (Title + Abstract 중심)
# ============================================================

# ---------- Stage 1: Aqueous Zn metal battery 도메인 필터 ----------
STAGE1_PROMPT_TEMPLATE = """You are an expert in aqueous zinc metal batteries (AZMB).

Goal
----
Decide whether the given paper is primarily about rechargeable aqueous zinc-based batteries that use a metallic Zn anode.

Definition and scope
--------------------
Treat a paper as AZMB-related (answer "YES") only if ALL of the following are true:
- The active anode material is metallic zinc (Zn foil, Zn plate, Zn powder, 3D Zn host, etc.).
- The electrochemical cell operates in an aqueous electrolyte (salt dissolved in water, including water-in-salt systems).
- The main topic is energy storage (rechargeable zinc batteries, zinc ion batteries, zinc metal batteries, zinc hybrid capacitors, etc.).

Answer "YES" also when:
- The system is called a "zinc ion hybrid capacitor" or "aqueous zinc ion capacitor" but still uses a Zn metal anode and aqueous electrolyte.

Answer "NO" if:
- The main system is Li/Na/K/Mg/Ca/Al batteries (non-zinc).
- The zinc chemistry is non-aqueous (organic solvent, polymer gel without clear water, ionic liquid) with no clear water-based electrolyte.
- The focus is on Zn corrosion, plating, sensing, photocatalysis, or other electrochemistry not directly targeting rechargeable aqueous zinc batteries.
- The Zn species are only in the cathode host (e.g., Zn2+ intercalation into MnO2) without using a Zn metal anode.

Input
-----
You will see a short metadata block plus title and abstract.

Here is the text block for this paper:

<<<INPUT_BLOCK>>>

Task
----
Return ONLY this JSON object:

{
  "is_aqueous_zmb": "YES" or "NO",
  "reason": "<ONE short sentence that explains your decision>"
}

Now, based on ALL the rules above and ONLY the given text block, return ONLY the JSON object, with no additional text.
"""

# ---------- Stage 2: ex-situ 보호층 + 랩스케일 실험 ----------
STAGE2_PROMPT_TEMPLATE = """You are an expert in ex-situ protective coatings for aqueous zinc metal batteries.

Goal
----
Among papers that are already known to be about aqueous zinc metal batteries, decide whether the work focuses on an EX-SITU protective layer on the Zn metal anode and whether new lab-scale experiments are reported.

Constraints
-----------
You will only see the title, abstract, and simple metadata (journal name, publication type).
Make your best judgment based on this limited information.

Definitions
-----------
"Ex-situ protective layer" means any artificial SEI, coating, interlayer, host structure, or film that is fabricated on the Zn metal surface (or between Zn and separator) BEFORE the cell is assembled.

Examples:
- polymer coating, inorganic coating, MOF layer, carbon layer, 3D scaffold filled with Zn,
- artificial SEI formed by dip-coating / spin-coating / casting / electrodeposition and then assembled into a cell,
- protective interlayer between Zn and separator that is fabricated prior to cycling.

"In-situ SEI or additive" means layers that form spontaneously during cycling due to electrolyte additives or reactions of the electrolyte itself; the Zn surface is not pre-coated before assembly.

When to answer "YES" for has_exsitu_protective_layer:
- The title or abstract clearly mention an artificial SEI, protective coating/layer, interlayer, 3D host, or modified Zn surface that is prepared before cycling.
- Or, the text clearly suggests fabricating a layer on Zn metal (coating, sputtering, dip-coating, casting, etc.) prior to cell assembly.

When to answer "NO" for has_exsitu_protective_layer:
- The main modification is only electrolyte composition or additives (in-situ SEI).
- The main modification is cathode material, separator, or current collector, not the Zn metal anode.
- The work is purely modeling/simulation without a real coating.
- The paper is a general review without a specific new ex-situ coating system.

Lab-scale experiments
---------------------
Answer "YES" for has_lab_scale_experiments if the title/abstract clearly indicate new experimental work on cells using Zn metal anodes, for example:
- symmetric Zn||Zn cells, Zn||MnO2 full cells, coin cells, pouch cells, or three-electrode cells;
- phrases like "we fabricated", "we prepared", "we demonstrated", "electrochemical performance was evaluated", etc.

Answer "NO" for has_lab_scale_experiments if:
- the paper is mainly theoretical, modeling, DFT-only, or simulation-only;
- the work is a review, perspective, or purely conceptual proposal with no new experiments (e.g., publication type = review, abstract clearly says it's a review).

Modification focus
------------------
Set modification_focus as one of:

- "ZN_EX_SITU_LAYER"           → Clearly about ex-situ protective coating/interlayer/host on Zn metal anode.
- "ZN_IN_SITU_SEI_OR_ADDITIVE" → Primarily electrolyte/additive-driven SEI formation on Zn (no pre-coating).
- "ELECTROLYTE_ONLY"           → Mainly electrolyte formulation, with no special Zn surface engineering.
- "CATHODE_OR_SEPARATOR_MOD"   → Main modification is cathode, separator, or other components (not Zn anode).
- "OTHER"                      → Does not fall into the above categories, or too ambiguous.

Input
-----
Here is the text block for this paper:

<<<INPUT_BLOCK>>>

Task
----
Return ONLY this JSON object:

{
  "has_exsitu_protective_layer": "YES" or "NO",
  "has_lab_scale_experiments": "YES" or "NO",
  "modification_focus": "ZN_EX_SITU_LAYER" or "ZN_IN_SITU_SEI_OR_ADDITIVE" or "ELECTROLYTE_ONLY" or "CATHODE_OR_SEPARATOR_MOD" or "OTHER",
  "reason": "<ONE short sentence summarizing your decision>"
}

Now, based on ALL the rules above and ONLY the given text block, return ONLY the JSON object, with no additional text.
"""


# ============================================================
# 메인 파이프라인
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="2-Stage Ollama screening pipeline for aqueous Zn metal battery ex-situ protective layer papers (Title+Abstract only)"
    )

    parser.add_argument("--csv", required=True, help="Input CSV file")
    parser.add_argument("--title_col", default="dc_title", help="Title column name (default: dc_title)")
    parser.add_argument("--abstract_col", default="abstract", help="Abstract column name (default: abstract)")
    parser.add_argument("--sf_col", default="source_file", help="Column used for folder naming (default: source_file)")
    parser.add_argument(
        "--journal_col", default="publicationName", help="Optional journal name column (default: publicationName)"
    )
    parser.add_argument(
        "--pubtype_col", default="pubType", help="Optional publication type column (default: pubType)"
    )

    parser.add_argument("--model", default="qwen3:30b-a3b-instruct", help="Ollama model name")
    parser.add_argument("--outdir", default="results_zmb_exsitu_stage2", help="Root dir for per-paper results")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of rows (for testing)")
    parser.add_argument(
        "--summary_csv",
        default="zmb_exsitu_stage2_summary.csv",
        help="Output summary CSV name",
    )

    args = parser.parse_args()

    # CSV 로드
    logging.info(f"Loading CSV: {args.csv}")
    df = pd.read_csv(args.csv)

    # 필수 컬럼 체크
    for col in [args.title_col, args.abstract_col, args.sf_col]:
        if col not in df.columns:
            logging.error(f"Column '{col}' not found in CSV!")
            return

    # optional 컬럼 없으면 빈 문자열로 처리
    if args.journal_col not in df.columns:
        logging.warning(f"Journal column '{args.journal_col}' not found. Will use empty string.")
        df[args.journal_col] = ""
    if args.pubtype_col not in df.columns:
        logging.warning(f"PubType column '{args.pubtype_col}' not found. Will use empty string.")
        df[args.pubtype_col] = ""

    if args.limit:
        df = df.head(args.limit)
        logging.info(f"Row limit set: {args.limit}, processing first {len(df)} rows")

    os.makedirs(args.outdir, exist_ok=True)

    # 요약 결과 저장용 리스트
    summary_rows = []

    # row-wise 처리
    for idx, row in tqdm(df.iterrows(), total=len(df), desc="Processing papers", unit="paper"):
        source_name_raw = str(row[args.sf_col])
        source_name = safe_folder_name(source_name_raw)
        run_dir = os.path.join(args.outdir, source_name)
        os.makedirs(run_dir, exist_ok=True)

        # 안전하게 NaN 처리
        title_val = row[args.title_col]
        abstract_val = row[args.abstract_col]
        journal_val = row[args.journal_col]
        pubtype_val = row[args.pubtype_col]

        title = "" if pd.isna(title_val) else str(title_val).strip()
        abstract = "" if pd.isna(abstract_val) else str(abstract_val).strip()
        journal = "" if pd.isna(journal_val) else str(journal_val).strip()
        pubtype = "" if pd.isna(pubtype_val) else str(pubtype_val).strip()

        # ===== 입력 블록 구성 (Title + Abstract + 약간의 메타데이터) =====
        input_block = (
            f"source_file: {source_name_raw}\n"
            f"journal: {journal}\n"
            f"pubType: {pubtype}\n"
            f"Title: {title}\n"
            f"Abstract: {abstract}"
        )

        # 요약 dict 초기값
        summary = {
            "source_file": source_name_raw,
            "journal": journal,
            "pubType": pubtype,
            # Stage 1
            "S1_is_aqueous_zmb": "NA",
            "S1_reason": "",
            # Stage 2
            "S2_has_exsitu_protective_layer": "NA",
            "S2_has_lab_scale_experiments": "NA",
            "S2_modification_focus": "",
            "S2_reason": "",
            # 간단 pass 플래그
            "S2_candidate_exsitu": "NO",  # Stage1=YES & ex-situ=YES & lab=YES 이면 YES로 설정
        }

        # ----------------------------------------------------
        # Stage 1 : Aqueous Zn metal battery 도메인 필터
        # ----------------------------------------------------
        try:
            prompt_s1 = STAGE1_PROMPT_TEMPLATE.replace("<<<INPUT_BLOCK>>>", input_block)

            # 저장: 입력 / 프롬프트
            with open(os.path.join(run_dir, "stage1_input.txt"), "w", encoding="utf-8") as f:
                f.write(input_block)
            with open(os.path.join(run_dir, "stage1_prompt.txt"), "w", encoding="utf-8") as f:
                f.write(prompt_s1)

            out_s1 = run_ollama(args.model, prompt_s1)
            with open(os.path.join(run_dir, "stage1_output_raw.txt"), "w", encoding="utf-8") as f:
                f.write(out_s1)

            data_s1 = parse_json_loose(out_s1)
            with open(os.path.join(run_dir, "stage1_result.json"), "w", encoding="utf-8") as f:
                json.dump(data_s1, f, ensure_ascii=False, indent=2)

            s1_flag = str(data_s1.get("is_aqueous_zmb", "")).upper()
            s1_reason = str(data_s1.get("reason", "")).strip()

            summary["S1_is_aqueous_zmb"] = s1_flag if s1_flag in ["YES", "NO"] else "NA"
            summary["S1_reason"] = s1_reason

        except Exception as e:
            fail_logger.info(f"{source_name_raw} | STAGE1_ERROR: {str(e)}")
            logging.error(f"[Row {idx}] Stage1 failed for {source_name_raw}: {e}")
            summary_rows.append(summary)
            continue

        # Stage1에서 NO면 Stage2 스킵
        if summary["S1_is_aqueous_zmb"] != "YES":
            logging.info(f"[Row {idx}] Stage1 != YES → skip Stage2 for {source_name_raw}")
            summary_rows.append(summary)
            continue

        # ----------------------------------------------------
        # Stage 2 : ex-situ 보호층 + 랩스케일 실험 여부
        # ----------------------------------------------------
        try:
            prompt_s2 = STAGE2_PROMPT_TEMPLATE.replace("<<<INPUT_BLOCK>>>", input_block)

            with open(os.path.join(run_dir, "stage2_input.txt"), "w", encoding="utf-8") as f:
                f.write(input_block)
            with open(os.path.join(run_dir, "stage2_prompt.txt"), "w", encoding="utf-8") as f:
                f.write(prompt_s2)

            out_s2 = run_ollama(args.model, prompt_s2)
            with open(os.path.join(run_dir, "stage2_output_raw.txt"), "w", encoding="utf-8") as f:
                f.write(out_s2)

            data_s2 = parse_json_loose(out_s2)
            with open(os.path.join(run_dir, "stage2_result.json"), "w", encoding="utf-8") as f:
                json.dump(data_s2, f, ensure_ascii=False, indent=2)

            s2_exsitu = str(data_s2.get("has_exsitu_protective_layer", "")).upper()
            s2_lab = str(data_s2.get("has_lab_scale_experiments", "")).upper()
            s2_focus = str(data_s2.get("modification_focus", "")).strip()
            s2_reason = str(data_s2.get("reason", "")).strip()

            summary["S2_has_exsitu_protective_layer"] = s2_exsitu if s2_exsitu in ["YES", "NO"] else "NA"
            summary["S2_has_lab_scale_experiments"] = s2_lab if s2_lab in ["YES", "NO"] else "NA"
            summary["S2_modification_focus"] = s2_focus
            summary["S2_reason"] = s2_reason

            # 간단 pass 플래그 계산
            if (
                summary["S1_is_aqueous_zmb"] == "YES"
                and summary["S2_has_exsitu_protective_layer"] == "YES"
                and summary["S2_has_lab_scale_experiments"] == "YES"
            ):
                summary["S2_candidate_exsitu"] = "YES"
            else:
                summary["S2_candidate_exsitu"] = "NO"

        except Exception as e:
            fail_logger.info(f"{source_name_raw} | STAGE2_ERROR: {str(e)}")
            logging.error(f"[Row {idx}] Stage2 failed for {source_name_raw}: {e}")
            summary_rows.append(summary)
            continue

        # 최종 요약 push
        summary_rows.append(summary)

    # ============================================================
    # 요약 CSV 저장
    # ============================================================
    summary_df = pd.DataFrame(summary_rows)
    summary_path = os.path.abspath(args.summary_csv)
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    logging.info(f"Saved ZMB ex-situ Stage2 summary CSV to: {summary_path}")
    logging.info("All papers processed.")


if __name__ == "__main__":
    main()

