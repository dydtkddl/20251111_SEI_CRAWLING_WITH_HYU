# scripts/contracts/metric_specs.py
"""
Metric Specification Registry for enterprise-grade extraction.

Each metric specifies:
- allowed_units: Valid unit strings
- required_conditions: Keys that MUST exist (value can be null)
- required_tags: Keys that MUST exist with enum values
- value_type: scalar / scalar_array / string / string_or_null
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

# =============================================================================
# Metric Specifications
# =============================================================================

METRIC_SPECS: Dict[str, Dict[str, Any]] = {
    # --- Current/Capacity Metrics ---
    "areal_current_density_mAcm2": {
        "allowed_units": ["mA/cm2", "mA cm-2", "mA·cm⁻²"],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "scalar"
    },
    "areal_capacity_mAhcm2": {
        "allowed_units": ["mAh/cm2", "mAh cm-2", "mA h cm-2"],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "scalar"
    },
    
    # --- EIS Metrics ---
    "eis_Rct_Ohm": {
        "allowed_units": ["Ohm", "Ω", "ohm"],
        "required_conditions": [],  # frequency_range_Hz is optional
        "required_tags": ["eis_metric_type"],
        "value_type": "scalar"
    },
    "eis_Rs_Ohm": {
        "allowed_units": ["Ohm", "Ω", "ohm"],
        "required_conditions": [],
        "required_tags": ["eis_metric_type"],
        "value_type": "scalar"
    },
    
    # --- Overpotential Metrics ---
    "overpotential_mV": {
        "allowed_units": ["mV"],
        "required_conditions": ["current_density_mAcm2"],
        "required_tags": ["overpotential_type"],
        "value_type": "scalar_or_series"
    },
    "nucleation_overpotential_mV": {
        "allowed_units": ["mV"],
        "required_conditions": ["current_density_mAcm2"],
        "required_tags": [],
        "value_type": "scalar"
    },
    "voltage_hysteresis_mV": {
        "allowed_units": ["mV"],
        "required_conditions": ["current_density_mAcm2"],
        "required_tags": [],
        "value_type": "scalar"
    },
    
    # --- Cycling Metrics ---
    "galvanostatic_cycling_cycles": {
        "allowed_units": ["cycles", "cycle"],
        "required_conditions": ["current_density_mAcm2"],
        "required_tags": [],
        "value_type": "scalar"
    },
    "galvanostatic_cycling_performance_h": {
        "allowed_units": ["h", "hours", "hr"],
        "required_conditions": ["current_density_mAcm2"],
        "required_tags": [],
        "value_type": "scalar"
    },
    "coulombic_efficiency_pct": {
        "allowed_units": ["%", "percent"],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "scalar"
    },
    
    # --- Material Properties ---
    "protective_layer_material": {
        "allowed_units": [None],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "string"
    },
    "protective_layer_thickness_nm": {
        "allowed_units": ["nm", "nanometer"],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "scalar"
    },
    "protective_layer_thickness_um": {
        "allowed_units": ["um", "µm", "micrometer"],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "scalar"
    },
    
    # --- Energy/Power Density ---
    "energy_density_Wh_kg": {
        "allowed_units": ["Wh/kg", "Wh kg-1"],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "scalar"
    },
    "energy_density_Wh_L": {
        "allowed_units": ["Wh/L", "Wh L-1"],
        "required_conditions": [],
        "required_tags": ["basis"],  # VOLUMETRIC
        "value_type": "scalar"
    },
    "power_density_W_kg": {
        "allowed_units": ["W/kg", "W kg-1"],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "scalar"
    },
    
    # --- Electrolyte ---
    "electrolyte_raw": {
        "allowed_units": [None],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "string"
    },
    "electrolyte_concentration_M": {
        "allowed_units": ["M", "mol/L"],
        "required_conditions": [],
        "required_tags": [],
        "value_type": "scalar"
    },
}


# =============================================================================
# Validation Functions
# =============================================================================

def get_metric_spec(metric: str) -> Optional[Dict[str, Any]]:
    """Get specification for a metric. Returns None if unknown."""
    return METRIC_SPECS.get(metric)


def validate_metric_requirements(m: Dict[str, Any]) -> List[str]:
    """
    Validate a measurement against its metric specification.
    Returns list of error strings.
    """
    errors = []
    metric = m.get("metric")
    
    if not metric:
        return ["METRIC_MISSING"]
    
    spec = METRIC_SPECS.get(metric)
    if not spec:
        # Unknown metric - allow but flag
        return []  # Could add: ["METRIC_UNKNOWN: {metric}"]
    
    # Unit validation
    unit = m.get("unit")
    allowed = spec.get("allowed_units", [])
    if allowed and unit not in allowed:
        if not (None in allowed and unit is None):
            errors.append(f"UNIT_INVALID: {metric} unit must be one of {allowed}, got {unit}")
    
    # Required conditions
    conditions = m.get("conditions") or {}
    for cond_key in spec.get("required_conditions", []):
        if cond_key not in conditions:
            errors.append(f"COND_MISSING: conditions.{cond_key} required for {metric}")
    
    # Required tags
    tags = m.get("tags") or {}
    for tag_key in spec.get("required_tags", []):
        if tag_key not in tags:
            errors.append(f"TAG_MISSING: tags.{tag_key} required for {metric}")
    
    return errors


def build_metric_requirements_prompt(metrics: List[str]) -> str:
    """
    Build a prompt snippet describing metric requirements.
    Used to guide LLM extraction.
    """
    lines = ["METRIC REQUIREMENTS (must fill conditions/tags if available):"]
    
    for metric in metrics:
        spec = METRIC_SPECS.get(metric)
        if not spec:
            continue
        
        parts = [f"- {metric}:"]
        
        units = spec.get("allowed_units", [])
        if units and units[0] is not None:
            parts.append(f"unit must be one of {units}")
        
        conds = spec.get("required_conditions", [])
        if conds:
            parts.append(f"conditions: {', '.join(conds)}")
        
        tags = spec.get("required_tags", [])
        if tags:
            parts.append(f"tags: {', '.join(tags)}")
        
        lines.append(" ".join(parts))
    
    return "\n".join(lines)
