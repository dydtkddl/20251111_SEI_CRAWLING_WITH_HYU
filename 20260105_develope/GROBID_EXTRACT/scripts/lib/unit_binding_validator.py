# scripts/lib/unit_binding_validator.py
"""
Unit-Binding QC Validator v2.0

Round 4 fixes:
- Hard range validation (transference_number: 0~1)
- Unit-binding checks (number must be adjacent to unit)
- Forbidden context detection (Fig., Zn2+, cm 2, etc.)

Round 6 enhancements:
- DFT energy sign preservation and chemistry filter
- Condition number filtering (days, M, cycles)
- Unit-metric compatibility check
- mA↔uA automatic remap

Reference: Round 4, 5, 6 Quality Reports
"""
from __future__ import annotations
import re
import math
import copy
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# METRIC VALIDATION RULES
# ============================================================================

METRIC_RULES = {
    # Critical: transference_number (0~1 hard constraint)
    "transference_number": {
        "hard_range": (0.0, 1.0),
        "forbidden_patterns": [
            r"Zn\s*\d+\s*[+\-]",      # Zn 2+ → 2 오염
            r"Fig\.|Figure|Table|S\d+",
            r"\(\s*[a-z]\s*\)",         # (a), (b) 등
        ],
        "required_keywords": ["transference", "transfer", "t+", "t⁺"],
    },
    
    # Critical: cycle_life_hours (unit binding required)
    "cycle_life_hours": {
        "hard_range": (1, 100000),
        "required_unit_regex": r"\bh\b|hours?\b",
        "forbidden_patterns": [
            r"mA\s*cm",               # 5 mA cm-2 → 5 h 오염
            r"cm\s*\d",               # cm 2 지수 분리
            r"Fig\.|Figure|Table|S\d+",
            r"\[\s*\d+\s*\]",         # 레퍼런스 [9]
        ],
        "forbidden_values": [2, 5, 10],  # 대표 오염값
    },
    
    # Critical: specific_capacity_mAh_g (Round 10: enhanced current density detection)
    "specific_capacity_mAh_g": {
        "hard_range": (10, 5000),
        "required_unit_regex": r"mAh?\s*/?\s*g|mAh?\s*g\s*[-−]?\s*1",
        "forbidden_patterns": [
            r"\d+\s*A\s*g\s*[-−]?\s*1",   # "20 A g⁻¹" 전류밀도 차단
            r"\d+\s*A\s*/?\s*g",           # "20 A/g" 전류밀도 차단
            r"at\s+\d+\.?\d*\s*A",         # "at 20 A g⁻¹" 패턴
            r"even\s+at\s+\d+",            # Round 10: "even at 20 A" 패턴
            r"cycle|cycles",               # 1600 cycles → capacity
            r"Fig\.|Figure|Table|S\d+",
            r"retention",                  # "용량 유지율" 문맥 차단
            r"C\.?\s*E\.?|coulombic",      # CE 문맥 차단
            r"mA\s*cm",                    # mA cm⁻² 전류밀도 차단
        ],
        # Round 10: 값이 소수점 이하 없이 10, 20 등 깔끔하면 전류밀도 의심
        "suspicious_values": [1, 5, 10, 20, 50, 100],  # 대표 전류밀도값
        "remap_if": [
            {
                # % 가 있고 mAh 가 없으면 → retention
                "condition": {
                    "quote_contains": "%", 
                    "quote_lacks": "mAh",
                    "value_range": (0, 100)
                },
                "remap_to": "capacity_retention_pct",
            },
            {
                "condition": {"quote_contains": "cycles", "value_range": (100, 1e8)},
                "remap_to": "cycle_life_cycles",
            },
        ],
    },
    
    # Warning: capacity_retention_pct (Round 8: unit-block to prevent mAh/g misroute)
    "capacity_retention_pct": {
        "hard_range": (0, 100),
        "required_unit_regex": r"%",
        "forbidden_patterns": [r"Fig\.|S\d+"],
        "unit_block": ["mAh/g", "mAh g", "mA h g"],  # Round 8: 이 단위면 리매핑 거부
    },
    
    # Warning: coulombic_efficiency_pct
    "coulombic_efficiency_pct": {
        "hard_range": (0, 100),
        "required_unit_regex": r"%",
        "forbidden_patterns": [r"Fig\.|S\d+"],
    },
    
    # Corrosion current (Round 8: mA detection with auto-remap)
    "corrosion_current_density_uAcm2": {
        "hard_range": (0, 1e6),
        "forbidden_patterns": [
            r"\d+\s*days?",          # "6 days" → 6 uA/cm2 오염 방지
            r"\d+\s*M\b",            # "2 M" 전해질 농도 오염 방지
            r"Fig\.|Figure|Table|S\d+",
        ],
        "required_unit_regex": r"[uμµlL]A\s*/?\s*cm|mA\s*/?\s*cm",  # lA = OCR오류로 μA
        "unit_conversion": {
            # Round 8: evidence에 mA가 있으면 값을 1000배 변환
            "detect_pattern": r"mA\s*cm",
            "scale_factor": 1000,  # mA → uA
            "add_flag": "UNIT_CONVERTED_FROM_mA",
        },
    },
    
    # === ROUND 6: DFT Energy with sign preservation ===
    "zn_adsorption_energy_eV": {
        "hard_range": (-10, 5),  # DFT adsorption은 보통 음수 (발열)
        "forbidden_patterns": [
            r"Zn\s*\d+\s*[+\-]",      # Zn2+ → 2 오염
            r"sp\d",                   # sp3 → 3 오염
            r"Li\d",                   # Li2SO4 → 2 오염
            r"[A-Z][a-z]?\d+[+\-]?",  # 모든 화학식 숫자
            r"Fig\.|Figure|Table|S\d+",
        ],
        "required_unit_regex": r"eV\b",
        "sign_check": True,  # 원문에 음수면 값도 음수여야 함
    },
    
    # === ROUND 6: cycle_life_cycles 강화 ===
    "cycle_life_cycles": {
        "hard_range": (1, 1e8),
        "required_unit_regex": r"cycle|cycles",
        "forbidden_patterns": [
            r"Fig\.|Figure|Table|S\d+",
            r"cm\s*\d",
            r"\bh\b",  # hours와 혼동 방지
        ],
    },
    
    # === ROUND 6: contact_angle_deg ===
    "contact_angle_deg": {
        "hard_range": (0, 180),
        "required_unit_regex": r"°|deg|degree",
        "forbidden_patterns": [r"Fig\.|S\d+"],
    },
    
    # === ROUND 6: overpotential_mV ===
    "overpotential_mV": {
        "hard_range": (0, 5000),
        "required_unit_regex": r"mV\b",
        "required_keywords": ["overpotential", "η", "nucleation", "deposition"],
        "forbidden_patterns": [r"Fig\.|S\d+", r"V\s*vs"],
    },
    
    "nucleation_overpotential_mV": {
        "hard_range": (0, 5000),
        "required_unit_regex": r"mV\b",
        "required_keywords": ["nucleation", "initial"],
        "forbidden_patterns": [r"Fig\.|S\d+"],
    },
    
    "deposition_overpotential_mV": {
        "hard_range": (0, 5000),
        "required_unit_regex": r"mV\b",
        "required_keywords": ["deposition", "steady", "plating"],
        "forbidden_patterns": [r"Fig\.|S\d+"],
    },
    
    # === ROUND 7: Protective layer loading (hallucination prevention) ===
    "protective_layer_loading_mg_cm2": {
        "hard_range": (0.01, 100),  # 합리적 범위
        "required_unit_regex": r"mg\s*/?\s*cm",  # 반드시 단위 존재
        "forbidden_patterns": [
            r"loading\s+capacity",       # 개념 표현 차단
            r"investigate.*loading",     # 연구 목적 문장 차단
            r"Fig\.|Figure|Table|S\d+",
        ],
    },
}

# ============================================================================
# GLOBAL CONDITION NUMBER PATTERNS (Round 6)
# ============================================================================
CONDITION_NUMBER_PATTERNS = [
    r"(\d+)\s*days?",      # 6 days
    r"(\d+)\s*M\b",        # 2 M molarity
    r"(\d+)\s*wt\.?\s*%",  # weight percent condition
]

# ============================================================================
# UNIT-METRIC COMPATIBILITY (Round 6)
# ============================================================================
UNIT_METRIC_COMPAT = {
    # metric suffix_pct은 반드시 % 단위
    "capacity_retention_pct": ["%", "percent"],
    "coulombic_efficiency_pct": ["%", "percent"],
    # mAh/g 단위는 specific_capacity로
    "specific_capacity_mAh_g": ["mAh/g", "mA h/g", "mAh g-1"],
}


def check_dft_sign(quote: str, value: Any, metric: str) -> Tuple[bool, Optional[float]]:
    """
    Check if DFT energy value has correct sign.
    Returns: (is_valid, corrected_value_or_None)
    
    DFT adsorption energy is typically negative (exothermic).
    If evidence has negative sign but value is positive, auto-correct.
    """
    if "_energy_eV" not in metric and "_binding_" not in metric:
        return (True, None)
    
    try:
        v = float(value)
    except (ValueError, TypeError):
        return (True, None)
    
    # Check if quote contains negative value pattern
    has_negative_in_quote = bool(re.search(r"[-−]\s*\d+\.?\d*\s*eV", quote, re.IGNORECASE))
    
    if has_negative_in_quote and v > 0:
        # Value should be negative but is positive → auto-correct
        return (False, -abs(v))
    
    return (True, None)


def check_unit_metric_compat(metric: str, unit: str, quote: str) -> Tuple[str, Optional[str]]:
    """
    Check if unit is compatible with metric.
    Returns: (decision, remap_metric_or_None)
    
    e.g., capacity_retention_pct with unit=mAh/g → REMAP to specific_capacity_mAh_g
    """
    if not unit:
        return ("KEEP", None)
    
    unit_lower = unit.lower().replace(" ", "")
    
    # If metric ends with _pct but unit is not %
    if metric.endswith("_pct"):
        if "%" not in unit and "percent" not in unit_lower:
            # Check if it looks like capacity unit
            if "mah" in unit_lower or "ah" in unit_lower:
                return ("REMAP", "specific_capacity_mAh_g")
    
    # If unit is mAh/g but metric is not specific_capacity
    if ("mah" in unit_lower and "g" in unit_lower) or "mahg" in unit_lower:
        if metric != "specific_capacity_mAh_g" and "capacity" in metric.lower():
            return ("REMAP", "specific_capacity_mAh_g")
    
    return ("KEEP", None)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def num_variants(value: Any) -> List[str]:
    """Generate string variants of a number for pattern matching."""
    try:
        v = float(value)
        if not math.isfinite(v):
            return [str(value)]
        
        variants = {str(value), f"{v:g}"}
        
        # Integer variant
        if abs(v - round(v)) < 1e-9:
            variants.add(str(int(round(v))))
        
        # Decimal variants
        variants.add(f"{v:.1f}")
        variants.add(f"{v:.2f}")
        
        return [s for s in variants if s and s != "nan"]
    except (ValueError, TypeError):
        return [str(value)]


def binds_to_unit(quote: str, value: Any, unit_regex: str) -> bool:
    """
    Check if value is directly bound to unit in quote.
    
    "265.8 mAh g-1" → binds("265.8", "mAh g") = True
    "0.2 A g-1 ... 265.8 mAh g-1" → binds("0.2", "mAh g") = False
    """
    if not quote or value is None:
        return False
    
    for v in num_variants(value):
        # Pattern: number followed by unit (with optional space)
        pattern = rf"(?<![0-9.]){re.escape(v)}\s*{unit_regex}"
        if re.search(pattern, quote, re.IGNORECASE):
            return True
    
    return False


def binds_to_forbidden(quote: str, value: Any, forbidden_regex: str) -> bool:
    """Check if value is in forbidden context (e.g., near Fig., Zn2+)."""
    if not quote or value is None:
        return False
    
    for v in num_variants(value):
        # Check if value appears near forbidden pattern (within ~20 chars)
        v_escaped = re.escape(v)
        
        # Pattern: forbidden pattern near the value
        pattern = rf"({forbidden_regex}).{{0,20}}{v_escaped}|{v_escaped}.{{0,20}}({forbidden_regex})"
        if re.search(pattern, quote, re.IGNORECASE):
            return True
    
    return False


def in_reference_brackets(quote: str, value: Any) -> bool:
    """Check if value appears in reference brackets like [9]."""
    if not quote or value is None:
        return False
    
    for v in num_variants(value):
        pattern = rf"\[\s*{re.escape(v)}\s*\]"
        if re.search(pattern, quote):
            return True
    
    return False


def value_equals_condition(m: Dict, condition_key: str) -> bool:
    """Check if value equals a condition value (likely contamination)."""
    value = m.get("value")
    conditions = m.get("conditions", {}) or {}
    cond_val = conditions.get(condition_key)
    
    if value is None or cond_val is None:
        return False
    
    try:
        return abs(float(value) - float(cond_val)) < 1e-9
    except (ValueError, TypeError):
        return False


# ============================================================================
# CORE VALIDATION FUNCTION
# ============================================================================

def validate_measurement(m: Dict) -> Tuple[str, Dict]:
    """
    Validate a single measurement record.
    
    Returns:
        (decision, updated_measurement)
        decision: "KEEP" | "DROP" | "REMAP"
    """
    metric = m.get("metric", "")
    value = m.get("value")
    quote = (m.get("evidence") or {}).get("quote", "") or ""
    
    rules = METRIC_RULES.get(metric)
    
    # No rules = KEEP
    if not rules:
        return ("KEEP", m)
    
    violations = []
    
    # === 1. Hard Range Check ===
    hard_range = rules.get("hard_range")
    if hard_range:
        try:
            v = float(value)
            if v < hard_range[0] or v > hard_range[1]:
                violations.append({
                    "rule": "HARD_RANGE",
                    "detail": f"value={v} outside [{hard_range[0]}, {hard_range[1]}]"
                })
                # Immediate DROP for range violation
                m_copy = copy.deepcopy(m)
                m_copy["validator"] = {
                    "decision": "DROP",
                    "violations": violations,
                }
                return ("DROP", m_copy)
        except (ValueError, TypeError):
            pass
    
    # === 1.5 Unit Conversion (Round 9: mA→uA scaling) ===
    unit_conv = rules.get("unit_conversion")
    if unit_conv:
        detect_pattern = unit_conv.get("detect_pattern", "")
        scale_factor = unit_conv.get("scale_factor", 1)
        add_flag = unit_conv.get("add_flag", "")
        
        if detect_pattern and re.search(detect_pattern, quote, re.IGNORECASE):
            try:
                original_value = float(value)
                converted_value = original_value * scale_factor
                m = copy.deepcopy(m)
                m["value"] = converted_value
                violations.append({
                    "rule": "UNIT_CONVERSION",
                    "detail": f"Converted {original_value}→{converted_value} (×{scale_factor})"
                })
                if add_flag:
                    if "tags" not in m:
                        m["tags"] = {}
                    m["tags"]["qc_flags"] = m["tags"].get("qc_flags", []) + [add_flag]
                value = converted_value  # Update value for subsequent checks
                logger.info(f"  Unit conversion: {original_value} → {converted_value}")
            except (ValueError, TypeError):
                pass
    
    # === 2. Forbidden Values Check ===
    forbidden_values = rules.get("forbidden_values", [])
    try:
        v = float(value)
        for fv in forbidden_values:
            if abs(v - fv) < 1e-9:
                violations.append({
                    "rule": "FORBIDDEN_VALUE",
                    "detail": f"value={v} in forbidden list"
                })
                m_copy = copy.deepcopy(m)
                m_copy["validator"] = {
                    "decision": "DROP",
                    "violations": violations,
                }
                return ("DROP", m_copy)
    except (ValueError, TypeError):
        pass
    
    # === 2.5 Suspicious Values Check (Round 11) ===
    suspicious_values = rules.get("suspicious_values", [])
    if suspicious_values:
        try:
            v = float(value)
            for sv in suspicious_values:
                if abs(v - sv) < 1e-9:
                    # Check if quote contains A/g pattern (current density indicator)
                    if re.search(r"\d+\s*A\s*g|at\s+\d+\s*A", quote, re.IGNORECASE):
                        violations.append({
                            "rule": "SUSPICIOUS_VALUE",
                            "detail": f"value={v} is typical current density, not capacity"
                        })
                        m_copy = copy.deepcopy(m)
                        m_copy["validator"] = {
                            "decision": "DROP",
                            "violations": violations,
                        }
                        return ("DROP", m_copy)
        except (ValueError, TypeError):
            pass
    
    # === 3. Reference Brackets Check [9] ===
    if in_reference_brackets(quote, value):
        violations.append({
            "rule": "REFERENCE_BRACKET",
            "detail": f"value={value} in [n] reference pattern"
        })
        m_copy = copy.deepcopy(m)
        m_copy["validator"] = {
            "decision": "DROP",
            "violations": violations,
        }
        return ("DROP", m_copy)
    
    # === 4. Forbidden Context Check ===
    forbidden_patterns = rules.get("forbidden_patterns", [])
    for fp in forbidden_patterns:
        if binds_to_forbidden(quote, value, fp):
            violations.append({
                "rule": "FORBIDDEN_CONTEXT",
                "detail": f"value near forbidden pattern: {fp}"
            })
    
    # If any forbidden context hits, DROP (Round 11: 기준 강화 2→1)
    if len([v for v in violations if v["rule"] == "FORBIDDEN_CONTEXT"]) >= 1:
        m_copy = copy.deepcopy(m)
        m_copy["validator"] = {
            "decision": "DROP",
            "violations": violations,
        }
        return ("DROP", m_copy)
    
    # === 5. Unit Binding Check ===
    required_unit = rules.get("required_unit_regex")
    if required_unit:
        if not binds_to_unit(quote, value, required_unit):
            violations.append({
                "rule": "UNIT_BINDING_MISSING",
                "detail": f"value={value} not bound to unit {required_unit}"
            })
            
            # Check for remap rules
            remap_rules = rules.get("remap_if", [])
            for rr in remap_rules:
                cond = rr.get("condition", {})
                check_quote = cond.get("quote_contains", "")
                check_lacks = cond.get("quote_lacks", "")  # Round 7: 없어야 할 패턴
                check_range = cond.get("value_range", (float("-inf"), float("inf")))
                
                try:
                    v = float(value)
                    quote_contains_match = check_quote.lower() in quote.lower() if check_quote else True
                    quote_lacks_match = check_lacks.lower() not in quote.lower() if check_lacks else True
                    range_match = check_range[0] <= v <= check_range[1]
                    
                    if quote_contains_match and quote_lacks_match and range_match:
                        # === Round 11: unit_block check before remap ===
                        target_rules = METRIC_RULES.get(rr["remap_to"], {})
                        unit_block = target_rules.get("unit_block", [])
                        current_unit = (m.get("unit") or "").lower()
                        
                        # Check if current unit is blocked for target metric
                        unit_blocked = any(ub.lower() in current_unit for ub in unit_block)
                        if unit_blocked:
                            # Don't remap - unit is incompatible with target metric
                            violations.append({
                                "rule": "UNIT_BLOCK",
                                "detail": f"unit={current_unit} blocked for {rr['remap_to']}"
                            })
                            # Continue to next remap rule or DROP
                            continue
                        
                        # REMAP
                        m_copy = copy.deepcopy(m)
                        m_copy["metric"] = rr["remap_to"]
                        m_copy["validator"] = {
                            "decision": "REMAP",
                            "original_metric": metric,
                            "remap_to": rr["remap_to"],
                            "violations": violations,
                        }
                        return ("REMAP", m_copy)
                except (ValueError, TypeError):
                    pass
            
            # No remap applicable → DROP
            m_copy = copy.deepcopy(m)
            m_copy["validator"] = {
                "decision": "DROP",
                "violations": violations,
            }
            return ("DROP", m_copy)
    
    # === 6. Value equals condition check (contamination) ===
    if metric == "specific_capacity_mAh_g":
        if value_equals_condition(m, "specific_current_A_g"):
            violations.append({
                "rule": "VALUE_EQUALS_CONDITION",
                "detail": "value equals specific_current_A_g (likely contamination)"
            })
            m_copy = copy.deepcopy(m)
            m_copy["validator"] = {
                "decision": "DROP",
                "violations": violations,
            }
            return ("DROP", m_copy)
    
    # === 7. DFT Energy Sign Check (Round 6) ===
    if rules.get("sign_check"):
        is_valid, corrected = check_dft_sign(quote, value, metric)
        if not is_valid and corrected is not None:
            # Auto-fix: prepend minus sign
            m_copy = copy.deepcopy(m)
            m_copy["value"] = corrected
            violations.append({
                "rule": "DFT_SIGN_FIX",
                "detail": f"Corrected sign: {value} → {corrected}"
            })
            m_copy["validator"] = {
                "decision": "FIX",
                "original_value": value,
                "corrected_value": corrected,
                "violations": violations,
            }
            return ("FIX", m_copy)
    
    # === 8. Unit-Metric Compatibility Check (Round 6) ===
    unit = m.get("unit", "")
    compat_decision, remap_metric = check_unit_metric_compat(metric, unit, quote)
    if compat_decision == "REMAP" and remap_metric:
        m_copy = copy.deepcopy(m)
        m_copy["metric"] = remap_metric
        violations.append({
            "rule": "UNIT_METRIC_INCOMPAT",
            "detail": f"Unit '{unit}' incompatible with '{metric}' → REMAP to '{remap_metric}'"
        })
        m_copy["validator"] = {
            "decision": "REMAP",
            "original_metric": metric,
            "remap_to": remap_metric,
            "violations": violations,
        }
        return ("REMAP", m_copy)
    
    # === PASSED ===
    if violations:
        m_copy = copy.deepcopy(m)
        m_copy["validator"] = {
            "decision": "KEEP",
            "violations": violations,
            "qc_flags": ["SOFT_VIOLATION"],
        }
        return ("KEEP", m_copy)
    
    return ("KEEP", m)


# ============================================================================
# BATCH VALIDATION
# ============================================================================

def validate_measurements_batch(measurements: List[Dict]) -> Tuple[List[Dict], Dict]:
    """
    Validate a batch of measurements.
    
    Returns:
        (validated_measurements, qc_summary)
    """
    kept = []
    dropped = []
    remapped = []
    fixed = []  # Round 6: DFT sign fixes
    
    for m in measurements:
        decision, validated = validate_measurement(m)
        
        if decision == "KEEP":
            kept.append(validated)
        elif decision == "REMAP":
            remapped.append(validated)
            kept.append(validated)  # Remapped records are kept
        elif decision == "FIX":
            fixed.append(validated)
            kept.append(validated)  # Fixed records are kept
        else:  # DROP
            dropped.append(validated)
    
    # === ROUND 10: Deduplication ===
    def deduplicate_measurements(measurements):
        """Remove duplicate measurements based on chunk+metric+value+material."""
        seen = set()
        unique = []
        dup_count = 0
        for m in measurements:
            key = (
                m.get("metric"),
                str(m.get("value")),
                m.get("chunk_id") or m.get("evidence", {}).get("anchor_id", ""),
                (m.get("conditions") or {}).get("material_id", ""),
            )
            if key not in seen:
                seen.add(key)
                unique.append(m)
            else:
                dup_count += 1
        if dup_count > 0:
            logger.info(f"  Dedup: removed {dup_count} duplicates")
        return unique
    
    kept = deduplicate_measurements(kept)
    
    # Summary
    qc_summary = {
        "input_count": len(measurements),
        "kept_count": len(kept),
        "dropped_count": len(dropped),
        "remapped_count": len(remapped),
        "drop_rate": len(dropped) / len(measurements) * 100 if measurements else 0,
    }
    
    # Count by rule
    rule_counts = {}
    for d in dropped:
        for v in d.get("validator", {}).get("violations", []):
            rule = v.get("rule", "UNKNOWN")
            rule_counts[rule] = rule_counts.get(rule, 0) + 1
    
    qc_summary["drop_by_rule"] = rule_counts
    
    logger.info(f"  QC Validator: {len(kept)} kept, {len(dropped)} dropped, {len(remapped)} remapped")
    
    return kept, qc_summary
