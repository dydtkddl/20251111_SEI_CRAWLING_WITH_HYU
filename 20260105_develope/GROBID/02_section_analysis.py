"""
GROBID Section Heading Analysis
섹션 헤딩 정규화, 카운트, 시각화
"""
import json
import re
from pathlib import Path
from collections import Counter
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib
from datetime import datetime

# 한글 폰트 설정
matplotlib.rcParams['font.family'] = 'Malgun Gothic'
matplotlib.rcParams['axes.unicode_minus'] = False

# 입력/출력 경로
BASE_DIR = Path(__file__).resolve().parent
INPUT_JSON = BASE_DIR / "01_run_out_v2" / "grobid_results_all.json"
OUTPUT_DIR = BASE_DIR / "02_section_analysis"


def normalize_heading(heading: str) -> str:
    """
    섹션 헤딩에서 넘버링 제거하고 정규화
    
    Examples:
        "1. Introduction" -> "introduction"
        "2.1. Materials and methods" -> "materials and methods"
        "S1. Supplementary data" -> "supplementary data"
        "3.2.1 Results" -> "results"
    """
    if not heading:
        return ""
    
    # 앞의 넘버링 패턴 제거
    # 패턴: 숫자.숫자.숫자 또는 S숫자. 또는 숫자) 등
    patterns = [
        r'^[0-9]+\.?[0-9]*\.?[0-9]*\.?\s*',  # 1. 또는 1.1. 또는 1.1.1.
        r'^[SsAaBbCcDd][0-9]+\.?\s*',         # S1. A1. B1. 등
        r'^\([0-9]+\)\s*',                      # (1)
        r'^[0-9]+\)\s*',                        # 1)
        r'^\[[0-9]+\]\s*',                      # [1]
    ]
    
    normalized = heading
    for pattern in patterns:
        normalized = re.sub(pattern, '', normalized)
    
    # 추가 공백 제거 및 소문자 변환
    normalized = normalized.strip().lower()
    
    return normalized


def extract_all_headings(data: list) -> list:
    """
    모든 섹션 헤딩 추출 (재귀적으로)
    """
    headings = []
    
    def extract_from_sections(sections):
        for section in sections:
            heading = section.get('heading', '')
            if heading:
                headings.append(heading)
            
            # 자식 섹션 처리
            children = section.get('children', [])
            if children:
                extract_from_sections(children)
    
    for doc in data:
        if doc.get('has_error') or 'error' in doc:
            continue
        
        sections = doc.get('sections', [])
        extract_from_sections(sections)
    
    return headings


def main():
    """메인 실행 함수"""
    print("=" * 60)
    print("GROBID Section Heading Analysis")
    print("=" * 60)
    
    # 출력 디렉토리 생성
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 1. JSON 파일 로드
    print(f"\n[1] Loading JSON from: {INPUT_JSON}")
    if not INPUT_JSON.exists():
        print(f"ERROR: File not found: {INPUT_JSON}")
        return
    
    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print(f"   ✓ Loaded {len(data)} papers")
    
    # 2. 모든 헤딩 추출
    print("\n[2] Extracting all section headings...")
    raw_headings = extract_all_headings(data)
    print(f"   ✓ Extracted {len(raw_headings)} total headings")
    
    # 3. 헤딩 정규화
    print("\n[3] Normalizing headings (removing numbering, lowercasing)...")
    normalized_headings = [normalize_heading(h) for h in raw_headings]
    
    # 빈 문자열 제거
    normalized_headings = [h for h in normalized_headings if h]
    print(f"   ✓ {len(normalized_headings)} headings after normalization")
    
    # 4. 카운트 및 정렬
    print("\n[4] Counting and sorting...")
    heading_counts = Counter(normalized_headings)
    
    # 빈도순으로 정렬
    sorted_headings = heading_counts.most_common()
    print(f"   ✓ Found {len(sorted_headings)} unique headings")
    
    # 5. CSV로 저장
    csv_path = OUTPUT_DIR / f"section_headings_{timestamp}.csv"
    print(f"\n[5] Saving to CSV: {csv_path}")
    
    df = pd.DataFrame(sorted_headings, columns=['heading', 'count'])
    df['rank'] = range(1, len(df) + 1)
    df = df[['rank', 'heading', 'count']]
    
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f"   ✓ Saved {len(df)} rows")
    
    # 상위 20개 출력
    print("\n[Top 20 Most Common Headings]")
    print("-" * 60)
    for rank, (heading, count) in enumerate(sorted_headings[:20], 1):
        print(f"{rank:3d}. {heading:40s} : {count:5d}")
    
    # 6. 시각화
    print("\n[6] Creating visualizations...")
    
    # 6-1. 상위 30개 막대 그래프
    fig1_path = OUTPUT_DIR / f"top30_headings_{timestamp}.png"
    
    top_30 = sorted_headings[:30]
    headings_top30 = [h[0] for h in top_30]
    counts_top30 = [h[1] for h in top_30]
    
    plt.figure(figsize=(14, 10))
    bars = plt.barh(range(len(headings_top30)), counts_top30, color='#e94560')
    plt.yticks(range(len(headings_top30)), headings_top30, fontsize=10)
    plt.xlabel('Count', fontsize=12, fontweight='bold')
    plt.title('Top 30 Most Common Section Headings', fontsize=14, fontweight='bold')
    plt.gca().invert_yaxis()
    
    # 값 레이블 추가
    for i, (bar, count) in enumerate(zip(bars, counts_top30)):
        plt.text(count + max(counts_top30) * 0.01, i, str(count), 
                va='center', fontsize=9, fontweight='bold')
    
    plt.tight_layout()
    plt.savefig(fig1_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"   ✓ Saved: {fig1_path}")
    
    # 6-2. 상위 10개 파이 차트
    fig2_path = OUTPUT_DIR / f"top10_pie_{timestamp}.png"
    
    top_10 = sorted_headings[:10]
    others_count = sum(count for _, count in sorted_headings[10:])
    
    labels = [h[0] for h in top_10] + ['others']
    sizes = [h[1] for h in top_10] + [others_count]
    
    colors = ['#e94560', '#f72585', '#7209b7', '#4cc9f0', '#4361ee', 
              '#f9c74f', '#ff6b6b', '#00f5d4', '#ff9770', '#90be6d', '#cccccc']
    
    plt.figure(figsize=(12, 8))
    plt.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90, colors=colors)
    plt.title('Top 10 Section Headings Distribution', fontsize=14, fontweight='bold')
    plt.axis('equal')
    plt.tight_layout()
    plt.savefig(fig2_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"   ✓ Saved: {fig2_path}")
    
    # 6-3. 빈도 분포 히스토그램
    fig3_path = OUTPUT_DIR / f"frequency_distribution_{timestamp}.png"
    
    counts_only = [count for _, count in sorted_headings]
    
    plt.figure(figsize=(12, 6))
    plt.hist(counts_only, bins=50, color='#4cc9f0', edgecolor='black', alpha=0.7)
    plt.xlabel('Count', fontsize=12, fontweight='bold')
    plt.ylabel('Number of Unique Headings', fontsize=12, fontweight='bold')
    plt.title('Frequency Distribution of Section Headings', fontsize=14, fontweight='bold')
    plt.yscale('log')
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(fig3_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"   ✓ Saved: {fig3_path}")
    
    # 7. 통계 요약 저장
    summary_path = OUTPUT_DIR / f"summary_{timestamp}.txt"
    print(f"\n[7] Saving summary: {summary_path}")
    
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        f.write("GROBID Section Heading Analysis Summary\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Analysis Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Input File: {INPUT_JSON}\n\n")
        
        f.write(f"Total Papers: {len(data)}\n")
        f.write(f"Total Headings (raw): {len(raw_headings)}\n")
        f.write(f"Total Headings (normalized): {len(normalized_headings)}\n")
        f.write(f"Unique Headings: {len(sorted_headings)}\n\n")
        
        f.write("Top 50 Most Common Headings:\n")
        f.write("-" * 60 + "\n")
        for rank, (heading, count) in enumerate(sorted_headings[:50], 1):
            f.write(f"{rank:3d}. {heading:45s} : {count:5d}\n")
    
    print(f"   ✓ Summary saved")
    
    # 최종 요약
    print("\n" + "=" * 60)
    print("Analysis Complete!")
    print("=" * 60)
    print(f"\nResults saved to: {OUTPUT_DIR}")
    print(f"  - CSV: {csv_path.name}")
    print(f"  - Top 30 Bar Chart: {fig1_path.name}")
    print(f"  - Top 10 Pie Chart: {fig2_path.name}")
    print(f"  - Frequency Distribution: {fig3_path.name}")
    print(f"  - Summary: {summary_path.name}")
    print()


if __name__ == "__main__":
    main()
