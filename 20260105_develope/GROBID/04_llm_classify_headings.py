"""
LLM 기반 섹션 헤딩 분류
Ollama를 사용해서 섹션이 experimental/preparation/synthesis 관련인지 판별
"""
import json
import argparse
from pathlib import Path
from collections import Counter
from typing import Dict, List
import requests
from tqdm import tqdm
from datetime import datetime


BASE_DIR = Path(__file__).resolve().parent
INPUT_JSON = BASE_DIR / "01_run_out_v2" / "grobid_results_all.json"
OUTPUT_DIR = BASE_DIR / "04_llm_classification"
CLASSIFICATION_PROMPT = """You are an expert in analyzing scientific paper structure for aqueous zinc-ion batteries (AZIB), focusing on Zn ANODE protective layers formed EX-SITU (pre-cycling coatings / artificial interphases / engineered interfaces).

I will give you a section heading from a scientific paper.
Your job: decide whether this heading is LIKELY to contain EXPERIMENTAL METHODS / MATERIAL PREPARATION / SYNTHESIS / FABRICATION procedures RELEVANT to Zn anode protection (ex-situ) or AZIB cell assembly.

IMPORTANT: We are optimizing for RECIPE / PREPARATION evidence, not results.

========================
NORMALIZATION
- Treat the heading case-insensitively.
- Ignore punctuation/noise; focus on key terms.

========================
DECISION PRIORITY (apply in this order)
1) HARD-NO (results/analysis/measurement-only) rules first
2) HARD-YES (prep/fabrication/assembly) rules next
3) Otherwise default NO

========================
HARD-NO (OVERRIDE) RULES ❌
Answer NO if the heading is clearly RESULTS / DISCUSSION / ANALYSIS oriented,
even if MOF/Zeolite/coating materials appear.

A) Results/Discussion/Analysis signals (non-exhaustive):
- "results", "discussion", "conclusion", "summary", "findings", "analysis"
- "electrochemical performance", "performance", "cycling", "rate capability", "capacity"
- "mechanism", "regulation", "evolution", "behavior", "kinetics", "dynamics"
- "corrosion resistance", "dendrite suppression", "nucleation", "deposition behavior"
- "morphology evolution", "plating/stripping reversibility" (when phrased as behavior/results)

=> If these dominate the heading, decision MUST be NO.

B) Characterization-only sections (exclude even if MOF/Zeolite appears):
- If the heading contains "characterization", "characterizations", "structure", "morphology",
  or technique lists like "XRD", "SEM", "TEM", "XPS", "Raman", "FTIR", "BET", "AFM"
AND it does NOT contain any fabrication/preparation verbs (see FABRICATION VERBS below)
=> MUST be NO.

Examples that MUST be NO:
- "characterization techniques"
- "characterization of protective layers"  (unless it also includes preparation/fabrication verbs)
- "structure and morphology of ..." (unless it also includes preparation/fabrication verbs)

C) Electrochemical measurement/testing-only sections:
- If the heading contains "electrochemical measurement(s)", "electrochemical measurements",
  "electrochemical testing", "EIS", "CV", "LSV", "GCD"
AND it does NOT contain any fabrication/preparation verbs
=> MUST be NO.

Examples that MUST be NO:
- "electrochemical measurements"
- "electrochemical measurements of Z8-SA@Zn"
- "electrochemical measurement."

D) Non-target device context (hard exclude unless explicit Zn-anode protective layer fabrication is stated):
- "supercapacitor", "hybrid supercapacitor", "ZHSC", "zinc-ion hybrid supercapacitors"
=> NO (unless the heading explicitly indicates protective-layer fabrication on Zn anode with fabrication verbs)

========================
KEY DEFINITIONS (signals)
[FABRICATION / PREPARATION VERBS]  ✅
Any of:
- "preparation", "prepare", "fabrication", "fabricate", "synthesis", "synthesize",
  "construction", "construct", "assembly", "assemble",
  "coating", "coat", "deposition", "deposit", "growth", "grow",
  "grafting", "casting", "spraying", "dip-coating", "dip coating",
  "soaking", "dipping", "immersion", "immersed",
  "drying", "annealing", "calcination", "curing"

[ZN-SURFACE / PROTECTIVE-LAYER SIGNALS] (strong) ✅
Any of:
- "@Zn", "Zn@", "coated Zn", "Zn foil coated", "modified Zn", "treated Zn", "pretreated Zn", "Zn foil"
- "protective layer" + (Zn OR anode)
- "artificial layer" + (Zn OR anode)
- "interface layer" + (Zn OR anode)
- "layer on Zn", "grown on Zn", "deposited on Zn"

[COATING-MATERIAL SIGNALS] (allow YES even without Zn@ when paired with FABRICATION VERBS) ✅
These materials are commonly used as ex-situ coating / artificial interphase components:
- Frameworks: "MOF", "metal-organic framework", "ZIF", "MIL", "UiO", "COF", "covalent organic framework",
  "zeolite", "ZSM", "SAPO"
- 2D/porous/interphase materials: "LDH", "mxene", "graphene", "rGO", "CNT", "carbon nanofiber", "CNF", "carbon cloth"
- Common coating polymers/binders/interphase formers (examples): "PVDF", "PVA", "PAA", "alginate", "chitosan",
  "cellulose", "PEO", "PEG", "PAM", "gel polymer electrolyte", "GPE", "hydrogel", "film", "membrane"

NOTE: These keywords alone do NOT guarantee YES. They must be paired with FABRICATION VERBS,
and must NOT be a results/characterization-only heading (HARD-NO rules still win).

[CATHODE-ONLY EXCLUSION]
- If the heading is ONLY about cathode preparation/synthesis (contains "cathode" or a clear cathode-only phrase)
and does NOT mention anode/Zn/negative electrode/cell assembly
=> NO.
- If it mentions BOTH electrodes or full cell assembly => can be YES.

========================
HARD-YES (OVERRIDE) RULES ✅
Answer YES if ANY of the following is true (and HARD-NO did not trigger):

1) Zn-surface protective-layer fabrication:
- (ZN-SURFACE / PROTECTIVE-LAYER SIGNALS present)
AND (FABRICATION / PREPARATION VERBS present OR the heading explicitly denotes a fabricated coated specimen)
=> YES

Examples:
- "preparation of MOF@Zn"
- "fabrication of ... layer on Zn anodes"
- "synthesis of TiO2 coated Zn foils (Zn@TiO2 foils)"
- "preparation of ... @Zn anode"

2) MOF/Zeolite (or other coating-material) preparation that likely feeds coating/artificial interphase:
- (COATING-MATERIAL SIGNALS present)
AND (FABRICATION / PREPARATION VERBS present)
AND NOT cathode-only
=> YES (even if Zn@ is absent)

Examples:
- "preparation of anodically exfoliated graphenes" => YES (material prep; may be used as coating)
- "prepare the electronegative MOF" => YES
- "synthesis of materials" => YES only if not clearly cathode-only; otherwise NO

3) Cell/battery assembly or electrolyte preparation:
- "cell assembly", "battery assembly", "coin cell assembly", "fabrication of batteries"
- "electrolyte preparation", "aqueous electrolyte preparation"
- "electrode preparation" / "anode preparation" / "negative electrode preparation" (not cathode-only)
=> YES

4) Broad experimental/methods umbrella sections:
- "experimental", "experimental section", "experimental details",
  "materials and methods", "material and methods", "methods", "experimental part"
=> YES (umbrella method sections likely contain preparation/recipe content)

========================
DEFAULT LOGIC
- If HARD-NO triggered => NO (high confidence).
- Else if any HARD-YES rule triggered => YES.
- Otherwise => NO.

Section heading: "{heading}"

Respond ONLY with a JSON object in this exact format:
{{"decision": "YES/NO", "confidence": 0.0-1.0, "reason": "brief explanation"}}"""

def normalize_heading(heading: str) -> str:
    """헤딩 정규화 (넘버링 제거)"""
    import re
    
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


def extract_all_headings(data: list) -> Counter:
    """모든 섹션 헤딩 추출 및 카운트"""
    headings = []
    
    def extract_from_sections(sections):
        for section in sections:
            heading = section.get('heading', '')
            if heading:
                normalized = normalize_heading(heading)
                if normalized and len(normalized) > 2:
                    headings.append(normalized)
            
            children = section.get('children', [])
            if children:
                extract_from_sections(children)
    
    for doc in data:
        if doc.get('has_error') or 'error' in doc:
            continue
        
        sections = doc.get('sections', [])
        extract_from_sections(sections)
    
    return Counter(headings)


def call_ollama_llm(heading: str, ollama_url: str, model: str) -> Dict:
    """Ollama LLM 호출"""
    prompt = CLASSIFICATION_PROMPT.format(heading=heading)
    
    try:
        response = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "top_p": 0.9,
                }
            },
            timeout=30
        )
        
        if response.status_code != 200:
            return {"decision": "ERROR", "confidence": 0.0, "reason": f"HTTP {response.status_code}"}
        
        result = response.json()
        llm_output = result.get('response', '').strip()
        
        # JSON 파싱
        try:
            # JSON 블록 찾기
            if '```json' in llm_output:
                llm_output = llm_output.split('```json')[1].split('```')[0].strip()
            elif '```' in llm_output:
                llm_output = llm_output.split('```')[1].split('```')[0].strip()
            
            parsed = json.loads(llm_output)
            
            # 검증
            if 'decision' not in parsed:
                return {"decision": "ERROR", "confidence": 0.0, "reason": "No decision field"}
            
            decision = parsed['decision'].upper()
            if decision not in ['YES', 'NO']:
                return {"decision": "ERROR", "confidence": 0.0, "reason": f"Invalid decision: {decision}"}
            
            return {
                "decision": decision,
                "confidence": float(parsed.get('confidence', 0.5)),
                "reason": parsed.get('reason', '')
            }
            
        except json.JSONDecodeError as e:
            return {"decision": "ERROR", "confidence": 0.0, "reason": f"JSON parse error: {str(e)[:100]}"}
    
    except requests.Timeout:
        return {"decision": "ERROR", "confidence": 0.0, "reason": "Timeout"}
    except Exception as e:
        return {"decision": "ERROR", "confidence": 0.0, "reason": f"Error: {str(e)[:100]}"}


def main():
    parser = argparse.ArgumentParser(description="LLM 기반 섹션 헤딩 분류")
    parser.add_argument("--ollama_url", default="http://localhost:11434", help="Ollama server URL")
    parser.add_argument("--llm_model", default="qwen2.5:14b-instruct", help="LLM model name")
    parser.add_argument("--min_count", type=int, default=1, help="Minimum heading count to classify")
    args = parser.parse_args()
    
    print("=" * 70)
    print("LLM-Based Section Heading Classification")
    print("=" * 70)
    print(f"Ollama URL: {args.ollama_url}")
    print(f"Model: {args.llm_model}")
    print()
    
    # 출력 디렉토리 생성
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 1. JSON 로드
    print(f"[1] Loading: {INPUT_JSON}")
    if not INPUT_JSON.exists():
        print("ERROR: File not found!")
        return
    
    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print(f"   ✓ Loaded {len(data)} papers")
    
    # 2. 헤딩 추출
    print("\n[2] Extracting unique headings...")
    heading_counts = extract_all_headings(data)
    
    # 빈도순 정렬 & 필터링
    filtered_headings = [(h, c) for h, c in heading_counts.most_common() if c >= args.min_count]
    
    print(f"   ✓ Found {len(heading_counts)} unique headings")
    print(f"   ✓ Filtering: {len(filtered_headings)} headings (count >= {args.min_count})")
    
    # 3. LLM 분류
    print(f"\n[3] Classifying with LLM ({args.llm_model})...")
    
    classifications = {}
    yes_count = 0
    no_count = 0
    error_count = 0
    
    for heading, count in tqdm(filtered_headings, desc="Classifying"):
        result = call_ollama_llm(heading, args.ollama_url, args.llm_model)
        
        classifications[heading] = {
            "count": count,
            "decision": result["decision"],
            "confidence": result["confidence"],
            "reason": result["reason"]
        }
        
        if result["decision"] == "YES":
            yes_count += 1
        elif result["decision"] == "NO":
            no_count += 1
        else:
            error_count += 1
    
    print(f"\n   ✓ Classification complete!")
    print(f"      YES: {yes_count}, NO: {no_count}, ERROR: {error_count}")
    
    # 4. 결과 저장 (JSON)
    output_json = OUTPUT_DIR / f"classification_results_{timestamp}.json"
    print(f"\n[4] Saving results: {output_json}")
    
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(classifications, f, ensure_ascii=False, indent=2)
    
    print(f"   ✓ Saved {len(classifications)} classifications")
    
    # 5. CSV 저장 (사람이 보기 쉽게)
    output_csv = OUTPUT_DIR / f"classification_results_{timestamp}.csv"
    print(f"\n[5] Saving CSV: {output_csv}")
    
    import pandas as pd
    
    rows = []
    for heading, info in classifications.items():
        rows.append({
            'heading': heading,
            'count': info['count'],
            'decision': info['decision'],
            'confidence': info['confidence'],
            'reason': info['reason']
        })
    
    df = pd.DataFrame(rows)
    df = df.sort_values(['decision', 'count'], ascending=[True, False])
    df.to_csv(output_csv, index=False, encoding='utf-8-sig')
    
    print(f"   ✓ CSV saved")
    
    # 6. 요약 통계
    print("\n[6] Summary Statistics:")
    print("-" * 70)
    print(f"Total headings classified: {len(classifications)}")
    print(f"  YES (experimental): {yes_count} ({yes_count/len(classifications)*100:.1f}%)")
    print(f"  NO (non-experimental): {no_count} ({no_count/len(classifications)*100:.1f}%)")
    print(f"  ERROR: {error_count} ({error_count/len(classifications)*100:.1f}%)")
    
    # YES 예시
    yes_examples = [(h, c) for h, c in classifications.items() if c['decision'] == 'YES']
    if yes_examples:
        print("\n✓ YES Examples (top 10 by count):")
        for heading, info in sorted(yes_examples, key=lambda x: x[1]['count'], reverse=True)[:10]:
            print(f"   • {heading} (count: {info['count']}, conf: {info['confidence']:.2f})")
    
    # NO 예시
    no_examples = [(h, c) for h, c in classifications.items() if c['decision'] == 'NO']
    if no_examples:
        print("\n✗ NO Examples (top 10 by count):")
        for heading, info in sorted(no_examples, key=lambda x: x[1]['count'], reverse=True)[:10]:
            print(f"   • {heading} (count: {info['count']}, conf: {info['confidence']:.2f})")
    
    print("\n" + "=" * 70)
    print("Classification Complete!")
    print("=" * 70)
    print(f"\nResults saved to: {OUTPUT_DIR}")
    print(f"  - JSON: {output_json.name}")
    print(f"  - CSV: {output_csv.name}")
    print()


if __name__ == "__main__":
    main()
