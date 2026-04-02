# scripts/lib/condition_extractor.py
# -*- coding: utf-8 -*-
"""
Candidate Condition Pre-Extraction

Extracts potential conditions from text using regex patterns.
LLM then "selects/validates" these candidates instead of generating from scratch.

This makes condition extraction structural rather than probabilistic.
"""

import re
from typing import Dict, Any, Optional

# === REGEX PATTERNS for common conditions ===

# Areal current density: "10 mA cm-2" or "10 mA/cm2"
_RE_AREAL_J = re.compile(
    r"(\d+(?:\.\d+)?)\s*mA\s*(?:cm\s*[-–]?\s*2|/\s*cm\s*2)",
    re.IGNORECASE
)

# Specific current: "8 A g-1" or "8 A/g"
_RE_SPECIFIC_J = re.compile(
    r"(\d+(?:\.\d+)?)\s*A\s*(?:g\s*[-–]?\s*1|/\s*g)",
    re.IGNORECASE
)

# C-rate: "1C", "0.5C", "1 C-rate"
_RE_RATE_C = re.compile(
    r"(\d+(?:\.\d+)?)\s*C(?:[\s-]?rate)?(?!\w)",
    re.IGNORECASE
)

# Temperature: "25°C" or "25 °C"
_RE_TEMP = re.compile(
    r"(\d+(?:\.\d+)?)\s*°\s*C",
    re.IGNORECASE
)

# Areal capacity: "1 mAh cm-2" or "1 mAh/cm2"
_RE_AREAL_Q = re.compile(
    r"(\d+(?:\.\d+)?)\s*mAh?\s*(?:cm\s*[-–]?\s*2|/\s*cm\s*2)",
    re.IGNORECASE
)

# Specific capacity: "100 mAh g-1" or "100 mAh/g"
_RE_SPECIFIC_Q = re.compile(
    r"(\d+(?:\.\d+)?)\s*mAh?\s*(?:g\s*[-–]?\s*1|/\s*g)",
    re.IGNORECASE
)

# Frequency range: "0.01 Hz to 100 kHz" or "0.01-100000 Hz"
_RE_FREQ = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:m)?Hz\s*(?:to|[-–])\s*(\d+(?:\.\d+)?)\s*(?:k)?Hz",
    re.IGNORECASE
)

# AC amplitude: "5 mV" or "10 mV amplitude"
_RE_AC_AMP = re.compile(
    r"(\d+(?:\.\d+)?)\s*mV\s*(?:amplitude|amp)?",
    re.IGNORECASE
)

# Cycle duration: "1000 h" or "500 hours"
_RE_CYCLE_H = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:hours?|h\b)",
    re.IGNORECASE
)

# Number of cycles: "1000 cycles"
_RE_CYCLES = re.compile(
    r"(\d+)\s*cycles?",
    re.IGNORECASE
)


def extract_candidate_conditions(text: str) -> Dict[str, Any]:
    """
    Extract potential conditions from text using regex patterns.
    Returns dict with standardized slot names.
    
    Args:
        text: Raw text from chunk/evidence
    
    Returns:
        Dict of condition candidates with proper slot names
    """
    if not text:
        return {}
    
    candidates = {}
    
    # Areal current density
    m = _RE_AREAL_J.search(text)
    if m:
        candidates["areal_current_density_mA_cm2"] = float(m.group(1))
    
    # Specific current
    m = _RE_SPECIFIC_J.search(text)
    if m:
        candidates["specific_current_A_g"] = float(m.group(1))
    
    # C-rate
    m = _RE_RATE_C.search(text)
    if m:
        candidates["rate_C"] = float(m.group(1))
    
    # Temperature
    m = _RE_TEMP.search(text)
    if m:
        candidates["temperature_C"] = float(m.group(1))
    
    # Areal capacity
    m = _RE_AREAL_Q.search(text)
    if m:
        candidates["areal_capacity_mAh_cm2"] = float(m.group(1))
    
    # Specific capacity
    m = _RE_SPECIFIC_Q.search(text)
    if m:
        candidates["specific_capacity_mAh_g"] = float(m.group(1))
    
    # Frequency range
    m = _RE_FREQ.search(text)
    if m:
        low = float(m.group(1))
        high = float(m.group(2))
        # Adjust for kHz
        if "khz" in text[m.start():m.end()].lower():
            high *= 1000
        candidates["frequency_range_Hz"] = [low, high]
    
    # AC amplitude
    m = _RE_AC_AMP.search(text)
    if m:
        candidates["ac_amplitude_mV"] = float(m.group(1))
    
    # Cycle duration
    m = _RE_CYCLE_H.search(text)
    if m:
        candidates["cycle_duration_h"] = float(m.group(1))
    
    # Number of cycles
    m = _RE_CYCLES.search(text)
    if m:
        candidates["num_cycles"] = int(m.group(1))
    
    return candidates


def format_candidates_for_prompt(candidates: Dict[str, Any]) -> str:
    """
    Format candidates for injection into LLM prompt.
    
    Returns:
        JSON-formatted string for prompt variable substitution
    """
    if not candidates:
        return "{}"
    
    import json
    return json.dumps(candidates, indent=2, ensure_ascii=False)
