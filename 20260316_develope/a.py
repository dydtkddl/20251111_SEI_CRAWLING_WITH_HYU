import pandas as pd
from pathlib import Path

files = {
    "colab_a100": r"01_preprocess_03_colab_a100.csv",
    "l4x2":       r"01_preprocess_03_l4x2.csv",
    "rtx4090":    r"01_preprocess_03_rtx4090.csv",
    "rtx5090":    r"01_preprocess_03_rtx5090.csv",
}

rows = []
total = 0
for name, path in files.items():
    count = len(pd.read_csv(path))
    total += count
    rows.append((name, path, count))

# 마크다운 테이블 출력
print("| env_name | count |")
print("|---|---|")
for name, path, count in rows:
    print(f"| {name} | {count} |")
print(f"| **합계** | **{total}** |")

