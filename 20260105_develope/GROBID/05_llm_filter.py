"""
LLM 분류 결과 기반 JSONL 필터링
04_llm_classify_headings.py의 결과를 사용해서 필터링
"""
import json
import re
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter

BASE_DIR = Path(__file__).resolve().parent
INPUT_JSON = BASE_DIR / "01_run_out_v2" / "grobid_results_all.json"
LLM_CLASSIFICATION_DIR = BASE_DIR / "04_llm_classification"
OUTPUT_DIR = BASE_DIR / "05_llm_filtered_data"


def normalize_heading(heading: str) -> str:
    """헤딩 정규화"""
    if not heading:
        return ""
    
    patterns = [
        r'^[0-9]+\.?[0-9]*\.?[0-9]*\.?\s*',
        r'^[SsAaBbCcDd][0-9]+\.?\s*',
        r'^\([0-9]+\)\s*',
        r'^[0-9]+\)\s*',
        r'^\[[0-9]+\]\s*',
    ]
    
    normalized = heading
    for pattern in patterns:
        normalized = re.sub(pattern, '', normalized)
    
    return normalized.strip().lower()


def find_latest_classification() -> Path:
    """최신 LLM 분류 결과 파일 찾기"""
    import glob
    
    pattern = str(LLM_CLASSIFICATION_DIR / "classification_results_*.json")
    files = glob.glob(pattern)
    
    if not files:
        return None
    
    return Path(max(files, key=lambda x: Path(x).stat().st_mtime))


def load_classification(classification_file: Path) -> dict:
    """LLM 분류 결과 로드"""
    with open(classification_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def should_keep_section(heading: str, classifications: dict, 
                       min_confidence: float, 
                       keep_unknown: bool,
                       remove_abstract: bool) -> bool:
    """섹션을 유지할지 판단"""
    if not heading:
        return False
    
    normalized = normalize_heading(heading)
    
    if not normalized:
        return False
    
    # Abstract 제거
    if remove_abstract and normalized == 'abstract':
        return False
    
    # LLM 분류 결과 확인
    if normalized in classifications:
        info = classifications[normalized]
        decision = info.get('decision', 'ERROR')
        confidence = info.get('confidence', 0.0)
        
        if decision == 'YES' and confidence >= min_confidence:
            return True
        elif decision == 'NO':
            return False
        else:  # ERROR
            return keep_unknown
    else:
        # 분류되지 않은 경우
        return keep_unknown


def filter_sections_recursive(sections: list, classifications: dict, 
                              min_confidence: float, keep_unknown: bool,
                              remove_abstract: bool,
                              kept_headings: Counter,
                              removed_headings: Counter) -> list:
    """재귀적 섹션 필터링"""
    filtered = []
    
    for section in sections:
        heading = section.get('heading', '')
        
        if should_keep_section(heading, classifications, min_confidence, keep_unknown, remove_abstract):
            # 섹션 유지
            normalized = normalize_heading(heading)
            if normalized:
                kept_headings[normalized] += 1
            
            new_section = {
                'level': section.get('level', 1),
                'heading': heading,
                'paragraphs': section.get('paragraphs', []),
                'sentences': section.get('sentences', []),
            }
            
            # 자식 섹션 필터링
            children = section.get('children', [])
            if children:
                filtered_children = filter_sections_recursive(
                    children, classifications, min_confidence, keep_unknown, 
                    remove_abstract, kept_headings, removed_headings
                )
                new_section['children'] = filtered_children
            else:
                new_section['children'] = []
            
            filtered.append(new_section)
        else:
            # 섹션 제거
            normalized = normalize_heading(heading)
            if normalized:
                removed_headings[normalized] += 1
            
            # 자식 섹션 검사
            children = section.get('children', [])
            if children:
                filtered_children = filter_sections_recursive(
                    children, classifications, min_confidence, keep_unknown,
                    remove_abstract, kept_headings, removed_headings
                )
                filtered.extend(filtered_children)
    
    return filtered


def filter_document(doc: dict, classifications: dict, 
                   min_confidence: float, keep_unknown: bool,
                   remove_abstract: bool,
                   kept_headings: Counter,
                   removed_headings: Counter) -> dict:
    """문서 필터링"""
    if doc.get('has_error') or 'error' in doc:
        return None
    
    sections = doc.get('sections', [])
    filtered_sections = filter_sections_recursive(
        sections, classifications, min_confidence, keep_unknown,
        remove_abstract, kept_headings, removed_headings
    )
    
    if not filtered_sections:
        return None
    
    # Abstract 처리
    abstract = doc.get('abstract_paragraphs', [])
    if remove_abstract:
        abstract = []
    
    return {
        'source_file': doc.get('source_file', ''),
        'title': doc.get('title', ''),
        'abstract_paragraphs': abstract,
        'sections': filtered_sections
    }


def count_sections_recursive(sections: list) -> int:
    """재귀적 섹션 개수 카운트"""
    count = len(sections)
    for section in sections:
        children = section.get('children', [])
        if children:
            count += count_sections_recursive(children)
    return count


def main():
    parser = argparse.ArgumentParser(description="LLM 분류 결과 기반 필터링")
    parser.add_argument("--classification_file", help="LLM classification JSON file (default: latest)")
    parser.add_argument("--min_confidence", type=float, default=0.5, help="Minimum confidence for YES decision")
    parser.add_argument("--keep_unknown", action="store_true", help="Keep sections not in classification")
    parser.add_argument("--remove_abstract", action="store_true", default=True, help="Remove abstracts")
    args = parser.parse_args()
    
    print("=" * 70)
    print("LLM Classification-Based Filtering")
    print("=" * 70)
    
    # 분류 파일 찾기
    if args.classification_file:
        classification_file = Path(args.classification_file)
    else:
        classification_file = find_latest_classification()
    
    if not classification_file or not classification_file.exists():
        print("ERROR: No classification file found!")
        print(f"Please run 04_llm_classify_headings.py first")
        return
    
    print(f"Classification file: {classification_file.name}")
    print(f"Min confidence: {args.min_confidence}")
    print(f"Keep unknown: {args.keep_unknown}")
    print(f"Remove abstract: {args.remove_abstract}")
    print()
    
    # 출력 디렉토리
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 1. 분류 결과 로드
    print("[1] Loading LLM classification results...")
    classifications = load_classification(classification_file)
    
    yes_count = sum(1 for c in classifications.values() if c.get('decision') == 'YES')
    no_count = sum(1 for c in classifications.values() if c.get('decision') == 'NO')
    
    print(f"   ✓ Loaded {len(classifications)} classifications")
    print(f"      YES: {yes_count}, NO: {no_count}")
    
    # 2. 입력 데이터 로드
    print(f"\n[2] Loading: {INPUT_JSON}")
    if not INPUT_JSON.exists():
        print("ERROR: File not found!")
        return
    
    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print(f"   ✓ Loaded {len(data)} papers")
    
    # 3. 필터링
    print("\n[3] Filtering...")
    
    filtered_data = []
    removed_count = 0
    
    original_sections = 0
    filtered_sections = 0
    kept_headings = Counter()
    removed_headings = Counter()
    
    for doc in data:
        if not doc.get('has_error') and 'error' not in doc:
            original_sections += count_sections_recursive(doc.get('sections', []))
        
        result = filter_document(
            doc, classifications, args.min_confidence, args.keep_unknown,
            args.remove_abstract, kept_headings, removed_headings
        )
        
        if result:
            filtered_data.append(result)
            filtered_sections += count_sections_recursive(result['sections'])
        else:
            removed_count += 1
    
    print(f"   ✓ Papers: {len(filtered_data)} kept, {removed_count} removed")
    print(f"   ✓ Sections: {original_sections} → {filtered_sections} ({filtered_sections/original_sections*100:.1f}%)")
    
    # 4. JSONL 저장
    output_jsonl = OUTPUT_DIR / f"llm_filtered_{timestamp}.jsonl"
    print(f"\n[4] Saving JSONL: {output_jsonl}")
    
    with open(output_jsonl, 'w', encoding='utf-8') as f:
        for doc in filtered_data:
            f.write(json.dumps(doc, ensure_ascii=False) + '\n')
    
    print(f"   ✓ Saved {len(filtered_data)} papers")
    
    # 5. 통계 저장
    stats_path = OUTPUT_DIR / f"filtering_stats_{timestamp}.txt"
    print(f"\n[5] Saving statistics: {stats_path}")
    
    with open(stats_path, 'w', encoding='utf-8') as f:
        f.write("=" * 70 + "\n")
        f.write("LLM-Based Filtering Statistics\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Classification file: {classification_file.name}\n")
        f.write(f"Min confidence: {args.min_confidence}\n")
        f.write(f"Keep unknown: {args.keep_unknown}\n")
        f.write(f"Remove abstract: {args.remove_abstract}\n\n")
        
        f.write(f"Original Papers: {len(data)}\n")
        f.write(f"Filtered Papers: {len(filtered_data)}\n")
        f.write(f"Removed Papers: {removed_count}\n\n")
        
        f.write(f"Original Sections: {original_sections}\n")
        f.write(f"Filtered Sections: {filtered_sections}\n")
        f.write(f"Retention Rate: {filtered_sections/original_sections*100:.1f}%\n\n")
        
        f.write("Top 30 Kept Headings:\n")
        f.write("-" * 70 + "\n")
        for idx, (heading, count) in enumerate(kept_headings.most_common(30), 1):
            f.write(f"{idx:3d}. {heading:50s} : {count:4d}\n")
    
    print(f"   ✓ Statistics saved")
    
    # 6. CSV 저장
    import pandas as pd
    
    kept_csv = OUTPUT_DIR / f"kept_headings_{timestamp}.csv"
    removed_csv = OUTPUT_DIR / f"removed_headings_{timestamp}.csv"
    
    print(f"\n[6] Saving CSVs...")
    
    df_kept = pd.DataFrame(kept_headings.most_common(), columns=['heading', 'count'])
    df_kept['rank'] = range(1, len(df_kept) + 1)
    df_kept = df_kept[['rank', 'heading', 'count']]
    df_kept.to_csv(kept_csv, index=False, encoding='utf-8-sig')
    
    df_removed = pd.DataFrame(removed_headings.most_common(), columns=['heading', 'count'])
    df_removed['rank'] = range(1, len(df_removed) + 1)
    df_removed = df_removed[['rank', 'heading', 'count']]
    df_removed.to_csv(removed_csv, index=False, encoding='utf-8-sig')
    
    print(f"   ✓ Kept: {len(df_kept)} headings")
    print(f"   ✓ Removed: {len(df_removed)} headings")
    
    # 최종 요약
    print("\n" + "=" * 70)
    print("Filtering Complete!")
    print("=" * 70)
    print(f"\nResults: {OUTPUT_DIR}")
    print(f"  - JSONL: {output_jsonl.name}")
    print(f"  - Stats: {stats_path.name}")
    print(f"  - Kept CSV: {kept_csv.name}")
    print(f"  - Removed CSV: {removed_csv.name}")
    print(f"\nRetention: {len(filtered_data)}/{len(data)} papers ({len(filtered_data)/len(data)*100:.1f}%)")
    print(f"Sections: {filtered_sections}/{original_sections} ({filtered_sections/original_sections*100:.1f}%)")
    print()


if __name__ == "__main__":
    main()
