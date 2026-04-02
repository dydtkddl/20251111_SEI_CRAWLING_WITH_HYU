"""
GROBID JSONL 필터링 - Experimental Section만 남기기
메타데이터 및 불필요한 섹션 제거
"""
import json
import re
from pathlib import Path
from datetime import datetime
from collections import Counter

# 경로 설정
BASE_DIR = Path(__file__).resolve().parent
INPUT_JSON = BASE_DIR / "01_run_out_v2" / "grobid_results_all.json"
OUTPUT_DIR = BASE_DIR / "03_filtered_data"


# 제거할 섹션 패턴 정의
REMOVE_PATTERNS = [
    # 메타데이터
    r'^introduction$',
    r'^conclusion[s]?$',
    r'^credit authorship',
    r'^declaration of competing',
    r'^fund$',
    r'^prime novelty',
    r'^resource availability',
    r'^lead contact',
    r'^materials availability',
    r'^conclusion and outlook',
    
    # Simulation & Calculation
    r'simulation',
    r'calculation[s]?',
    r'dft',
    r'theoretical',
    r'computational',
    r'first-principles',
    r'molecular dynamics',
    r'finite element',
    
    # Characterization (제거)
    r'^characterization[s]?$',
    r'characterization[s]?\s*(techniques|methods|of)',
    r'material[s]?\s*characterization[s]?',
    r'electrochemical\s*characterization',
    r'morpholog(y|ical)\s*(and\s*)?(structural\s*)?characterization',
    r'physical\s*(and\s*)?(chemical\s*)?characterization',
    r'structural\s*(and\s*)?morphological\s*characterization',
    
    # Performance & Results (일부)
    r'performance',
    r'^results?\s*(and\s*)?discussion[s]?',
    
    # 테이블, 피규어, 기타 유효하지 않은 섹션명
    r'^table\s+\d+',
    r'^fig\.',
    r'^figure\s+\d+',
    r'^\+$',
    r'^-$',
    r'^ce$',
    r'^\d+\s*\+\s*\d+$',
]

# 유지할 섹션 패턴 (우선순위)
KEEP_PATTERNS = [
    # Experimental, Methods
    r'experimental',
    r'experiment[s]?',
    r'method[s]?',
    
    # Preparation, Synthesis
    r'preparation',
    r'synthesis',
    r'fabrication',
    r'assembly',
    
    # Materials (구체적인)
    r'materials?\s*(and\s*)?(methods|reagents)',
    r'chemical[s]?',
    
    # Electrode preparation
    r'electrode',
    r'anode',
    r'cathode',
    
    # Electrochemical
    r'electrochemical\s*(measurement|test|properties|analysis)',
]


def normalize_heading(heading: str) -> str:
    """헤딩 정규화"""
    if not heading:
        return ""
    
    # 넘버링 제거
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


def should_remove_section(heading: str) -> bool:
    """섹션을 제거해야 하는지 판단"""
    if not heading:
        return True
    
    normalized = normalize_heading(heading)
    
    if not normalized:
        return True
    
    # 먼저 유지 패턴 확인 (우선순위)
    for pattern in KEEP_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            # 하지만 제거 패턴과도 매치되는지 확인
            for remove_pattern in REMOVE_PATTERNS:
                if re.search(remove_pattern, normalized, re.IGNORECASE):
                    # characterization이 포함된 경우는 제거
                    if 'characterization' in normalized:
                        return True
                    # performance가 포함된 경우도 제거
                    if 'performance' in normalized:
                        return True
            # 제거 패턴과 매치되지 않으면 유지
            return False
    
    # 유지 패턴과 매치되지 않으면 제거 패턴 확인
    for pattern in REMOVE_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            return True
    
    # 기본: 매우 짧거나 숫자만 있는 경우 제거
    if len(normalized) < 3:
        return True
    
    # 숫자/특수문자만 있는 경우
    if re.match(r'^[\d\s\.\-\+\(\)]+$', normalized):
        return True
    
    # 기본적으로 명확하지 않은 것은 제거
    return True


def filter_sections_recursive(sections: list, removed_headings: Counter) -> list:
    """재귀적으로 섹션 필터링"""
    filtered = []
    
    for section in sections:
        heading = section.get('heading', '')
        
        # 섹션 제거 여부 판단
        if should_remove_section(heading):
            # 제거된 헤딩 카운트
            normalized = normalize_heading(heading)
            if normalized:
                removed_headings[normalized] += 1
            
            # 자식 섹션은 검사 (유용한 하위 섹션이 있을 수 있음)
            children = section.get('children', [])
            if children:
                filtered_children = filter_sections_recursive(children, removed_headings)
                # 유효한 자식이 있으면 추가
                filtered.extend(filtered_children)
            continue
        
        # 섹션 유지
        new_section = {
            'level': section.get('level', 1),
            'heading': heading,
            'paragraphs': section.get('paragraphs', []),
            'sentences': section.get('sentences', []),
        }
        
        # 자식 섹션 필터링
        children = section.get('children', [])
        if children:
            filtered_children = filter_sections_recursive(children, removed_headings)
            new_section['children'] = filtered_children
        else:
            new_section['children'] = []
        
        filtered.append(new_section)
    
    return filtered


def filter_document(doc: dict, removed_headings: Counter) -> dict:
    """문서 필터링"""
    if doc.get('has_error') or 'error' in doc:
        return None
    
    sections = doc.get('sections', [])
    filtered_sections = filter_sections_recursive(sections, removed_headings)
    
    # 유효한 섹션이 없으면 제외
    if not filtered_sections:
        return None
    
    return {
        'source_file': doc.get('source_file', ''),
        'title': doc.get('title', ''),
        'abstract_paragraphs': doc.get('abstract_paragraphs', []),
        'sections': filtered_sections
    }


def count_sections_recursive(sections: list) -> int:
    """재귀적으로 섹션 개수 세기"""
    count = len(sections)
    for section in sections:
        children = section.get('children', [])
        if children:
            count += count_sections_recursive(children)
    return count


def main():
    """메인 실행"""
    print("=" * 70)
    print("GROBID JSONL Filtering - Experimental Sections Only")
    print("=" * 70)
    
    # 출력 디렉토리 생성
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 1. 입력 파일 로드
    print(f"\n[1] Loading input: {INPUT_JSON}")
    if not INPUT_JSON.exists():
        print(f"ERROR: File not found!")
        return
    
    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print(f"   ✓ Loaded {len(data)} papers")
    
    # 2. 필터링
    print("\n[2] Filtering sections...")
    filtered_data = []
    removed_count = 0
    
    # 통계
    original_sections = 0
    filtered_sections = 0
    removed_headings = Counter()  # 제거된 헤딩 카운터 추가
    kept_headings = Counter()
    
    for doc in data:
        # 원본 섹션 수 계산
        if not doc.get('has_error') and 'error' not in doc:
            original_sections += count_sections_recursive(doc.get('sections', []))
        
        result = filter_document(doc, removed_headings)  # removed_headings 전달
        
        if result:
            filtered_data.append(result)
            filtered_sections += count_sections_recursive(result['sections'])
            
            # 유지된 헤딩 카운트
            def count_headings(sections):
                for section in sections:
                    heading = normalize_heading(section.get('heading', ''))
                    if heading:
                        kept_headings[heading] += 1
                    if section.get('children'):
                        count_headings(section['children'])
            count_headings(result['sections'])
        else:
            removed_count += 1
    
    print(f"   ✓ Filtered: {len(filtered_data)} papers kept, {removed_count} removed")
    print(f"   ✓ Sections: {original_sections} → {filtered_sections} (제거: {original_sections - filtered_sections})")
    print(f"   ✓ Removed headings: {len(removed_headings)} unique types")
    
    # 3. JSONL로 저장
    output_jsonl = OUTPUT_DIR / f"filtered_experimental_{timestamp}.jsonl"
    print(f"\n[3] Saving to JSONL: {output_jsonl}")
    
    with open(output_jsonl, 'w', encoding='utf-8') as f:
        for doc in filtered_data:
            f.write(json.dumps(doc, ensure_ascii=False) + '\n')
    
    print(f"   ✓ Saved {len(filtered_data)} papers")
    
    # 4. 통계 저장
    stats_path = OUTPUT_DIR / f"filtering_stats_{timestamp}.txt"
    print(f"\n[4] Saving statistics: {stats_path}")
    
    with open(stats_path, 'w', encoding='utf-8') as f:
        f.write("=" * 70 + "\n")
        f.write("GROBID Filtering Statistics\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        f.write(f"Original Papers: {len(data)}\n")
        f.write(f"Filtered Papers: {len(filtered_data)}\n")
        f.write(f"Removed Papers: {removed_count}\n\n")
        
        f.write(f"Original Sections: {original_sections}\n")
        f.write(f"Filtered Sections: {filtered_sections}\n")
        f.write(f"Removed Sections: {original_sections - filtered_sections}\n")
        f.write(f"Retention Rate: {filtered_sections / original_sections * 100:.1f}%\n\n")
        
        f.write("Top 30 Kept Section Headings:\n")
        f.write("-" * 70 + "\n")
        for idx, (heading, count) in enumerate(kept_headings.most_common(30), 1):
            f.write(f"{idx:3d}. {heading:50s} : {count:4d}\n")
    
    print(f"   ✓ Statistics saved")
    
    # 5. 유지된 헤딩 CSV 저장
    csv_path = OUTPUT_DIR / f"kept_headings_{timestamp}.csv"
    print(f"\n[5] Saving kept headings CSV: {csv_path}")
    
    import pandas as pd
    df = pd.DataFrame(kept_headings.most_common(), columns=['heading', 'count'])
    df['rank'] = range(1, len(df) + 1)
    df = df[['rank', 'heading', 'count']]
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    
    print(f"   ✓ Saved {len(df)} unique kept headings")
    
    # 6. 제거된 헤딩 CSV 저장
    removed_csv_path = OUTPUT_DIR / f"removed_headings_{timestamp}.csv"
    print(f"\n[6] Saving removed headings CSV: {removed_csv_path}")
    
    df_removed = pd.DataFrame(removed_headings.most_common(), columns=['heading', 'count'])
    df_removed['rank'] = range(1, len(df_removed) + 1)
    df_removed = df_removed[['rank', 'heading', 'count']]
    df_removed.to_csv(removed_csv_path, index=False, encoding='utf-8-sig')
    
    print(f"   ✓ Saved {len(df_removed)} unique removed headings")
    
    # 최종 요약
    print("\n" + "=" * 70)
    print("Filtering Complete!")
    print("=" * 70)
    print(f"\nResults saved to: {OUTPUT_DIR}")
    print(f"  - JSONL: {output_jsonl.name}")
    print(f"  - Statistics: {stats_path.name}")
    print(f"  - Kept Headings CSV: {csv_path.name}")
    print(f"  - Removed Headings CSV: {removed_csv_path.name}")
    print(f"\nRetention: {len(filtered_data)}/{len(data)} papers ({len(filtered_data)/len(data)*100:.1f}%)")
    print(f"Sections: {filtered_sections}/{original_sections} ({filtered_sections/original_sections*100:.1f}%)")
    print(f"Unique headings: {len(kept_headings)} kept, {len(removed_headings)} removed")
    print()


if __name__ == "__main__":
    main()
