import os

# === 사용자 설정 ===
INPUT_DIR = "pdfs"                 # PDF가 있는 폴더
OUTPUT_ROOT = "pdfs_marker_output"    # 결과 저장 루트 폴더
OUTPUT_FILE = "marker_commands.sh"    # 생성할 실행 스크립트

os.makedirs(OUTPUT_ROOT, exist_ok=True)

# PDF 목록 가져오기
pdf_files = sorted([
    f for f in os.listdir(INPUT_DIR)
    if f.lower().endswith(".pdf")
])

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    for pdf in pdf_files:
        input_path = os.path.join(INPUT_DIR, pdf)

        # output_dir = 파일명(확장자 제거)
        pdf_name = os.path.splitext(pdf)[0]
        output_dir = os.path.join(OUTPUT_ROOT, pdf_name)

        # 커맨드 생성
        cmd = (
            f'marker_single "{input_path}" '
            f'--output_dir "{output_dir}" '
            f'--output_format markdown\n'
        )

        f.write(cmd)

print(f"Generated {len(pdf_files)} commands → {OUTPUT_FILE}")

