import os
import re
import shutil

# === 설정 ===
INPUT_ROOT = r"d:\20251111_SEI_CRAWLING_WITH_HYU\20260105_전달내용_및_develope\pdfs_marker_output"
OUTPUT_ROOT = r"d:\20251111_SEI_CRAWLING_WITH_HYU\20260105_전달내용_및_develope\pdfs_experiments_only"

# 추출하고 싶은 섹션 키워드 (소문자 기준)
TARGET_KEYWORDS = ["experimental", "methods", "materials and methods", "methodology"]
# 제외하고 싶은 섹션 키워드 (오탐지 방지)
EXCLUDE_KEYWORDS = ["supplementary", "appendix", "references", "acknowledgements"]

def is_target_header(header_text):
    text_lower = header_text.lower()
    # 제외 키워드 확인
    if any(ex in text_lower for ex in EXCLUDE_KEYWORDS):
        return False
    # 목표 키워드 확인
    if any(kw in text_lower for kw in TARGET_KEYWORDS):
        return True
    return False

def extract_section_from_md(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    extracted_content = []
    capture_mode = False
    capture_level = 0
    
    # 헤더 탐지용 정규식 (예: ## 2. Experimental)
    header_pattern = re.compile(r"^(#+)\s+(.*)")

    for line in lines:
        match = header_pattern.match(line)
        
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            
            if capture_mode:
                # 이미 캡처 중일 때:
                # 현재 헤더 레벨이 처음 캡처 시작한 레벨과 같거나 더 상위 레벨(숫자가 작음)이면 종료
                if level <= capture_level:
                    capture_mode = False
                    # 여기서 종료하지만, 만약 이 헤더도 타겟일 수 있으므로(연속된 섹션 등), 다시 검사 위임은 복잡해짐.
                    # 보통 Experimental 섹션은 하나이므로 여기서 끊어도 무방.
                    # 혹시 'Experimental' 끝나고 'Methods'가 나올 수도 있으니 break 대신 continue해서 다시 검사할 수도 있음.
                    # 여기서는 섹션이 끝나면 loop 계속 돌면서 다른 타겟 섹션이 있는지 찾도록 함.
                else:
                    # 하위 섹션(예: ####)이므로 계속 캡처
                    extracted_content.append(line)
            
            # 캡처 모드가 꺼져있거나 방금 꺼졌을 때, 새로운 타겟인지 확인
            if not capture_mode:
                if is_target_header(title):
                    capture_mode = True
                    capture_level = level
                    extracted_content.append(line)
        
        else:
            # 헤더가 아닌 일반 텍스트
            if capture_mode:
                extracted_content.append(line)

    return "".join(extracted_content)

def main():
    if not os.path.exists(OUTPUT_ROOT):
        os.makedirs(OUTPUT_ROOT)

    processed_count = 0
    extracted_count = 0

    print(f"Scanning {INPUT_ROOT}...")

    for root, dirs, files in os.walk(INPUT_ROOT):
        for file in files:
            if file.lower().endswith(".md"):
                full_path = os.path.join(root, file)
                processed_count += 1
                
                # 섹션 추출
                content = extract_section_from_md(full_path)
                
                if content.strip():
                    # 결과 저장 경로 생성 (구조 유지)
                    rel_path = os.path.relpath(root, INPUT_ROOT)
                    target_dir = os.path.join(OUTPUT_ROOT, rel_path)
                    os.makedirs(target_dir, exist_ok=True)
                    
                    target_file = os.path.join(target_dir, file)
                    with open(target_file, "w", encoding="utf-8") as f:
                        f.write(content)
                    
                    extracted_count += 1
                    print(f"[OK] Extracted: {file}")
                else:
                    # 섹션을 못 찾았거나 비어있음
                    pass

    print("="*50)
    print(f"Total Markdown files scanned: {processed_count}")
    print(f"Files with extracted content: {extracted_count}")
    print(f"Saved to: {OUTPUT_ROOT}")

if __name__ == "__main__":
    main()
