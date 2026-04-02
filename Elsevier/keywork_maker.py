# -*- coding: utf-8 -*-
"""
AZMB ex-situ Protective Layer Keyword Generator
-------------------------------------------------------------
- Field1~5 기반 모든 조합 생성
- 표현 다양성 + 커버리지 최적화된 필드 구성 반영
- Logging + tqdm 지원
- JSON 출력
"""

import json
import logging
from itertools import product
from tqdm import tqdm
import os

# ============================================================
# LOGGING CONFIG
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("keywordmaker_AZMB.log", encoding="utf-8"),
        logging.StreamHandler()
    ],
)

# ============================================================
# FIELD DEFINITIONS  (최종 확정본)
# ============================================================

FIELD1 = [
    "aqueous zinc battery",
    "zinc metal battery",
]

FIELD2 = [
    "zinc anode",
    "anode protection",
    "protective interface",
    "artificial interphase",
    "SEI layer",
]

FIELD3 = [
    "ex-situ coating",
    "pre-coated zinc",
    "coated zinc",
    "protective layer",
    "artificial protective layer",
    "surface modification",
    "interfacial engineering",
]

FIELD4 = [
    "polymer",
    "hydrogel",
    "MOF",
    "MXene",
    "carbon",
    "oxide",
    "composite",
]

FIELD5 = [
    "dendrite suppression",
    "corrosion resistance",
    "HER suppression",
    "cycling stability",
]

ALL_FIELDS = [FIELD1, FIELD2, FIELD3, FIELD4, FIELD5]


# ============================================================
# COMBINATION GENERATOR
# ============================================================

def generate_combinations():
    logging.info("Generating combinations from Field1~5...")

    combos = []

    # build product list
    for combo in tqdm(list(product(*ALL_FIELDS)), desc="Generating"):
        # combo = (f1, f2, f3, f4, f5)
        words = [c for c in combo if c.strip()]

        if not words:
            continue

        query = " ".join(words)

        # overly long string skip (rare case)
        if len(query) > 200:
            query = query[:200]

        combos.append(query)

    logging.info(f"Raw combinations: {len(combos):,}")

    # remove duplicates
    combos = sorted(list(set(combos)))

    logging.info(f"Unique combinations: {len(combos):,}")

    return combos


# ============================================================
# SAVE JSON
# ============================================================

def save_json(data, path="keyword_combinations_AZMB_exsitu.json"):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logging.info(f"Saved JSON → {path}")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    combos = generate_combinations()
    save_json(combos, "output/keyword_combinations_AZMB_exsitu.json")

    logging.info("Done.")
