#!/bin/bash
# === transfer_to_ga00.sh ===

LOCAL_ROOT="/mnt/d/20251111_SEI_CRAWLING_WITH_HYU/20260316_develope"
GA_USER="yongsang"
GA_HOST="203.250.74.27"
GA_PORT="25000"
GA_DEST="/home/yongsang/20251111_SEI_CRAWLING_WITH_HYU/20260316_scp"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -p ${GA_PORT}"
RSYNC_OPTS="-avz --progress --partial"

echo "========================================"
echo " Transfer: WSL → ga00 (port ${GA_PORT})"
echo "========================================"

# 1) 원격 디렉토리 생성
echo ">>> [0] 원격 디렉토리 생성..."
ssh $SSH_OPTS ${GA_USER}@${GA_HOST} "mkdir -p ${GA_DEST}/{pdfs,supplementary_files,output}"

# 2) 스크립트 & CSV & README
echo ""
echo ">>> [1/4] 스크립트/CSV/문서 전송..."
rsync $RSYNC_OPTS -e "ssh ${SSH_OPTS}" \
  ${LOCAL_ROOT}/02_marker_pdf_convert.py \
  ${LOCAL_ROOT}/02_marker_pdf_convert.README.md \
  ${LOCAL_ROOT}/02_marker_pdf_convert.md \
  ${GA_USER}@${GA_HOST}:${GA_DEST}/

rsync $RSYNC_OPTS -e "ssh ${SSH_OPTS}" \
  ${LOCAL_ROOT}/01_preprocess_03_*.csv \
  ${GA_USER}@${GA_HOST}:${GA_DEST}/ 2>/dev/null

rsync $RSYNC_OPTS -e "ssh ${SSH_OPTS}" \
  ${LOCAL_ROOT}/01.preprocess.*.py \
  ${GA_USER}@${GA_HOST}:${GA_DEST}/ 2>/dev/null

# 3) Main PDFs
echo ""
echo ">>> [2/4] Main PDFs 전송..."
MAIN_COUNT=$(find ${LOCAL_ROOT}/pdfs -name "*.pdf" 2>/dev/null | wc -l)
MAIN_SIZE=$(du -sh ${LOCAL_ROOT}/pdfs 2>/dev/null | cut -f1)
echo "    파일 수: ${MAIN_COUNT}, 크기: ${MAIN_SIZE}"
rsync $RSYNC_OPTS -e "ssh ${SSH_OPTS}" \
  ${LOCAL_ROOT}/pdfs/ \
  ${GA_USER}@${GA_HOST}:${GA_DEST}/pdfs/

# 4) Supplementary PDFs
echo ""
echo ">>> [3/4] Supplementary PDFs 전송..."
SUPP_COUNT=$(find ${LOCAL_ROOT}/supplementary_files -name "*.pdf" 2>/dev/null | wc -l)
SUPP_SIZE=$(du -sh ${LOCAL_ROOT}/supplementary_files 2>/dev/null | cut -f1)
echo "    파일 수: ${SUPP_COUNT}, 크기: ${SUPP_SIZE}"
rsync $RSYNC_OPTS -e "ssh ${SSH_OPTS}" \
  ${LOCAL_ROOT}/supplementary_files/ \
  ${GA_USER}@${GA_HOST}:${GA_DEST}/supplementary_files/

# 5) Output (있으면)
echo ""
echo ">>> [4/4] Output 전송..."
if [ -d "${LOCAL_ROOT}/output" ] && [ "$(ls -A ${LOCAL_ROOT}/output 2>/dev/null)" ]; then
  rsync $RSYNC_OPTS -e "ssh ${SSH_OPTS}" \
    ${LOCAL_ROOT}/output/ \
    ${GA_USER}@${GA_HOST}:${GA_DEST}/output/
else
  echo "    output 비어있음 — 건너뜀"
fi

# 6) 검증
echo ""
echo "========================================"
echo " 전송 완료 — 원격 검증"
echo "========================================"
ssh $SSH_OPTS ${GA_USER}@${GA_HOST} << VERIFY
GA_DEST="/home/yongsang/20251111_SEI_CRAWLING_WITH_HYU/20260316_scp"
echo "  스크립트/CSV : \$(ls \${GA_DEST}/*.py \${GA_DEST}/*.csv \${GA_DEST}/*.md 2>/dev/null | wc -l) files"
echo "  Main PDFs   : \$(find \${GA_DEST}/pdfs -name '*.pdf' 2>/dev/null | wc -l) files"
echo "  Supp PDFs   : \$(find \${GA_DEST}/supplementary_files -name '*.pdf' 2>/dev/null | wc -l) files"
echo "  Output      : \$(find \${GA_DEST}/output -type f 2>/dev/null | wc -l) files"
echo ""
du -sh \${GA_DEST}/*/
echo ""
du -sh \${GA_DEST}
VERIFY

echo ""
echo "모든 전송 완료!"

