import json
import argparse
import re
from pathlib import Path
from typing import Dict, List

# Markdown heading (ATX) + blockquote/list prefix 허용
#   #### 2.3 ...
#   > #### 2.3 ...
#   - #### 2.3 ...
MD_HEADING_RE = re.compile(r'^\s*(?:>+\s*)?(?:[-*+•]\s*)?(#{1,6})\s*(.+?)\s*$')

# "# 없이" 번호만 있는 섹션도 약하게 헤더로 인식
#   2.3 Characterization ...
#   2.3. Characterization ...
#   2.3) Characterization ...
NUM_HEADING_RE = re.compile(r'^\s*(\d+(?:\.\d+)*)\s*[\.\)]?\s+(.+?)\s*$')

# false-positive 줄이기용 (원하시면 추가/삭제)
NUM_HEADING_EXCLUDE_PREFIX = (
    "fig", "figure", "table", "scheme", "eq", "equation"
)

def _preclean_line(line: str) -> str:
    """라인 선처리: NBSP/BOM/zero-width 제거 + trim"""
    if line is None:
        return ""
    s = line.replace("\u00a0", " ")  # NBSP -> space
    s = s.lstrip("\ufeff\u200b\u200c\u200d\u2060")  # BOM/zero-width
    return s.strip()

def _looks_like_number_heading(s: str) -> bool:
    """
    '# 없는 번호 헤딩'을 헤딩으로 볼지 여부.
    본문 숫자 문장 오탐 줄이기 위해 약간의 제약을 둠.
    """
    if len(s) > 160:  # 너무 길면 본문일 확률 큼
        return False
    # 알파벳/한글이 최소 1개는 있어야 함
    if not re.search(r'[A-Za-z가-힣]', s):
        return False

    lower = s.lower().lstrip()
    # Figure/Table/Eq 같은 캡션류 오탐 방지
    for p in NUM_HEADING_EXCLUDE_PREFIX:
        if lower.startswith(p + " "):
            return False
        if lower.startswith(p + "."):
            return False
        if lower.startswith(p + "\t"):
            return False
    return True

def collect_headers(root_dir: str, output_file: str) -> None:
    """
    marker output 폴더(root_dir) 아래의 모든 .md에서 헤딩 라인만 모아서
    {top_folder_name: [heading_lines...]} JSON으로 저장.
    """
    root_path = Path(root_dir)
    if not root_path.exists():
        raise FileNotFoundError(f"Directory does not exist: {root_dir}")

    results: Dict[str, List[str]] = {}

    for file_path in root_path.rglob("*.md"):
        try:
            relative_path = file_path.relative_to(root_path)
        except Exception:
            continue

        # key는 root_dir 바로 아래 1-depth 폴더명
        top_folder = relative_path.parts[0] if relative_path.parts else "root"
        key = top_folder

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"[WARN] Failed to read: {file_path} ({e})")
            continue

        headers: List[str] = []
        for line in content.splitlines():
            raw = _preclean_line(line)
            if not raw:
                continue

            # 1) 정상 markdown heading
            m = MD_HEADING_RE.match(raw)
            if m:
                hashes, title = m.group(1), m.group(2).strip()
                # 표준화: "#... <space>title"
                headers.append(f"{hashes} {title}")
                continue

            # 2) "# 없는 번호 heading도 약하게 수집
            m2 = NUM_HEADING_RE.match(raw)
            if m2 and _looks_like_number_heading(raw):
                # downstream 파서가 쉽게 먹도록 강제로 #### 붙임
                headers.append(f"#### {raw}")

        if headers:
            results.setdefault(key, []).extend(headers)

    # 빈 엔트리 제거
    results = {k: v for k, v in results.items() if v}

    out_path = Path(output_file)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {out_path}  (folders={len(results)})")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect headings from marker-output markdown files.")
    parser.add_argument("root_dir", help="Directory containing marker output subfolders (root).")
    parser.add_argument("--output", default="01_headers_summary.json", help="Output JSON file path.")
    args = parser.parse_args()

    collect_headers(args.root_dir, args.output)
