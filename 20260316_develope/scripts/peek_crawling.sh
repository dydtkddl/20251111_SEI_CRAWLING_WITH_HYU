#!/bin/bash
# peek_crawling.sh

BASE="/mnt/d/20251111_SEI_CRAWLING_WITH_HYU/20260316_develope/crawling"

for f in "$BASE"/*; do
    ext="${f##*.}"
    fname=$(basename "$f")
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "📄 $fname"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    case "$ext" in
        py|bat)  cat "$f" ;;
        csv)     head -2 "$f" ;;
        *)       echo "(스킵: .$ext)" ;;
    esac
done

