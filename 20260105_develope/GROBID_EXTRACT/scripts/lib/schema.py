# scripts/lib/schema.py
"""
Enterprise-grade JSON Schema definitions with Pydantic validation.
Ensures strict output format and provides automatic validation.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Literal, Optional, Union
import json


# ============================================================================
# Core Evidence Structure
# ============================================================================
@dataclass
class Evidence:
    """Evidence structure for traceability."""
    doc: Literal["MAIN", "SUPP"]
    section_path: str
    page_range: Optional[List[int]] = None
    figure_id: Optional[str] = None
    table_id: Optional[str] = None
    quote: str = ""  # Max 30 words
    data_quality_flags: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ============================================================================
# Measurement Schema
# ============================================================================
@dataclass
class Measurement:
    """Single measurement with full metadata."""
    paper_id: str
    case_id: str
    metric: str
    value: Optional[Union[float, int, str]] = None
    unit: Optional[str] = None
    confidence: float = 0.0
    evidence: Optional[Evidence] = None
    
    # Tags for scope disambiguation
    tags: Dict[str, str] = field(default_factory=dict)
    # Conditions (current density, temperature, etc.)
    conditions: Dict[str, Any] = field(default_factory=dict)
    # Derivation info
    derived: bool = False
    derivation_formula: Optional[str] = None
    metric_source: Literal["TEXT_EXPLICIT", "CALCULATED", "DIGITIZED", "UNCLEAR"] = "UNCLEAR"
    # Extractor metadata
    extractor_id: str = ""
    extractor_version: str = "v2"
    
    def to_dict(self) -> dict:
        d = asdict(self)
        if self.evidence:
            d["evidence"] = self.evidence.to_dict()
        return {k: v for k, v in d.items() if v is not None and v != {} and v != []}


# ============================================================================
# Case Schema
# ============================================================================
@dataclass
class Case:
    """Experimental case definition."""
    paper_id: str
    case_id: str
    coating_label: Optional[str] = None  # e.g., "Zn@GO-5um"
    material_raw: Optional[str] = None
    material_class: Optional[str] = None  # polymer, MOF, carbon, inorganic, composite
    protective_layer_thickness_um: Optional[float] = None
    electrolyte_raw: Optional[str] = None
    electrolyte_concentration_M: Optional[float] = None
    cell_type: Literal["SYMMETRIC", "FULL_CELL", "HALF_CELL", "OTHER", "UNCLEAR"] = "UNCLEAR"
    areal_capacity_mAhcm2: Optional[float] = None
    areal_current_density_mAcm2: Optional[float] = None
    temperature_C: Optional[float] = None
    evidence: List[Evidence] = field(default_factory=list)
    notes: str = ""
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["evidence"] = [e.to_dict() if isinstance(e, Evidence) else e for e in self.evidence]
        return {k: v for k, v in d.items() if v is not None and v != "" and v != []}


# ============================================================================
# Digitize Task Schema
# ============================================================================
@dataclass
class DigitizeTask:
    """Task for figure digitization."""
    paper_id: str
    case_id: str
    task_type: Literal["DIGITIZE_EIS", "DIGITIZE_CYCLING", "DIGITIZE_TAFEL", "DIGITIZE_OVERPOTENTIAL"]
    figure_id: str
    reason: str
    needs: List[str] = field(default_factory=list)  # e.g., ["Rs", "Rct"]
    axis_hints: Dict[str, str] = field(default_factory=dict)
    status: str = "PENDING"
    
    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v != {} and v != []}


# ============================================================================
# QC Flag Schema
# ============================================================================
@dataclass
class QCFlag:
    """Quality control flag."""
    flag_type: Literal["RANGE_OUTLIER", "MISSING_SCOPE", "MISSING_TYPE", "CONFLICT", "UNIT_SUSPECT", "NEED_REVIEW"]
    case_id: Optional[str] = None
    metric: Optional[str] = None
    value: Optional[Any] = None
    expected_range: Optional[List[float]] = None
    hint: str = ""
    severity: Literal["WARNING", "ERROR", "INFO"] = "WARNING"
    
    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ============================================================================
# Cross-Reference Link Schema (for fragmented sentence linking)
# ============================================================================
@dataclass
class CrossRef:
    """Cross-reference link between chunks."""
    source_chunk_id: str
    target_chunk_id: str
    link_type: Literal["SAME_CASE", "SAME_METRIC", "FIGURE_REF", "TABLE_REF", "RESULT_METHOD_LINK"]
    confidence: float = 0.0
    evidence: str = ""  # Why these are linked
    
    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================
# Validation Helpers
# ============================================================================
# VALID_METRICS is now dynamically populated from METRIC_REGISTRY
# to ensure single source of truth for allowed metrics
def _get_valid_metrics():
    """Dynamically get all valid metrics from METRIC_REGISTRY."""
    try:
        from scripts.lib.metric_registry import METRIC_REGISTRY
        return set(METRIC_REGISTRY.keys())
    except ImportError:
        # Fallback to legacy set if import fails
        return _LEGACY_VALID_METRICS

# Legacy fallback (maintained for compatibility)
_LEGACY_VALID_METRICS = {
    # Input metrics
    "protective_layer_material",
    "protective_layer_thickness_um",
    "ion_conductivity_mS_cm",
    "contact_angle_deg",
    "contact_angle_delta_deg",
    "zn_adsorption_energy_eV",
    "areal_capacity_mAhcm2",
    "areal_current_density_mAcm2",
    # Output metrics
    "galvanostatic_cycling_performance_h",
    "galvanostatic_cycling_cycles",
    "coulombic_efficiency_pct",
    "capacity_retention_pct",
    "corrosion_current_density_uAcm2",
    "corrosion_potential_V",
    "overpotential_mV",
    "eis_Rs_Ohm",
    "eis_Rct_Ohm",
    "eis_Rsei_Ohm",
    "eis_fit_model",
    "specific_capacity_mAh_g",
    "energy_density_Wh_kg",
    "power_density_W_kg",
    "graphene_sheet_lateral_size_nm",
    "graphene_sheet_thickness_nm",
    "graphene_prep_electrolyte",
    "graphene_prep_exfoliation_potential_V_vs_AgAgCl",
    "graphene_prep_exfoliation_time_s",
    "cathode_graphene_deposition_potential_range_V_vs_AgAgCl",
    "cathode_graphene_deposition_scan_rate_mV_s",
    "cathode_graphene_deposition_cycles",
    "cathode_pani_deposition_current_density_mAcm2",
    "cathode_pani_deposition_time_s",
    "cathode_pani_deposition_electrolyte",
    "cathode_pani_mass_loading_mg_cm2",
    "protective_layer_deposition_potential_V_vs_AgAgCl",
    "protective_layer_deposition_time_min",
    "zn_substrate_thickness_mm",
}

# Use dynamic loading from registry
VALID_METRICS = _get_valid_metrics()

VALID_SCOPES = {
    "ionic_conductivity_scope": ["COATING", "ELECTROLYTE", "UNCLEAR"],
    "contact_angle_baseline": ["BARE_ZN", "COATED", "BOTH", "UNCLEAR"],
    "overpotential_type": ["NUCLEATION", "STEADY", "DEPOSITION", "HYSTERESIS", "SERIES", "UNCLEAR"],
    "eis_metric_type": ["Rs", "Rct", "Rsei", "R0", "Z_total", "FIT", "UNCLEAR"],
    "zn_adsorption_source": ["DFT", "EXPERIMENT", "UNCLEAR"],
}


def validate_measurement(m: Dict[str, Any], skip_metadata_check: bool = False) -> List[str]:
    """Validate a measurement dict. Returns list of errors."""
    errors = []
    
    # Skip paper_id and case_id check if they will be added later
    if not skip_metadata_check:
        if not m.get("paper_id"):
            errors.append("Missing paper_id")
        if not m.get("case_id"):
            errors.append("Missing case_id")
    
    if not m.get("metric"):
        errors.append("Missing metric")
    elif m["metric"] not in VALID_METRICS:
        errors.append(f"Unknown metric: {m['metric']}")
    
    # Check scope tags
    tags = m.get("tags", {})
    for scope_name, valid_values in VALID_SCOPES.items():
        if scope_name in tags:
            if tags[scope_name] not in valid_values:
                errors.append(f"Invalid {scope_name}: {tags[scope_name]}")
    
    # Check evidence
    ev = m.get("evidence")
    if ev:
        if ev.get("doc") not in ["MAIN", "SUPP"]:
            errors.append(f"Invalid evidence.doc: {ev.get('doc')}")
        if not ev.get("section_path") and not ev.get("quote"):
            errors.append("Evidence needs section_path or quote")
    
    return errors


def validate_case(c: Dict[str, Any]) -> List[str]:
    """Validate a case dict. Returns list of errors."""
    errors = []
    
    if not c.get("paper_id"):
        errors.append("Missing paper_id")
    if not c.get("case_id"):
        errors.append("Missing case_id")
    
    cell_type = c.get("cell_type")
    if cell_type and cell_type not in ["SYMMETRIC", "FULL_CELL", "HALF_CELL", "OTHER", "UNCLEAR"]:
        errors.append(f"Invalid cell_type: {cell_type}")
    
    return errors


def parse_llm_output(raw: str, expected_keys: List[str] = None) -> Dict[str, Any]:
    """
    Parse LLM output with multiple fallback strategies.
    Handles: markdown fences, thinking/output split, partial JSON.
    """
    import re
    
    s = raw.strip()
    
    # 1. Remove markdown code fences
    if "```" in s:
        # Extract content between ``` markers
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
        if match:
            s = match.group(1).strip()
        else:
            s = s.replace("```json", "").replace("```", "").strip()
    
    # 2. Try direct parse
    try:
        obj = json.loads(s)
        # Handle thinking/output wrapper
        if isinstance(obj, dict) and "output" in obj and "thinking" in obj:
            return obj["output"]
        return obj
    except:
        pass
    
    # 3. Find first { to last }
    i = s.find("{")
    j = s.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            obj = json.loads(s[i:j+1])
            if isinstance(obj, dict) and "output" in obj and "thinking" in obj:
                return obj["output"]
            return obj
        except:
            pass
    
    # 4. Try to find array
    i = s.find("[")
    j = s.rfind("]")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(s[i:j+1])
        except:
            pass
    
    raise ValueError(f"Failed to parse JSON: {s[:200]}...")


# ============================================================================
# PATCH 2: Strict Validator for Stage4 Output
# ============================================================================

from scripts.lib.contracts import (
    ALLOWED_EVIDENCE_DOC, 
    ALLOWED_VALUE_STATUS,
    CURRENT_TYPE_SLOTS
)

def ensure_measurement_defaults(m: dict) -> dict:
    """Ensure conditions/tags/evidence are dicts, not null."""
    if not isinstance(m, dict):
        return {}
    if not isinstance(m.get("conditions"), dict):
        m["conditions"] = {}
    if not isinstance(m.get("tags"), dict):
        m["tags"] = {}
    if not isinstance(m.get("evidence"), dict):
        m["evidence"] = {}
    return m


def validate_measurement_strict(m: Dict[str, Any]) -> List[str]:
    """
    Stage4 output validation with strict enforcement.
    Returns list of error strings. Empty = valid.
    
    Checks:
    - Required keys existence
    - evidence.doc = MAIN/SUPP
    - evidence.section_path required
    - evidence anchor (quote/chunk_id/figure_id/table_id)
    - Quote quality (>= 20 chars, <= 25 words, not figure label)
    - Null value requires value_status tag
    - Current type slot mutual exclusivity
    """
    errors = []
    m = ensure_measurement_defaults(m)
    
    # [1] Required top-level keys
    for k in ["metric", "value", "unit", "confidence", "conditions", "tags", "evidence"]:
        if k not in m:
            errors.append(f"Missing key: {k}")
    
    # [2] Confidence validation
    conf = m.get("confidence")
    if conf is not None:
        if not isinstance(conf, (int, float)):
            errors.append("Invalid confidence: must be number")
        elif not (0 <= float(conf) <= 1):
            errors.append("Invalid confidence: must be in [0,1]")
    
    # [3] Evidence hard requirements
    ev = m.get("evidence") if isinstance(m.get("evidence"), dict) else {}
    doc = ev.get("doc")
    if doc not in ALLOWED_EVIDENCE_DOC:
        errors.append(f"Invalid evidence.doc: {doc}")
    
    if not ev.get("section_path"):
        errors.append("Evidence needs section_path")
    
    # [4] Anchor check (at least one required)
    quote = ev.get("quote")
    chunk_id = ev.get("chunk_id")
    fig = ev.get("figure_id")
    tbl = ev.get("table_id")
    has_anchor = bool(quote) or bool(chunk_id) or bool(fig) or bool(tbl)
    if not has_anchor:
        errors.append("Evidence needs at least one of quote/chunk_id/figure_id/table_id")
    
    # [5] Quote quality checks
    if isinstance(quote, str):
        q_stripped = quote.strip()
        if len(q_stripped) < 20:
            errors.append(f"Evidence.quote too short ({len(q_stripped)} < 20 chars)")
        if len(quote.split()) > 25:
            errors.append("Evidence.quote too long (>25 words)")
        # Block figure labels like "4a.", "Fig. 3", etc.
        q_lower = q_stripped.lower()
        if q_lower in ["4a", "4a.", "fig", "figure"] or q_lower.startswith(("fig.", "figure")):
            errors.append("Evidence.quote is figure label, not sentence")
    
    # [6] Null value rule enforcement
    if m.get("value") is None:
        vs = (m.get("tags") or {}).get("value_status")
        if vs not in ALLOWED_VALUE_STATUS:
            errors.append("value is null => tags.value_status must be FIGURE_DIGITIZE_REQUIRED or NOT_FOUND")
    
    # [7] Current type slot mutual exclusivity
    cond = m.get("conditions") or {}
    found_current_slots = [k for k in CURRENT_TYPE_SLOTS if cond.get(k) is not None]
    if len(found_current_slots) > 1:
        errors.append(f"Multiple current types not allowed: {found_current_slots}")
    
    return errors


# ============================================================================
# 08_설계: Quote Override Detection
# ============================================================================
import re
from scripts.lib.contracts import (
    BAD_QUOTE_PATTERNS, 
    METRIC_REGISTRY,
    KEY_ALIAS,
    ALLOWED_VALUE_STATUS_V11,
    CURRENT_TYPE_SLOTS,
)

def should_override_quote(quote: str, value: Any) -> bool:
    """
    Determine if a quote is bad and should be overridden during hydration.
    
    Bad quote patterns (08_설계 Section 4.2):
    - "4a.", "7 Fig.", "Value extracted..." (no value evidence)
    - Too short (< 20 chars)
    - Numeric value not present in quote
    
    Returns True if quote should be replaced.
    """
    if quote is None:
        return True
    
    q = (quote or "").strip()
    q_lower = q.lower()
    
    # Too short
    if len(q) < 20:
        return True
    
    # Bad pattern matches
    for pat in BAD_QUOTE_PATTERNS:
        if re.search(pat, q_lower, re.IGNORECASE):
            return True
    
    # Numeric value not in quote
    if isinstance(value, (int, float)):
        val_str = str(value)
        # For floats, try both exact and truncated forms
        if val_str not in q:
            # Try integer version for floats that are whole numbers
            if isinstance(value, float) and value == int(value):
                if str(int(value)) not in q:
                    return True
            else:
                return True
    
    return False


# ============================================================================
# 08_설계: METRIC_REGISTRY-based Validation
# ============================================================================

def validate_with_registry(m: Dict[str, Any]) -> List[str]:
    """
    Validate measurement against METRIC_REGISTRY spec.
    Checks required conditions and tags for each metric.
    
    Returns list of error strings. Empty = valid.
    """
    errors = []
    m = ensure_measurement_defaults(m)
    
    metric = m.get("metric")
    if not metric:
        return errors  # Can't validate without metric name
    
    spec = METRIC_REGISTRY.get(metric)
    if not spec:
        return errors  # No spec, skip registry validation
    
    conditions = m.get("conditions") or {}
    tags = m.get("tags") or {}
    
    # Check required conditions
    for cond_key in spec.get("required_conditions", {}):
        if cond_key not in conditions:
            errors.append(f"conditions_missing:{cond_key}")
    
    # Check required tags
    for tag_key, allowed_values in spec.get("required_tags", {}).items():
        if tag_key not in tags:
            errors.append(f"tags_missing:{tag_key}")
        elif tags[tag_key] not in allowed_values and tags[tag_key] is not None:
            errors.append(f"tags_invalid:{tag_key}={tags[tag_key]}")
    
    # Check unit
    allowed_units = spec.get("allowed_units")
    if allowed_units:
        unit = m.get("unit")
        if unit and unit not in allowed_units:
            errors.append(f"unit_invalid:{unit} (allowed: {allowed_units})")
    
    # Check value range
    value_range = spec.get("value_range")
    if value_range and isinstance(m.get("value"), (int, float)):
        v = m["value"]
        if v < value_range[0] or v > value_range[1]:
            errors.append(f"value_out_of_range:{v} (expected {value_range})")
    
    return errors


def fill_required_keys_with_null(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fill missing required conditions/tags with null to ensure consistent schema.
    Uses METRIC_REGISTRY to determine required keys.
    """
    m = ensure_measurement_defaults(m)
    metric = m.get("metric")
    if not metric:
        return m
    
    spec = METRIC_REGISTRY.get(metric)
    if not spec:
        return m
    
    conditions = m.get("conditions") or {}
    tags = m.get("tags") or {}
    
    # Fill missing conditions with null
    for cond_key in spec.get("required_conditions", {}):
        if cond_key not in conditions:
            conditions[cond_key] = None
    
    # Fill missing tags with null
    for tag_key in spec.get("required_tags", {}):
        if tag_key not in tags:
            tags[tag_key] = None
    
    m["conditions"] = conditions
    m["tags"] = tags
    return m


# ============================================================================
# 10_설계: Contract v1.1 - Condition Key Canonicalization
# ============================================================================

def canonicalize_conditions(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Canonicalize condition keys using KEY_ALIAS mappings.
    
    Maps various key aliases to canonical form:
    - current_density_mA_cm2 -> areal_current_density_mA_cm2
    - areal_capacity_mAhcm2 -> areal_capacity_mAh_cm2
    - temperature, temp -> temperature_C
    
    Also enforces mutual exclusivity for CURRENT_TYPE_SLOTS:
    - Only one of (areal_current_density, gravimetric_current, c_rate) allowed
    - If multiple found, keeps the first non-null, drops others
    
    Returns modified measurement dict.
    """
    if not isinstance(m, dict):
        return m
    
    m = dict(m)  # Don't mutate original
    cond = m.get("conditions") or {}
    if not isinstance(cond, dict):
        cond = {}
    
    cond = dict(cond)  # Don't mutate original
    
    # Step 1: Remap alias keys to canonical keys
    new_cond = {}
    for k, v in cond.items():
        canonical_key = KEY_ALIAS.get(k, k)  # Use canonical if aliased, else keep original
        if canonical_key in new_cond:
            # If canonical already exists, prefer non-null value
            if new_cond[canonical_key] is None and v is not None:
                new_cond[canonical_key] = v
        else:
            new_cond[canonical_key] = v
    
    # Step 2: Enforce mutual exclusivity for current-type slots
    found_current_slots = [k for k in CURRENT_TYPE_SLOTS if new_cond.get(k) is not None]
    if len(found_current_slots) > 1:
        # Keep first non-null, set others to null (with warning note in tags)
        keep_slot = found_current_slots[0]
        for slot in found_current_slots[1:]:
            new_cond[slot] = None
        # Optionally track that we dropped overlapping slots
        tags = m.get("tags") or {}
        if isinstance(tags, dict):
            tags = dict(tags)
            tags["_dropped_current_slots"] = found_current_slots[1:]
            m["tags"] = tags
    
    m["conditions"] = new_cond
    return m


def enforce_null_value_rules(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enforce Contract v1.1 null value rules.
    
    When value is null:
    1. tags.value_status MUST be set (FIGURE_DIGITIZE_REQUIRED or NOT_FOUND)
    2. evidence MUST have figure_id, table_id, or chunk_id
    
    If value is null but value_status is missing, infer based on evidence:
    - Has figure_id -> FIGURE_DIGITIZE_REQUIRED
    - Has table_id/chunk_id -> NOT_FOUND (table text not extractable)
    - Otherwise -> NOT_FOUND
    
    Returns modified measurement dict.
    """
    if not isinstance(m, dict):
        return m
    
    m = dict(m)  # Don't mutate original
    
    val = m.get("value")
    tags = m.get("tags") or {}
    if isinstance(tags, dict):
        tags = dict(tags)
    else:
        tags = {}
    
    ev = m.get("evidence") or {}
    if not isinstance(ev, dict):
        ev = {}
    
    if val is None:
        # Value is null - enforce value_status
        if "value_status" not in tags or tags["value_status"] is None:
            # Infer value_status based on evidence
            if ev.get("figure_id"):
                tags["value_status"] = "FIGURE_DIGITIZE_REQUIRED"
            else:
                tags["value_status"] = "NOT_FOUND"
        
        # Validate value_status is in allowed set
        if tags.get("value_status") not in ALLOWED_VALUE_STATUS_V11:
            # If not in allowed set, default to NOT_FOUND
            tags["value_status"] = "NOT_FOUND"
    else:
        # Value is not null - remove legacy value_status if present and set to null
        # But preserve valid quality flags like OK_UNIT_UNCLEAR
        current_status = tags.get("value_status")
        if current_status and current_status not in {"OK_UNIT_UNCLEAR", "OK_CONTEXT_UNCLEAR"}:
            # Clear invalid status for non-null values
            tags.pop("value_status", None)
    
    # Remove legacy needs_digitize flag (replaced by value_status)
    tags.pop("needs_digitize", None)
    tags.pop("digitize", None)
    
    m["tags"] = tags
    return m


# ============================================================================
# 08_설계 Section 5: Type Guards for Pipeline Stability
# ============================================================================

def assert_all_dict(measurements: List[Any], stage: str) -> None:
    """
    Assert that all items in measurements list are dicts.
    Raises TypeError with detailed info if not.
    
    Used to catch tuple corruption before organize step.
    """
    for i, m in enumerate(measurements):
        if not isinstance(m, dict):
            raise TypeError(
                f"[{stage}] measurement[{i}] is {type(m).__name__}: {repr(m)[:200]}"
            )


def coerce_to_measurement_dict(x: Any) -> Optional[Dict[str, Any]]:
    """
    Coerce various types to measurement dict.
    
    Handles:
    - dict -> return as-is
    - (dict, score) tuple -> extract first element
    - [dict, ...] list -> extract first element
    - None/other -> return None
    """
    if isinstance(x, dict):
        return x
    if isinstance(x, (tuple, list)) and x and isinstance(x[0], dict):
        return x[0]
    return None


def safe_filter_measurements(measurements: List[Any], stage: str = "unknown") -> List[Dict[str, Any]]:
    """
    Safely filter measurements, coercing tuples and logging drops.
    
    Returns only valid dict measurements, logging any that were dropped or coerced.
    """
    result = []
    dropped = 0
    coerced = 0
    
    for x in measurements:
        if x is None:
            dropped += 1
            continue
        
        if isinstance(x, dict):
            result.append(x)
        else:
            m = coerce_to_measurement_dict(x)
            if m is not None:
                result.append(m)
                coerced += 1
            else:
                dropped += 1
    
    if dropped > 0 or coerced > 0:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"[{stage}] safe_filter: dropped={dropped}, coerced={coerced}")
    
    return result


# ============================================================================
# 09_설계: Stage4 Contract Enforcer Functions
# ============================================================================

ALLOWED_DOC = {"MAIN", "SUPP"}

def normalize_stage4_output(obj: Any) -> Dict[str, Any]:
    """
    Normalize any LLM output to contract form: {"measurements": [...]}
    
    Handles:
    - {"measurements": [...]} -> as-is
    - {"data", "items", "results": [...]} -> wrap as measurements
    - [...] list -> wrap as measurements
    - single dict with metric/value -> wrap as single measurement
    - None/other -> empty measurements
    """
    if obj is None:
        return {"measurements": []}
    
    if isinstance(obj, list):
        return {"measurements": obj}
    
    if isinstance(obj, dict):
        if "measurements" in obj and isinstance(obj["measurements"], list):
            return obj
        # Common alternative keys
        for alt in ("data", "items", "results"):
            if alt in obj and isinstance(obj[alt], list):
                return {"measurements": obj[alt]}
        # Single measurement dict
        if "metric" in obj and "value" in obj:
            return {"measurements": [obj]}
        # Empty
        return {"measurements": []}
    
    return {"measurements": []}


def normalize_measurement(m: Any, default_doc: str = "MAIN") -> Dict[str, Any]:
    """
    Normalize a single measurement to have all required keys with defaults.
    Handles tuples, ensures evidence.doc is never None.
    """
    if not isinstance(m, dict):
        # Handle tuple/list
        if isinstance(m, (tuple, list)) and m and isinstance(m[0], dict):
            m = m[0]
        else:
            return {}
    
    m = dict(m)  # Don't mutate original
    
    # Top-level defaults
    m.setdefault("metric", None)
    m.setdefault("value", None)
    m.setdefault("unit", None)
    m.setdefault("confidence", None)
    m.setdefault("conditions", {})
    m.setdefault("tags", {})
    m.setdefault("evidence", {})
    
    # Evidence defaults
    ev = m["evidence"] if isinstance(m["evidence"], dict) else {}
    ev = dict(ev)
    
    # doc cannot be None
    if ev.get("doc") not in ALLOWED_DOC:
        ev["doc"] = default_doc
    
    ev.setdefault("section_path", None)
    ev.setdefault("quote", None)
    ev.setdefault("chunk_id", None)
    ev.setdefault("anchor_id", None)
    ev.setdefault("figure_id", None)
    ev.setdefault("table_id", None)
    
    m["evidence"] = ev
    return m


def apply_metric_contract(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply METRIC_REGISTRY contract to fill required conditions/tags.
    Uses fill_required_keys_with_null internally.
    """
    return fill_required_keys_with_null(m)


def is_bad_quote(q: Any) -> bool:
    """
    Check if quote is bad quality (too short, placeholder, or pattern match).
    Returns True if quote should be rejected/overridden.
    """
    if not q or not isinstance(q, str):
        return True
    
    qs = q.strip().lower()
    if len(qs) < 20:
        return True
    
    for pat in BAD_QUOTE_PATTERNS:
        if re.search(pat, qs, re.IGNORECASE):
            return True
    
    return False


def validate_measurement_contract(m: Dict[str, Any]) -> List[str]:
    """
    Validate a single measurement against 09_설계 contract.
    Returns list of path-based error strings.
    """
    errs = []
    if not isinstance(m, dict):
        return ["measurement_not_dict"]
    
    # Evidence critical checks
    ev = m.get("evidence") or {}
    if ev.get("doc") not in ALLOWED_DOC:
        errs.append("evidence.doc_invalid_or_missing")
    if not ev.get("section_path"):
        errs.append("evidence.section_path_missing")
    if is_bad_quote(ev.get("quote")):
        errs.append("evidence.quote_missing_or_low_quality")
    
    # Registry-based checks
    metric = m.get("metric")
    spec = METRIC_REGISTRY.get(metric) or {}
    req_c = spec.get("required_conditions", {})
    req_t = spec.get("required_tags", {})
    cond = m.get("conditions") or {}
    tags = m.get("tags") or {}
    
    for k in req_c:
        if k not in cond:
            errs.append(f"conditions.missing:{k}")
    for k in req_t:
        if k not in tags:
            errs.append(f"tags.missing:{k}")
    
    return errs


def validate_stage4_output(obj: Dict[str, Any]) -> List[str]:
    """
    Validate full Stage4 output against contract.
    Returns list of path-based error strings. Empty = valid.
    """
    errs = []
    if not isinstance(obj, dict):
        return ["output_not_dict"]
    
    ms = obj.get("measurements")
    if not isinstance(ms, list):
        return ["measurements_missing_or_not_list"]
    
    for i, m in enumerate(ms):
        me = validate_measurement_contract(m)
        for e in me:
            errs.append(f"measurements[{i}].{e}")
    
    return errs


def build_contract_text(task_name: str = "Stage4") -> str:
    """
    Build the JSON contract text to append to LLM prompts.
    """
    return f"""
You must output ONLY valid JSON (no markdown).

Return exactly:
{{
  "measurements": [
    {{
      "metric": "...",
      "value": number|string|[number,...]|null,
      "unit": string|null,
      "confidence": number,

      "conditions": {{}},
      "tags": {{}},

      "evidence": {{
        "doc": "MAIN"|"SUPP",
        "section_path": string,
        "chunk_id": string|null,
        "anchor_id": string|null,
        "figure_id": string|null,
        "table_id": string|null,
        "quote": string
      }}
    }}
  ]
}}

CRITICAL RULES:
1) evidence.doc cannot be null - must be "MAIN" or "SUPP".
2) evidence.section_path cannot be null.
3) evidence.quote must be a real sentence/snippet (not '4a.' or 'Value extracted...').
4) If the numeric value is only in a plot and cannot be read from text/caption/table:
   set value=null and set tags.needs_digitize=true and set evidence.figure_id.
5) conditions/tags MUST include all required keys given in the request, even if null.
""".strip()


def build_correction_hint(errors: List[str], required_keys: Dict[str, List[str]]) -> str:
    """
    Build correction hint from validation errors.
    """
    error_lines = "\n".join(f"- {e}" for e in errors[:15])  # Limit to 15 errors
    cond_keys = required_keys.get("conditions", [])
    tag_keys = required_keys.get("tags", [])
    
    return f"""
Your previous output violated the contract.

Errors (fix ALL):
{error_lines}

You MUST ensure:
- measurements is a list
- evidence.doc, evidence.section_path, evidence.quote exist for every measurement
- required condition keys: {cond_keys}
- required tag keys: {tag_keys}

Return ONLY corrected JSON.
""".strip()


# Task-specific required keys mapping (09_설계 Section 5)
TASK_REQUIRED_KEYS = {
    "EXTRACT_OVERPOTENTIAL": {
        "conditions": ["current_density_mA_cm2", "material"],
        "tags": ["overpotential_type"],
    },
    "EXTRACT_EIS": {
        "conditions": ["frequency_range_Hz", "ac_amplitude_V", "applied_potential_V"],
        "tags": ["eis_metric_type", "before_after"],
    },
    "EXTRACT_CYCLING": {
        "conditions": ["current_density_value", "current_density_unit", "temperature_C"],
        "tags": [],
    },
    "EXTRACT_INPUT": {
        "conditions": [],
        "tags": [],
    },
    "EXTRACT_CORROSION": {
        "conditions": ["reference_electrode"],
        "tags": ["sample_type"],
    },
}


def get_task_required_keys(task_name: str) -> Dict[str, List[str]]:
    """Get required keys for a specific extraction task."""
    return TASK_REQUIRED_KEYS.get(task_name, {"conditions": [], "tags": []})


# ============================================================================
# 11_설계: METRIC_REGISTRY v1.0 - Strict Validator
# ============================================================================

# Import the new registry
try:
    from scripts.lib.metric_registry import (
        METRIC_REGISTRY as REGISTRY_V1,
        CURRENT_TYPE_SLOTS as CURRENT_SLOTS_V1,
        ALLOWED_VALUE_STATUS,
        get_required_conditions,
        is_valid_metric,
        has_forbidden_pattern,
    )
    REGISTRY_AVAILABLE = True
except ImportError:
    REGISTRY_AVAILABLE = False
    REGISTRY_V1 = {}
    CURRENT_SLOTS_V1 = []


# Placeholder quote patterns that indicate no real evidence
PLACEHOLDER_QUOTE_PATTERNS = [
    r"^value extracted",
    r"^extracted from fig",
    r"^value from figure",
    r"^from table",
    r"^see fig",
    r"^data from",
    r"^refer to",
    r"^\s*fig\s*\d",
    r"^\s*table\s*\d",
]


def is_placeholder_quote(quote: Any) -> bool:
    """
    Check if quote is a placeholder rather than real evidence.
    Returns True if quote should be rejected.
    """
    if not quote or not isinstance(quote, str):
        return True
    
    q = quote.strip().lower()
    
    # Too short
    if len(q) < 15:
        return True
    
    # Matches placeholder patterns
    for pat in PLACEHOLDER_QUOTE_PATTERNS:
        if re.search(pat, q, re.IGNORECASE):
            return True
    
    return False


def validate_measurement_registry(m: Dict[str, Any], extractor_name: str = None) -> List[str]:
    """
    Validate measurement against METRIC_REGISTRY v1.0.
    
    Returns list of ERROR strings. Empty = valid.
    
    Strict ERROR rules:
    1. Metric not in registry
    2. Pipe (|) in metric name
    3. Required condition key missing (null value OK)
    4. Value=null without tags.value_status
    5. Multiple current-type slots non-null
    6. Placeholder quote
    7. Missing evidence.doc or section_path
    """
    errors = []
    
    if not REGISTRY_AVAILABLE:
        return errors  # Skip validation if registry not imported
    
    if not isinstance(m, dict):
        return ["ERROR:measurement_not_dict"]
    
    metric = m.get("metric", "")
    
    # Rule 1: Metric must be in registry
    if not is_valid_metric(metric):
        errors.append(f"ERROR:metric_unknown:{metric}")
    
    # Rule 2: No pipe in metric name
    if has_forbidden_pattern(metric):
        errors.append(f"ERROR:metric_pipe_forbidden:{metric}")
    
    # Rule 3: Required conditions must exist (null value OK)
    spec = REGISTRY_V1.get(metric)
    if spec:
        conditions = m.get("conditions") or {}
        for req_key in spec.required_conditions:
            if req_key not in conditions:
                errors.append(f"ERROR:condition_key_missing:{req_key}")
    
    # Rule 4: Null value requires value_status
    value = m.get("value")
    tags = m.get("tags") or {}
    if value is None:
        if "value_status" not in tags or tags.get("value_status") is None:
            errors.append("ERROR:null_value_without_status")
        elif tags.get("value_status") not in ALLOWED_VALUE_STATUS:
            errors.append(f"ERROR:invalid_value_status:{tags.get('value_status')}")
    
    # Rule 5: Current-type mutual exclusivity
    conditions = m.get("conditions") or {}
    non_null_current_slots = [
        slot for slot in CURRENT_SLOTS_V1 
        if conditions.get(slot) is not None
    ]
    if len(non_null_current_slots) > 1:
        errors.append(f"ERROR:current_slot_conflict:{non_null_current_slots}")
    
    # Rule 6: Placeholder quote detection
    evidence = m.get("evidence") or {}
    quote = evidence.get("quote")
    if is_placeholder_quote(quote):
        errors.append("ERROR:placeholder_quote")
    
    # Rule 7: Missing evidence.doc or section_path
    doc = evidence.get("doc")
    if doc not in {"MAIN", "SUPP"}:
        errors.append("ERROR:evidence_doc_missing")
    
    section_path = evidence.get("section_path")
    if not section_path:
        errors.append("ERROR:evidence_section_path_missing")
    
    return errors


def validate_measurements_batch(
    measurements: List[Dict[str, Any]], 
    extractor_name: str = None
) -> Dict[str, Any]:
    """
    Validate a batch of measurements.
    
    Returns:
        {
            "valid": [...],  # Measurements with no errors
            "invalid": [...],  # Measurements with errors
            "errors_by_index": {idx: [errors]},
            "summary": {"total": N, "valid": M, "invalid": K}
        }
    """
    valid = []
    invalid = []
    errors_by_index = {}
    
    for i, m in enumerate(measurements):
        errs = validate_measurement_registry(m, extractor_name)
        if errs:
            invalid.append(m)
            errors_by_index[i] = errs
        else:
            valid.append(m)
    
    return {
        "valid": valid,
        "invalid": invalid,
        "errors_by_index": errors_by_index,
        "summary": {
            "total": len(measurements),
            "valid": len(valid),
            "invalid": len(invalid),
        }
    }


def build_registry_correction_hint(errors: List[str], metric: str = None) -> str:
    """
    Build a detailed correction hint from registry validation errors.
    Uses JSONPath format for precise error location.
    """
    if not errors:
        return ""
    
    hints = []
    for err in errors[:10]:  # Limit to 10 errors
        if "metric_unknown" in err:
            hints.append(f"• CHANGE `metric` to a registered name (see allowed metrics list)")
        elif "metric_pipe_forbidden" in err:
            hints.append(f"• SPLIT combined metrics into separate measurements (no '|' allowed)")
        elif "condition_key_missing" in err:
            key = err.split(":")[-1]
            hints.append(f"• ADD `conditions.{key}` (set to null if unknown)")
        elif "null_value_without_status" in err:
            hints.append(f"• ADD `tags.value_status`: use 'FIGURE_DIGITIZE_REQUIRED' or 'NOT_FOUND'")
        elif "current_slot_conflict" in err:
            hints.append(f"• KEEP only ONE current-type field (areal_current_density OR specific_current OR rate_C)")
        elif "placeholder_quote" in err:
            hints.append(f"• REPLACE `evidence.quote` with actual sentence from paper (no placeholders)")
        elif "evidence_doc_missing" in err:
            hints.append(f"• SET `evidence.doc` to 'MAIN' or 'SUPP'")
        elif "evidence_section_path_missing" in err:
            hints.append(f"• SET `evidence.section_path` to actual section name")
        else:
            hints.append(f"• FIX: {err}")
    
    return "\n".join(hints)

