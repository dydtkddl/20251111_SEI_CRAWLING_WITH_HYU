"""Test _json_parse_strict with actual EXTRACT_RATE response"""
import sys
sys.path.insert(0, '.')

from pathlib import Path

# Load the actual LLM response
response_path = Path('cache/llm_traces/S000862232400438X/CASE-001_extract_rate_20260124_194142_response.txt')
raw = response_path.read_text(encoding='utf-8')

# Skip the header lines (until ======= line)
lines = raw.split('\n')
content_start = 0
for i, line in enumerate(lines):
    if line.startswith('==='):
        content_start = i + 1
        break

actual_response = '\n'.join(lines[content_start:])
print(f"Response length: {len(actual_response)}")
print(f"Starts with: {actual_response[:100]}")

# Import and test the function
from scripts.lib.llm_client import _json_parse_strict

try:
    result = _json_parse_strict(actual_response)
    measurements = result.get('measurements', [])
    print(f"\n✅ Parsed successfully!")
    print(f"Measurements count: {len(measurements)}")
    for m in measurements[:3]:
        print(f"  - {m.get('metric')}: {m.get('value')} @ {m.get('conditions', {}).get('specific_current_A_g')} A/g")
except Exception as e:
    print(f"\n❌ Parse failed: {e}")
    import traceback
    traceback.print_exc()
