#!/bin/bash
# ───────────────────────────────────────────────
# 📤 Git push PDF_01 ~ PDF_10 순차 자동 업로드
# 용상 @ KHU | 2025-11-11
# ───────────────────────────────────────────────
set -euo pipefail

for i in $(seq -w 1 10); do
    folder="PDF_${i}"
    if [ -d "$folder" ]; then
        echo "📦 업로드 중: $folder"
        git add "$folder"
        git commit -m "Add $folder"
        git push
        echo "✅ 완료: $folder"
    else
        echo "⚠️ 폴더 없음: $folder (건너뜀)"
    fi
done

echo "🎉 모든 PDF 폴더 push 완료!"

