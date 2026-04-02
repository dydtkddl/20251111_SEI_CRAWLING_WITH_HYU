#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
도메인 전문가용 S1/S2 검수 Streamlit 앱 (투박한 연구실 스타일 UI + 자동 저장)

기능
----
1) CSV 로딩 (경로 고정: CSV_PATH)
   - 시작 시 autosave 파일이 있으면, 그 라벨(S1, S2_exsitu, S2_lab, S2_focus) 우선 적용
2) 규칙에 따라 샘플링된 5개 그룹을 탭으로 표시
   - 그룹 A: S1 == NO                           (Stage1 컷)
   - 그룹 B: S1 == YES & S2_exsitu = NEG & S2_lab = NEG
   - 그룹 C1: S1 == YES & S2_exsitu = POS & S2_lab = NEG
   - 그룹 C2: S1 == YES & S2_exsitu = NEG & S2_lab = POS
   - 그룹 D: S1 == YES & S2_exsitu = POS & S2_lab = POS
   * Stage2 라벨은 아래 매핑으로 POS/NEG/NA로 일반화
     - POS: OK/YES/Y/TRUE/T/1
     - NEG: X/NO/N/FALSE/F/0
     - NA : 그 외, NaN, 빈 문자열 등
3) 각 논문별 카드형 블록 UI
   - 왼쪽: Title / Abstract / DOI / ScienceDirect 링크
   - 오른쪽: S1, S2_exsitu, S2_lab, S2_focus 라벨 선택 박스
4) 라벨 의미 상단에서 텍스트로 설명
5) 모든 선택값을 매 rerun마다 autosave CSV(AUTOSAVE_PATH)에 자동 저장
6) 필요시 버튼으로 수동 다운로드도 가능
7) logging + tqdm 로 진행 상황 로그
"""

import io
import logging
from pathlib import Path
from typing import Optional, List

import numpy as np
import pandas as pd
import streamlit as st
from tqdm import tqdm

# ============================================================
# 설정
# ============================================================
RANDOM_SEED = 42
CSV_PATH = "../post_processed_results.csv"               # 원본
AUTOSAVE_PATH = "../post_processed_results_autosave.csv" # 자동 저장용

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)
np.random.seed(RANDOM_SEED)


# ============================================================
# 유틸 함수
# ============================================================
def find_first_existing_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    """후보 리스트 중 실제 DataFrame에 존재하는 첫 번째 컬럼명을 반환."""
    for col in candidates:
        if col in df.columns:
            return col
    return None


def make_doi_url(doi: str) -> Optional[str]:
    """prism_doi / dc_identifier에서 DOI 문자열을 받아 클릭 가능한 URL 생성."""
    if not isinstance(doi, str):
        return None
    doi_clean = doi.strip()
    if doi_clean.lower().startswith("doi:"):
        doi_clean = doi_clean[4:]
    doi_clean = doi_clean.strip()
    if not doi_clean:
        return None
    return f"https://doi.org/{doi_clean}"


def html_escape(text: str) -> str:
    """아주 간단한 HTML escape."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def get_label_options(series: pd.Series, add_na_label: bool = False) -> List[str]:
    """
    라벨 후보 리스트 생성 (NaN 제외).
    add_na_label=True 이면 'NA' (평가 안됨) 옵션을 추가.
    """
    vals = sorted(set(str(v) for v in series.dropna().unique()))
    if add_na_label:
        if "NA" not in vals:
            vals = ["NA"] + vals
    return vals


def normalize_stage1_label(val) -> str:
    """
    S1 라벨을 YES/NO 두 가지로 정규화.
    - YES 계열: YES, Y, OK, TRUE, T, 1
    - 나머지는 전부 NO 취급
    """
    if not isinstance(val, str):
        return "NO"
    s = val.strip().upper()
    if s in {"YES", "Y", "OK", "TRUE", "T", "1"}:
        return "YES"
    return "NO"


def normalize_stage2_label(val) -> str:
    """
    S2_exsitu / S2_lab 라벨을 POS/NEG/NA로 정규화.
    - POS: OK/YES/Y/TRUE/T/1
    - NEG: X/NO/N/FALSE/F/0
    - NA : 그 외, NaN, "", NA, NAN, NONE 등
    """
    if not isinstance(val, str):
        return "NA"
    s = val.strip().upper()
    if s in {"OK", "YES", "Y", "TRUE", "T", "1"}:
        return "POS"
    if s in {"X", "NO", "N", "FALSE", "F", "0"}:
        return "NEG"
    if s in {"NA", "NAN", "NONE", ""}:
        return "NA"
    return "NA"


def render_group(
    name: str,
    df_group: pd.DataFrame,
    total_count: int,
    s1_options: List[str],
    s2_ex_options: List[str],
    s2_lab_options: List[str],
    s2_focus_options: List[str],
    title_col: str,
    abstract_col: Optional[str],
    doi_col: Optional[str],
    scidir_col: Optional[str],
):
    """한 그룹(탭) 안에서 논문 리스트 렌더링 (카드 UI)"""
    st.markdown(f"#### {name}")
    st.caption(
        f"조건을 만족하는 전체 논문: {total_count}편 · "
        f"이 탭에서 샘플링된 논문: {len(df_group)}편"
    )

    if len(df_group) == 0:
        st.warning("해당 조건을 만족하는 논문이 없습니다.")
        return

    for idx, row in tqdm(
        df_group.iterrows(),
        total=len(df_group),
        desc=f"Rendering group {name}",
    ):
        with st.container():
            st.markdown('<div class="paper-block">', unsafe_allow_html=True)

            meta_col, label_col = st.columns([2.5, 1.5])

            # ----------------- 왼쪽: 제목/초록/링크 -----------------
            with meta_col:
                title = str(row.get(title_col, ""))
                title_html = html_escape(title)

                st.markdown(
                    f"""
                    <div class="paper-header">
                        <div class="paper-index">Index: {idx}</div>
                        <div class="paper-title">{title_html}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                if abstract_col is not None:
                    abstract = str(row.get(abstract_col, ""))
                    if abstract.strip():
                        st.markdown(
                            '<div class="paper-section-title">Abstract</div>',
                            unsafe_allow_html=True,
                        )
                        st.write(abstract)
                    else:
                        st.info("Abstract 없음 / 비어 있음")
                else:
                    st.info("Abstract 컬럼을 찾지 못했습니다.")

                # DOI / ScienceDirect 링크
                doi_url = None
                if doi_col is not None:
                    doi_url = make_doi_url(row.get(doi_col, ""))

                scidir_url = None
                if scidir_col is not None:
                    scidir_url = row.get(scidir_col, None)

                link_parts = []
                if doi_url:
                    link_parts.append(f"[DOI 링크]({doi_url})")
                if isinstance(scidir_url, str) and scidir_url.strip():
                    link_parts.append(f"[ScienceDirect]({scidir_url.strip()})")

                if link_parts:
                    st.markdown(
                        "<div class='paper-links'>"
                        + " · ".join(link_parts)
                        + "</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption("유효한 DOI / ScienceDirect 링크 없음")

            # ----------------- 오른쪽: 라벨 패널 -----------------
            with label_col:
                st.markdown('<div class="label-panel">', unsafe_allow_html=True)

                # 현재 값 (NaN → 'NA')
                cur_s1 = str(row.get("S1")) if not pd.isna(row.get("S1")) else None

                if pd.isna(row.get("S2_exsitu")):
                    cur_s2_ex = "NA"
                else:
                    cur_s2_ex = str(row.get("S2_exsitu"))

                if pd.isna(row.get("S2_lab")):
                    cur_s2_lab = "NA"
                else:
                    cur_s2_lab = str(row.get("S2_lab"))

                if pd.isna(row.get("S2_focus")):
                    cur_s2_focus = "NA"
                else:
                    cur_s2_focus = str(row.get("S2_focus"))

                s1_key = f"s1_{idx}"
                s2_ex_key = f"s2_ex_{idx}"
                s2_lab_key = f"s2_lab_{idx}"
                s2_focus_key = f"s2_focus_{idx}"

                st.markdown(
                    "<div class='label-panel-title'>Re-labeling</div>",
                    unsafe_allow_html=True,
                )

                # S1
                st.selectbox(
                    "S1 (1단계: 수계 Zn 금속 배터리 + 실험 논문 여부)",
                    options=s1_options,
                    index=s1_options.index(cur_s1) if cur_s1 in s1_options else 0,
                    key=s1_key,
                    help=(
                        "YES: 수계 아연 금속 배터리(Zn metal anode) 관련 실험 논문\n"
                        "NO: 그 외 (리뷰, 다른 전지계, 시뮬레이션-only 등)"
                    ),
                )

                # S2_exsitu
                st.selectbox(
                    "S2_exsitu (2-1: Ex-situ 보호층 존재 여부)",
                    options=s2_ex_options,
                    index=s2_ex_options.index(cur_s2_ex)
                    if cur_s2_ex in s2_ex_options
                    else 0,
                    key=s2_ex_key,
                    help="OK: ex-situ 보호층/코팅 존재 · X: 없음 · NA: 평가 안 됨",
                )

                # S2_lab
                st.selectbox(
                    "S2_lab (2-2: 전기화학 실험 수행 여부)",
                    options=s2_lab_options,
                    index=s2_lab_options.index(cur_s2_lab)
                    if cur_s2_lab in s2_lab_options
                    else 0,
                    key=s2_lab_key,
                    help="OK: 대칭셀·풀셀 등 실험 있음 · X: 실험 없음 · NA: 평가 안 됨",
                )

                # S2_focus
                st.selectbox(
                    "S2_focus (2-3: 논문 핵심 포커스)",
                    options=s2_focus_options,
                    index=s2_focus_options.index(cur_s2_focus)
                    if cur_s2_focus in s2_focus_options
                    else 0,
                    key=s2_focus_key,
                    help="OK: Zn ex-situ 보호층이 메인 주제 · X: 다른 게 메인 · NA: 평가 안 됨",
                )

                st.markdown("</div>", unsafe_allow_html=True)  # label-panel 닫기

            st.markdown("</div>", unsafe_allow_html=True)  # paper-block 닫기


def apply_edits_to_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    st.session_state에 들어있는 s1_*, s2_* 값들을
    원본 df에 반영한 새로운 DataFrame을 반환.
    'NA'는 NaN으로 다시 저장.
    """
    df_new = df.copy()

    logger.info("Applying edits from session_state to DataFrame...")
    for idx in tqdm(df.index, desc="Applying edits"):
        s1_key = f"s1_{idx}"
        s2_ex_key = f"s2_ex_{idx}"
        s2_lab_key = f"s2_lab_{idx}"
        s2_focus_key = f"s2_focus_{idx}"

        if s1_key in st.session_state:
            df_new.at[idx, "S1"] = st.session_state[s1_key]

        if s2_ex_key in st.session_state:
            val = st.session_state[s2_ex_key]
            df_new.at[idx, "S2_exsitu"] = np.nan if val == "NA" else val

        if s2_lab_key in st.session_state:
            val = st.session_state[s2_lab_key]
            df_new.at[idx, "S2_lab"] = np.nan if val == "NA" else val

        if s2_focus_key in st.session_state:
            val = st.session_state[s2_focus_key]
            df_new.at[idx, "S2_focus"] = np.nan if val == "NA" else val

    logger.info("Edits applied.")
    return df_new


# ============================================================
# 메인 Streamlit 앱
# ============================================================
def main():
    st.set_page_config(
        page_title="S1/S2 Expert Review Tool",
        layout="wide",
        page_icon=None,
    )

    # --------------------------------------------------------
    # 전역 스타일 (모바일 / 다크모드 대비 포함)
    # --------------------------------------------------------
    st.markdown(
        """
        <style>
        /* 상단 메뉴/푸터 숨김 */
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        header {visibility: hidden;}

        /* 전역 텍스트 색: 다크/라이트 테마 상관없이 진한 회색 */
        html, body, [class^="css"] {
            color: #111111 !important;
        }

        /* 메인 영역 배경 */
        div[data-testid="stAppViewContainer"] {
            background-color: #f5f5f7;
        }

        /* 사이드바 배경 */
        section[data-testid="stSidebar"] > div {
            background-color: #f0f0f2;
        }

        /* 마크다운 기본 텍스트 */
        div[data-testid="stMarkdownContainer"] {
            color: #111827 !important;
        }

        /* 논문 카드 */
        .paper-block {
            border: 1px solid #e0e0e6;
            padding: 0.9rem 1rem;
            margin-bottom: 0.9rem;
            background-color: #ffffff;
            border-radius: 0.5rem;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        }
        .paper-header {
            margin-bottom: 0.4rem;
        }
        .paper-index {
            font-size: 0.8rem;
            color: #6b7280;
            margin-bottom: 0.15rem;
        }
        .paper-title {
            font-weight: 600;
            font-size: 1.0rem;
            color: #111827;
        }
        .paper-section-title {
            font-size: 0.85rem;
            font-weight: 600;
            margin-top: 0.4rem;
            margin-bottom: 0.2rem;
            color: #374151;
        }
        .paper-links {
            font-size: 0.8rem;
            margin-top: 0.35rem;
        }
        .paper-links a {
            color: #2563eb;
            text-decoration: none;
        }
        .paper-links a:hover {
            text-decoration: underline;
        }

        /* 라벨 패널 */
        .label-panel {
            border-left: 1px solid #e0e0e6;
            padding-left: 0.75rem;
            margin-left: 0.3rem;
        }
        .label-panel-title {
            font-size: 0.85rem;
            font-weight: 600;
            margin-bottom: 0.3rem;
            color: #111827;
        }

        /* 탭 버튼 글자 크기 약간 줄이기 */
        button[data-baseweb="tab"] {
            font-size: 0.9rem;
        }

        /* 모바일 대응: 라벨 패널을 아래로 내리고 경계선 변경 */
        @media (max-width: 768px) {
            .paper-block {
                padding: 0.75rem 0.8rem;
            }
            .label-panel {
                border-left: none;
                border-top: 1px dashed #e5e7eb;
                margin-left: 0;
                margin-top: 0.6rem;
                padding-left: 0;
                padding-top: 0.5rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # 상단 헤더 (배너 있으면 사용)
    banner_path = Path("banner.png")
    if banner_path.exists():
        st.image(str(banner_path), use_column_width=False)
    else:
        st.markdown("### AZMB Ex-situ Protective Layer Paper Review")

    st.markdown("---")

    # 라벨 의미 안내
    with st.expander("S1 / S2 라벨 의미", expanded=True):
        st.markdown(
            """
**S1 (Stage 1 – 수계 Zn 금속 배터리 + 실험 논문 필터)**  
- YES: 수계 아연 금속 배터리(Zn metal anode) 관련 + 실험 연구  
- NO: 그 외 (리뷰, 다른 전지계, DFT-only, 공정 논문 등) → 이 경우 S2_*는 보통 NaN (평가 안 함)

**S2_exsitu (Stage 2-1 – Ex-situ 보호층 존재 여부)**  
- OK: Zn 표면에 ex-situ 보호층/코팅을 실제로 제작·적용  
- X: ex-situ 보호층 없음, in-situ additive만, 혹은 완전히 다른 초점  
- NA: 이 단계까지 평가되지 않음 (예: S1=NO였던 행 등)

**S2_lab (Stage 2-2 – 전기화학 실험 수행 여부)**  
- OK: 대칭셀/풀셀 등 전기화학 실험 데이터 존재  
- X: 실험 없이 리뷰/시뮬레이션/이론 위주  
- NA: 평가 안 됨

**S2_focus (Stage 2-3 – 논문 핵심 포커스)**  
- OK: 논문의 메인 포커스가 Zn 금속 ex-situ 보호층 설계/성능  
- X: 보호층은 부수적 언급, 다른 소재·개념이 메인  
- NA: 평가 안 됨
"""
        )

    # --------------------------------------------------------
    # CSV 로딩
    # --------------------------------------------------------
    logger.info("Loading original CSV from %s ...", CSV_PATH)
    try:
        df = pd.read_csv(CSV_PATH)
    except Exception as e:
        st.error(f"원본 CSV를 읽는 중 오류 발생: {e}")
        logger.exception("Error while loading original CSV")
        st.stop()

    logger.info("Original CSV loaded: shape=%s", df.shape)

    # autosave가 있으면 라벨 덮어쓰기
    auto_path = Path(AUTOSAVE_PATH)
    if auto_path.exists():
        try:
            df_auto = pd.read_csv(auto_path)
            if len(df_auto) == len(df):
                for col in ["S1", "S2_exsitu", "S2_lab", "S2_focus"]:
                    if col in df_auto.columns:
                        df[col] = df_auto[col]
                logger.info("Loaded autosaved labels from %s", AUTOSAVE_PATH)
            else:
                logger.warning(
                    "Autosave file length (%d) != original (%d). Ignoring autosave.",
                    len(df_auto),
                    len(df),
                )
        except Exception as e:
            logger.exception("Failed to load autosave file: %s", e)

    st.caption(f"자동 저장 경로: {AUTOSAVE_PATH}")

    # 필수 컬럼 체크
    required_cols = ["S1", "S2_exsitu", "S2_lab"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        st.error(f"필수 컬럼이 없습니다: {missing}")
        st.stop()

    # Title / Abstract / DOI / ScienceDirect 컬럼 탐색
    title_col = "dc_title" if "dc_title" in df.columns else None
    if title_col is None:
        st.error("제목 컬럼(dc_title)을 찾을 수 없습니다.")
        st.stop()

    abstract_candidates = ["abstract", "dc_description", "description", "abstractText"]
    abstract_col = find_first_existing_column(df, abstract_candidates)

    doi_candidates = ["prism_doi", "dc_identifier"]
    doi_col = find_first_existing_column(df, doi_candidates)

    scidir_candidates = ["link_scidir", "link_scidir_html", "link_fulltext"]
    scidir_col = find_first_existing_column(df, scidir_candidates)

    # S2_focus 없으면 빈 컬럼 추가
    if "S2_focus" not in df.columns:
        df["S2_focus"] = np.nan

    # --------------------------------------------------------
    # 라벨 옵션(실제 데이터에 존재하는 값 기준)
    # --------------------------------------------------------
    s1_options = get_label_options(df["S1"]) or ["NO", "YES"]
    s2_ex_options = get_label_options(df["S2_exsitu"], add_na_label=True) or [
        "NA",
        "X",
        "OK",
    ]
    s2_lab_options = get_label_options(df["S2_lab"], add_na_label=True) or [
        "NA",
        "X",
        "OK",
    ]
    s2_focus_options = get_label_options(df["S2_focus"], add_na_label=True) or [
        "NA",
        "X",
        "OK",
    ]

    # --------------------------------------------------------
    # 사이드바: 샘플 개수 설정 (기본 10개)
    # --------------------------------------------------------
    st.sidebar.header("샘플링 설정")
    n_s1_no = st.sidebar.number_input("S1 = NO 샘플 개수", 0, 100, 10)
    n_s1_yes_s2_both_x = st.sidebar.number_input(
        "S1 = YES & (S2_exsitu NEG, S2_lab NEG) 샘플 개수", 0, 100, 10
    )
    n_s1_yes_s2_mixed1 = st.sidebar.number_input(
        "S1 = YES & (S2_exsitu POS, S2_lab NEG) 샘플 개수", 0, 100, 10
    )
    n_s1_yes_s2_mixed2 = st.sidebar.number_input(
        "S1 = YES & (S2_exsitu NEG, S2_lab POS) 샘플 개수", 0, 100, 10
    )
    n_s1_yes_s2_all_ok = st.sidebar.number_input(
        "S1 = YES & (S2_exsitu POS, S2_lab POS) 샘플 개수", 0, 100, 10
    )

    # --------------------------------------------------------
    # 그룹 생성 전: 값 정규화 (Stage1/Stage2)
    # --------------------------------------------------------
    df["S1_norm"] = df["S1"].apply(normalize_stage1_label)
    df["S2_exsitu_bin"] = df["S2_exsitu"].astype(str).apply(normalize_stage2_label)
    df["S2_lab_bin"] = df["S2_lab"].astype(str).apply(normalize_stage2_label)

    logger.info("S1_norm counts: %s", df["S1_norm"].value_counts().to_dict())
    logger.info("S2_exsitu_bin counts: %s", df["S2_exsitu_bin"].value_counts().to_dict())
    logger.info("S2_lab_bin counts: %s", df["S2_lab_bin"].value_counts().to_dict())

    # --------------------------------------------------------
    # 그룹 생성 + 샘플링 (랜덤 시드 고정)
    # --------------------------------------------------------
    logger.info("Creating groups and sampling with fixed random seed=%d", RANDOM_SEED)

    # Group A: S1 == NO
    group_a_all = df[df["S1_norm"] == "NO"]
    group_a = group_a_all.sample(
        n=min(n_s1_no, group_a_all.shape[0]),
        random_state=RANDOM_SEED,
    )

    # Group B: S1 == YES & 둘 다 NEG
    mask_b = (
        (df["S1_norm"] == "YES")
        & (df["S2_exsitu_bin"] == "NEG")
        & (df["S2_lab_bin"] == "NEG")
    )
    group_b_all = df[mask_b]
    group_b = group_b_all.sample(
        n=min(n_s1_yes_s2_both_x, group_b_all.shape[0]),
        random_state=RANDOM_SEED,
    )

    # Group C1: S1 == YES & (ex POS, lab NEG)
    mask_c1 = (
        (df["S1_norm"] == "YES")
        & (df["S2_exsitu_bin"] == "POS")
        & (df["S2_lab_bin"] == "NEG")
    )
    group_c1_all = df[mask_c1]
    group_c1 = group_c1_all.sample(
        n=min(n_s1_yes_s2_mixed1, group_c1_all.shape[0]),
        random_state=RANDOM_SEED,
    )

    # Group C2: S1 == YES & (ex NEG, lab POS)
    mask_c2 = (
        (df["S1_norm"] == "YES")
        & (df["S2_exsitu_bin"] == "NEG")
        & (df["S2_lab_bin"] == "POS")
    )
    group_c2_all = df[mask_c2]
    group_c2 = group_c2_all.sample(
        n=min(n_s1_yes_s2_mixed2, group_c2_all.shape[0]),
        random_state=RANDOM_SEED,
    )

    # Group D: S1 == YES & 둘 다 POS
    mask_d = (
        (df["S1_norm"] == "YES")
        & (df["S2_exsitu_bin"] == "POS")
        & (df["S2_lab_bin"] == "POS")
    )
    group_d_all = df[mask_d]
    group_d = group_d_all.sample(
        n=min(n_s1_yes_s2_all_ok, group_d_all.shape[0]),
        random_state=RANDOM_SEED,
    )

    logger.info(
        "Group sizes (before sampling) - A:%d, B:%d, C1:%d, C2:%d, D:%d",
        group_a_all.shape[0],
        group_b_all.shape[0],
        group_c1_all.shape[0],
        group_c2_all.shape[0],
        group_d_all.shape[0],
    )

    # --------------------------------------------------------
    # 탭 UI
    # --------------------------------------------------------
    tab_a, tab_b, tab_c1, tab_c2, tab_d = st.tabs(
        [
            "S1 = NO",
            "S1 = YES & (NEG, NEG)",
            "S1 = YES & (POS, NEG)",
            "S1 = YES & (NEG, POS)",
            "S1 = YES & (POS, POS)",
        ]
    )

    with tab_a:
        render_group(
            "S1 = NO (Stage1에서 컷된 논문)",
            group_a,
            len(group_a_all),
            s1_options,
            s2_ex_options,
            s2_lab_options,
            s2_focus_options,
            title_col,
            abstract_col,
            doi_col,
            scidir_col,
        )

    with tab_b:
        render_group(
            "S1 = YES & (S2_exsitu NEG, S2_lab NEG)",
            group_b,
            len(group_b_all),
            s1_options,
            s2_ex_options,
            s2_lab_options,
            s2_focus_options,
            title_col,
            abstract_col,
            doi_col,
            scidir_col,
        )

    with tab_c1:
        render_group(
            "S1 = YES & (S2_exsitu POS, S2_lab NEG)",
            group_c1,
            len(group_c1_all),
            s1_options,
            s2_ex_options,
            s2_lab_options,
            s2_focus_options,
            title_col,
            abstract_col,
            doi_col,
            scidir_col,
        )

    with tab_c2:
        render_group(
            "S1 = YES & (S2_exsitu NEG, S2_lab POS)",
            group_c2,
            len(group_c2_all),
            s1_options,
            s2_ex_options,
            s2_lab_options,
            s2_focus_options,
            title_col,
            abstract_col,
            doi_col,
            scidir_col,
        )

    with tab_d:
        render_group(
            "S1 = YES & (S2_exsitu POS, S2_lab POS)",
            group_d,
            len(group_d_all),
            s1_options,
            s2_ex_options,
            s2_lab_options,
            s2_focus_options,
            title_col,
            abstract_col,
            doi_col,
            scidir_col,
        )

    # --------------------------------------------------------
    # 자동 저장 + 다운로드
    # --------------------------------------------------------
    df_new = apply_edits_to_df(df)
    try:
        df_new.to_csv(AUTOSAVE_PATH, index=False)
        logger.info("Autosaved labels to %s", AUTOSAVE_PATH)
    except Exception as e:
        logger.exception("Failed to autosave labels: %s", e)
        st.error(f"자동 저장 중 오류 발생: {e}")

    st.markdown("---")
    st.header("라벨 수정본 CSV 다운로드 (선택사항)")

    if st.button("현재 상태 CSV 다운로드용 생성"):
        csv_buf = io.StringIO()
        df_new.to_csv(csv_buf, index=False)
        csv_bytes = csv_buf.getvalue().encode("utf-8-sig")

        st.download_button(
            label="수정된 라벨 CSV 다운로드",
            data=csv_bytes,
            file_name="labels_reviewed.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
