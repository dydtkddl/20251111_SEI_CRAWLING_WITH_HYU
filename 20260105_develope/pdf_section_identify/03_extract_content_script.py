import json
import argparse
from pathlib import Path
import re
import difflib

HEADER_RE = re.compile(r'^(#{1,6})\s*(.+?)\s*$')


def load_json(path: Path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def find_markdown_file(root_dir: Path, folder_name: str):
    """
    Finds the main markdown file within the specific folder.
    Heuristic:
      1) prefer a file named <folder_name>.md
      2) else choose the largest .md file under that folder
    """
    folder_path = root_dir / folder_name
    if not folder_path.exists():
        return None

    candidates = list(folder_path.rglob("*.md"))
    if not candidates:
        return None

    for c in candidates:
        if c.stem == folder_name:
            return c

    return max(candidates, key=lambda p: p.stat().st_size)


def strip_hashes(header_line: str) -> str:
    """Remove leading #'s and surrounding whitespace."""
    s = header_line.strip()
    s = re.sub(r'^#{1,6}\s*', '', s)
    return s.strip()


def normalize_heading_text(s: str) -> str:
    """
    Normalize heading text for matching:
      - remove markdown emphasis chars *, _, ` 
      - collapse whitespace
      - lowercase
    """
    s = strip_hashes(s)
    s = s.replace('*', '').replace('_', '').replace('`', '')
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s


def extract_section_number(heading_text_no_hash: str) -> str | None:
    """
    Extract a section number prefix like:
      "2. Experiment" -> "2"
      "2.1.1. Synthesis..." -> "2.1.1"
    """
    t = heading_text_no_hash.strip()
    # common patterns: "2.1.1. Title" or "2.1 Title"
    m = re.match(r'^(\d+(?:\.\d+)*)', t)
    return m.group(1) if m else None


def parse_headers(lines: list[str]):
    """
    Return list of headers with:
      - idx: line index
      - raw: stripped full header line (including #'s)
      - level: number of #'s
      - text_no_hash: header text without leading #'s
      - norm_text: normalized text for robust matching
      - secnum: extracted section number if present
    """
    headers = []
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line.strip())
        if not m:
            continue
        hashes, title = m.group(1), m.group(2)
        raw = line.strip()
        level = len(hashes)
        text_no_hash = strip_hashes(raw)
        norm_text = normalize_heading_text(raw)
        secnum = extract_section_number(text_no_hash)
        headers.append({
            "idx": i,
            "raw": raw,
            "level": level,
            "text_no_hash": text_no_hash,
            "norm_text": norm_text,
            "secnum": secnum,
        })
    return headers


def build_section_spans(lines: list[str], headers: list[dict]):
    """
    For each header, compute the span (start, end) of its content:
      - start: line after the header
      - end: line index of the next header that is same-or-higher level (<= current level)
             or EOF if none.
    This includes subheaders (deeper level) as part of the parent section content.
    """
    spans = {}
    n = len(lines)

    for hi, h in enumerate(headers):
        start = h["idx"] + 1
        end = n
        cur_level = h["level"]

        for nxt in headers[hi + 1:]:
            if nxt["level"] <= cur_level:
                end = nxt["idx"]
                break

        spans[h["raw"]] = (start, end)

    return spans


def choose_best_header_match(target_header: str, headers: list[dict]):
    """
    Robust matching:
      1) exact raw match
      2) normalized text exact match
      3) section-number match (e.g., 2.1.1)
      4) difflib similarity on normalized text
    Returns the matched header dict or None.
    """
    target_raw = target_header.strip()

    # 1) exact raw match
    for h in headers:
        if h["raw"] == target_raw:
            return h

    # prep for other matching
    target_norm = normalize_heading_text(target_raw)
    target_text_no_hash = strip_hashes(target_raw)
    target_secnum = extract_section_number(target_text_no_hash)

    # 2) normalized text exact match
    same_norm = [h for h in headers if h["norm_text"] == target_norm]
    if len(same_norm) == 1:
        return same_norm[0]
    if len(same_norm) > 1:
        # if multiple, prefer same level if target provides hashes
        target_level = len(re.match(r'^(#{1,6})', target_raw).group(1)) if re.match(r'^(#{1,6})', target_raw) else None
        if target_level is not None:
            same_level = [h for h in same_norm if h["level"] == target_level]
            if len(same_level) >= 1:
                return same_level[0]
        return same_norm[0]

    # 3) section-number match
    if target_secnum:
        same_num = [h for h in headers if h["secnum"] == target_secnum]
        if len(same_num) == 1:
            return same_num[0]
        if len(same_num) > 1:
            # pick the one with best similarity of remaining title
            def score(h):
                return difflib.SequenceMatcher(None, target_norm, h["norm_text"]).ratio()
            same_num.sort(key=score, reverse=True)
            return same_num[0]

    # 4) similarity fallback
    best = None
    best_score = 0.0
    for h in headers:
        s = difflib.SequenceMatcher(None, target_norm, h["norm_text"]).ratio()
        if s > best_score:
            best_score = s
            best = h

    # threshold to avoid totally wrong matches
    if best and best_score >= 0.72:
        return best

    return None


def extract_sections(md_path: Path, target_headers: list[str]):
    """
    Extract content for each header in target_headers.
    Content: from the header line to the next header of same-or-higher level (not any header).
    Includes subheaders inside the extracted content.
    Also does robust header matching if exact string doesn't match.
    """
    if not target_headers:
        return {}

    try:
        content = md_path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        print(f"Error reading {md_path}: {e}")
        return {}

    lines = content.splitlines()
    headers = parse_headers(lines)
    spans = build_section_spans(lines, headers)

    results = {}

    for target in target_headers:
        matched = choose_best_header_match(target, headers)
        if not matched:
            print(f"[WARN] Header not found in {md_path.name}: {target}")
            results[target] = ""
            continue

        raw = matched["raw"]
        start, end = spans.get(raw, (matched["idx"] + 1, len(lines)))
        section_text = "\n".join(lines[start:end]).strip()

        # key를 "입력 target"로 유지할지, "실제 매칭된 raw 헤더"로 유지할지 선택 문제:
        # - 입력 구조 유지가 필요하면 target을 key로
        # - 실제 파일의 헤더명을 key로 하고 싶으면 raw로
        # 여기서는 "입력 target"을 key로 유지하되, 매칭 정보도 같이 넣어줌.
        results[target] = {
            "_matched_header": raw,
            "_matched_level": matched["level"],
            "content": section_text
        }

    return results


def main(input_json_path, root_dir_path, output_json_path):
    data = load_json(Path(input_json_path))
    root = Path(root_dir_path)

    results = {}

    for folder_name, headers in data.items():
        if not headers:
            results[folder_name] = {}
            continue

        md_file = find_markdown_file(root, folder_name)
        if not md_file:
            print(f"Warning: No markdown file found for {folder_name}")
            results[folder_name] = {}
            continue

        extracted_data = extract_sections(md_file, headers)
        results[folder_name] = extracted_data

    with open(output_json_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Extraction complete. Saved to {output_json_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json", help="Path to numbered methods sections JSON")
    parser.add_argument("root_dir", help="Path to pdfs_marker_output root directory")
    parser.add_argument("--output", default="03_extracted_content.json", help="Output JSON path")
    args = parser.parse_args()

    main(args.input_json, args.root_dir, args.output)
