#!/bin/bash

# --- Configuration ---
MAX_LINES=1800
BASE_NAME="project_core_code"
# ---------------------

PART=1
CURRENT_FILE="${BASE_NAME}_part${PART}.md"
LINE_COUNT=0

# Clean old parts
rm -f ${BASE_NAME}_part*.md

start_new_file() {
    echo "# Project Core Source Code - Part ${PART}" > "$CURRENT_FILE"
    echo "Generated on: $(date)" >> "$CURRENT_FILE"
    echo "Part: ${PART}" >> "$CURRENT_FILE"
    echo "" >> "$CURRENT_FILE"
    LINE_COUNT=$(wc -l < "$CURRENT_FILE")
}

# Initialize first file
start_new_file

# .py 파일 및 configs/prompts 폴더 내의 .md 파일 검색
find . -type f \( -name "*.py" -o -path "*/configs/prompts/*.md" \) \
    -not -path "*/.*" \
    -not -path "*/__pycache__/*" \
    -not -path "*/data/*" \
    -not -path "*/runs/*" | while read -r file; do
    
    # Create temporary buffer for the current file's content
    TMP_BUF=$(mktemp)
    
    echo '```' >> "$TMP_BUF"
    echo "$file" >> "$TMP_BUF"
    echo '```' >> "$TMP_BUF"
    
    if [[ $file == *.py ]]; then
        echo '```python' >> "$TMP_BUF"
    else
        echo '```markdown' >> "$TMP_BUF"
    fi
    
    cat "$file" >> "$TMP_BUF"
    echo '```' >> "$TMP_BUF"
    echo "" >> "$TMP_BUF"
    
    BUF_LINES=$(wc -l < "$TMP_BUF")
    
    # Check if we need to split
    if (( LINE_COUNT + BUF_LINES > MAX_LINES )) && (( LINE_COUNT > 10 )); then
        PART=$((PART + 1))
        CURRENT_FILE="${BASE_NAME}_part${PART}.md"
        start_new_file
    fi
    
    # Append buffer to current file
    cat "$TMP_BUF" >> "$CURRENT_FILE"
    LINE_COUNT=$((LINE_COUNT + BUF_LINES))
    rm "$TMP_BUF"
    
    echo "Added to Part $PART: $file"
done

echo "Done! Final files created as ${BASE_NAME}_partX.md"
