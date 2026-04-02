# scripts/lib/metric_registry.py
"""
METRIC_REGISTRY v1.0 - Single Source of Truth for Measurement Schema

This module defines:
- 25 core metrics for aqueous Zn battery ex-situ protective layer research
- Canonical conditions with mutual exclusivity rules
- Strict validation specifications per metric
- Evidence requirements for traceability

Reference: 11_설계 METRIC_REGISTRY v1.0
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Union


# ============================================================================
# MetricSpec Dataclass
# ============================================================================

@dataclass
class MetricSpec:
    """Specification for a single metric in the registry."""
    value_type: str                           # "number"|"string"|"array"|"nullable_number"
    canonical_unit: Optional[str]             # e.g., "mA/cm2", None for strings
    required_conditions: List[str]            # keys that MUST exist (null value OK)
    allowed_tags: List[str]                   # allowed tag keys for this metric
    evidence_requires: List[str] = field(     # evidence fields that must be present
        default_factory=lambda: ["doc", "section_path", "quote"]
    )
    forbid_patterns: List[str] = field(       # patterns forbidden in value/metric
        default_factory=list
    )
    value_range: Optional[tuple] = None       # (min, max) for numeric validation
    allowed_values: Optional[List[str]] = None  # enum values for string types


# ============================================================================
# CANONICAL_CONDITIONS - Universal condition keys
# ============================================================================

CANONICAL_CONDITIONS = {
    "cell_type": None,                # SYMMETRIC|FULL_CELL|HALF_CELL|OTHER
    "temperature_C": None,            # Temperature in Celsius
    "electrolyte": None,              # Electrolyte description
    "separator": None,                # Separator material
    "current_mode": None,             # GALVANOSTATIC|POTENTIOSTATIC
    "areal_capacity_mAh_cm2": None,   # Areal capacity
    "cutoff_voltage_V": None,         # Cutoff voltage
}

# Current-type slots (MUTUALLY EXCLUSIVE - only ONE may be non-null)
CURRENT_TYPE_SLOTS = [
    "areal_current_density_mA_cm2",
    "specific_current_A_g",
    "rate_C"
]


# ============================================================================
# VALUE_STATUS - Standard null value reasons
# ============================================================================

ALLOWED_VALUE_STATUS = {
    "OK",                        # Value found and extracted
    "FIGURE_DIGITIZE_REQUIRED",  # Value only in plot, needs digitization
    "NOT_FOUND",                 # Value not present in paper
    "OK_UNIT_UNCLEAR",           # Value found but unit is unclear
    "OK_CONTEXT_UNCLEAR",        # Value found but context is unclear
}


# ============================================================================
# TAG ENUMS - Standard tag values
# ============================================================================

TAG_ENUMS = {
    "source_type": ["TEXT", "TABLE", "FIGURE", "DFT"],
    "before_after": ["BEFORE_COATING", "AFTER_COATING", "UNCLEAR"],
    "overpotential_type": ["NUCLEATION", "DEPOSITION", "HYSTERESIS", "APPLIED_CA", "UNCLEAR"],
    "eis_param": ["Rs", "Rct", "R0", "Rsei", "Warburg", "UNCLEAR"],
    "ionic_conductivity_scope": ["COATING", "ELECTROLYTE", "UNCLEAR"],
    "sample_type": ["COATED", "BARE_ZN", "CONTROL", "UNCLEAR"],
    "cell_type": ["SYMMETRIC", "FULL_CELL", "HALF_CELL", "OTHER", "UNCLEAR"],
}


# ============================================================================
# METRIC_REGISTRY v1.0 - 25 Core Metrics
# ============================================================================

METRIC_REGISTRY: Dict[str, MetricSpec] = {
    
    # =========================================================================
    # INPUT METRICS - Protective Layer Characterization
    # =========================================================================
    
    "protective_layer_material": MetricSpec(
        value_type="string",
        canonical_unit=None,
        required_conditions=[],
        allowed_tags=["source_type"],
        evidence_requires=["doc", "section_path", "quote"],
    ),
    
    "protective_layer_method": MetricSpec(
        value_type="string",
        canonical_unit=None,
        required_conditions=[],
        allowed_tags=["source_type"],
        allowed_values=["ELECTRODEPOSITION", "SPIN_COATING", "DROP_CASTING", 
                        "DIP_COATING", "CVD", "ALD", "SPRAY", "BLADE_COATING", 
                        "IN_SITU_REACTION", "OTHER"],
    ),
    
    "protective_layer_thickness_nm": MetricSpec(
        value_type="nullable_number",
        canonical_unit="nm",
        required_conditions=[],
        allowed_tags=["source_type", "value_status"],
        value_range=(0.1, 100000),  # 0.1 nm to 100 um
    ),
    
    "protective_layer_loading_mg_cm2": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mg/cm2",
        required_conditions=[],
        allowed_tags=["source_type", "value_status"],
        value_range=(0.001, 100),
    ),
    
    # =========================================================================
    # FABRICATION METRICS
    # =========================================================================
    
    "deposition_potential_V_vs_ref": MetricSpec(
        value_type="number",
        canonical_unit="V",
        required_conditions=["reference_electrode"],
        allowed_tags=["source_type"],
    ),
    
    "deposition_time_min": MetricSpec(
        value_type="number",
        canonical_unit="min",
        required_conditions=[],
        allowed_tags=["source_type"],
        value_range=(0.01, 1440),  # seconds to 24 hours
    ),
    
    # =========================================================================
    # PROPERTY METRICS
    # =========================================================================
    
    "electrolyte_ionic_conductivity_mS_cm": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mS/cm",
        required_conditions=["temperature_C"],
        allowed_tags=["ionic_conductivity_scope", "value_status"],
        value_range=(0.001, 1000),
    ),
    
    "contact_angle_deg": MetricSpec(
        value_type="nullable_number",
        canonical_unit="deg",
        required_conditions=["temperature_C"],
        allowed_tags=["before_after", "value_status"],
        value_range=(0, 180),
    ),
    
    "zn_adsorption_energy_eV": MetricSpec(
        value_type="number",
        canonical_unit="eV",
        required_conditions=[],
        allowed_tags=["source_type"],  # typically DFT
        value_range=(-10, 0),  # usually negative for adsorption
    ),
    
    "zn_binding_energy_eV": MetricSpec(
        value_type="number",
        canonical_unit="eV",
        required_conditions=[],
        allowed_tags=["source_type"],
        value_range=(-10, 0),
    ),
    
    # =========================================================================
    # CURRENT/CAPACITY SLOTS (mutually exclusive current types)
    # =========================================================================
    
    "areal_current_density_mA_cm2": MetricSpec(
        value_type="number",
        canonical_unit="mA/cm2",
        required_conditions=["cell_type"],
        allowed_tags=["value_status"],
        value_range=(0.01, 1000),
    ),
    
    "specific_current_A_g": MetricSpec(
        value_type="number",
        canonical_unit="A/g",
        required_conditions=["cell_type"],
        allowed_tags=["value_status"],
        value_range=(0.001, 100),
    ),
    
    "rate_C": MetricSpec(
        value_type="number",  # can be float like 0.5C
        canonical_unit="C",
        required_conditions=["cell_type"],
        allowed_tags=["value_status"],
        value_range=(0.01, 100),
    ),
    
    "areal_capacity_mAh_cm2": MetricSpec(
        value_type="number",
        canonical_unit="mAh/cm2",
        required_conditions=["cell_type"],
        allowed_tags=["value_status"],
        value_range=(0.01, 100),
    ),
    
    # =========================================================================
    # CYCLING METRICS
    # =========================================================================
    
    "cycle_life_hours": MetricSpec(
        value_type="nullable_number",
        canonical_unit="h",
        required_conditions=["cell_type", "temperature_C"],  # + current slot
        allowed_tags=["value_status", "source_type"],
        evidence_requires=["doc", "section_path", "quote", "figure_id|table_id"],
        value_range=(0.1, 50000),
    ),
    
    "cycle_life_cycles": MetricSpec(
        value_type="nullable_number",
        canonical_unit="cycles",
        required_conditions=["cell_type", "temperature_C"],
        allowed_tags=["value_status", "source_type"],
        evidence_requires=["doc", "section_path", "quote", "figure_id|table_id"],
        value_range=(1, 100000),
    ),
    
    "capacity_retention_pct": MetricSpec(
        value_type="nullable_number",
        canonical_unit="%",
        required_conditions=["cell_type", "cycle_number"],
        allowed_tags=["value_status", "source_type"],
        value_range=(0, 100),
    ),
    
    "coulombic_efficiency_pct": MetricSpec(
        value_type="nullable_number",
        canonical_unit="%",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type"],
        value_range=(0, 100),
    ),
    
    # =========================================================================
    # EIS METRICS
    # =========================================================================
    
    "eis_Rs_Ohm": MetricSpec(
        value_type="nullable_number",
        canonical_unit="Ohm",
        required_conditions=["temperature_C"],
        allowed_tags=["eis_param", "before_after", "value_status"],
        value_range=(0, 10000),
    ),
    
    "eis_Rct_Ohm": MetricSpec(
        value_type="nullable_number",
        canonical_unit="Ohm",
        required_conditions=["temperature_C"],
        allowed_tags=["eis_param", "before_after", "value_status"],
        value_range=(0, 100000),
    ),
    
    "eis_frequency_range_Hz": MetricSpec(
        value_type="array",  # [min_Hz, max_Hz]
        canonical_unit="Hz",
        required_conditions=[],
        allowed_tags=["value_status"],
    ),
    
    "eis_ac_amplitude_V": MetricSpec(
        value_type="number",
        canonical_unit="V",
        required_conditions=[],
        allowed_tags=[],
        value_range=(0.001, 1),
    ),
    
    # =========================================================================
    # OVERPOTENTIAL METRICS
    # =========================================================================
    
    "overpotential_mV": MetricSpec(
        value_type="nullable_number",  # can be array for multiple values
        canonical_unit="mV",
        required_conditions=[],  # current slot is contextual
        allowed_tags=["overpotential_type", "value_status", "before_after", "sample_type"],
        value_range=(0, 2000),
    ),
    
    "nucleation_overpotential_mV": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mV",
        required_conditions=["cell_type"],
        allowed_tags=["overpotential_type", "value_status", "before_after", "sample_type"],
        value_range=(0, 500),
    ),
    
    "deposition_overpotential_mV": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mV",
        required_conditions=["cell_type"],
        allowed_tags=["overpotential_type", "value_status", "before_after", "sample_type"],
        value_range=(0, 500),
    ),
    
    # =========================================================================
    # CORROSION METRICS
    # =========================================================================
    
    "corrosion_potential_V": MetricSpec(
        value_type="number",
        canonical_unit="V",
        required_conditions=["electrolyte", "reference_electrode"],
        allowed_tags=["sample_type", "value_status"],
    ),
    
    "corrosion_current_uA_cm2": MetricSpec(
        value_type="number",
        canonical_unit="uA/cm2",
        required_conditions=["electrolyte"],
        allowed_tags=["sample_type", "value_status"],
        value_range=(0, 10000),
    ),
    
    # =========================================================================
    # VOLTAGE HYSTERESIS (Cycling Related)
    # =========================================================================
    
    "voltage_hysteresis_mV": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mV",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type"],
        value_range=(0, 500),
    ),
    
    # =========================================================================
    # RATE PERFORMANCE METRICS (18_설계 Phase 1.1)
    # =========================================================================
    
    "specific_capacity_mAh_g": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mAh/g",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type"],
        value_range=(1, 1000),
    ),
    
    "energy_density_Wh_kg": MetricSpec(
        value_type="nullable_number",
        canonical_unit="Wh/kg",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type"],
        value_range=(1, 1000),
    ),
    
    "power_density_W_kg": MetricSpec(
        value_type="nullable_number",
        canonical_unit="W/kg",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type"],
        value_range=(1, 50000),
    ),
    
    # =========================================================================
    # CORROSION METRICS
    # =========================================================================
    
    "corrosion_current_density_uAcm2": MetricSpec(
        value_type="nullable_number",
        canonical_unit="uA/cm2",
        required_conditions=["electrolyte"],
        allowed_tags=["sample_type", "value_status", "source_type"],
        value_range=(0.1, 10000),
    ),
    
    "corrosion_potential_V": MetricSpec(
        value_type="nullable_number",
        canonical_unit="V",
        required_conditions=["electrolyte", "reference_electrode"],
        allowed_tags=["sample_type", "value_status", "source_type"],
        value_range=(-2, 0),
    ),

    # =========================================================================
    # MATERIAL PROPERTIES (Round 14 New)
    # =========================================================================

    "youngs_modulus_GPa": MetricSpec(
        value_type="number",
        canonical_unit="GPa",
        required_conditions=[],
        allowed_tags=["source_type", "sample_type"],
        value_range=(0.01, 1000.0),
    ),

    "pore_size_nm": MetricSpec(
        value_type="number",
        canonical_unit="nm",
        required_conditions=[],
        allowed_tags=["source_type", "sample_type"],
        value_range=(0.1, 100.0),
    ),

    "surface_work_function_eV": MetricSpec(
        value_type="number",
        canonical_unit="eV",
        required_conditions=[],
        allowed_tags=["source_type", "sample_type", "before_after"],
    ),

    "surface_potential_mV": MetricSpec(
        value_type="number",
        canonical_unit="mV",
        required_conditions=[],
        allowed_tags=["source_type", "sample_type", "before_after"],
        # Captures Vcpd (Contact Potential Difference) from KPFM
    ),

    "eis_frequency_range_Hz": MetricSpec(
        value_type="string", # "100 kHz - 0.1 Hz"
        canonical_unit="Hz",
        required_conditions=[],
        allowed_tags=["source_type"],
        forbid_patterns=[],
    ),
    
    # =========================================================================
    # KINETICS METRICS (18_설계 Phase 1.2)
    # =========================================================================
    
    "transference_number": MetricSpec(
        value_type="nullable_number",
        canonical_unit=None,
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type"],
        value_range=(0, 1),
    ),
    
    "nucleation_overpotential_mV": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mV",
        required_conditions=["cell_type"],
        allowed_tags=["overpotential_type", "value_status", "source_type"],
        value_range=(0, 500),
    ),
    
    "deposition_overpotential_mV": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mV",
        required_conditions=["cell_type"],
        allowed_tags=["overpotential_type", "value_status", "source_type", "sample_type"],
        value_range=(0, 300),
    ),
    
    # =========================================================================
    # CORROSION METRICS - Alternative Unit (18_설계 Phase 1.3)
    # =========================================================================
    
    "corrosion_current_mA_cm2": MetricSpec(
        value_type="number",
        canonical_unit="mA/cm2",
        required_conditions=["electrolyte"],
        allowed_tags=["sample_type", "value_status"],
        value_range=(0, 100),
    ),
    
    # =========================================================================
    # KINETICS METRICS - Phase 3
    # =========================================================================
    
    "ion_diffusion_coeff_cm2_s": MetricSpec(
        value_type="nullable_number",
        canonical_unit="cm2/s",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "value_type", "sample_type"],
        value_range=(1e-15, 1e-6),
    ),
    
    "capacitive_contribution_pct": MetricSpec(
        value_type="nullable_number",
        canonical_unit="%",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type"],
        value_range=(0, 100),
    ),
    
    "b_value_kinetic_exponent": MetricSpec(
        value_type="nullable_number",
        canonical_unit=None,  # dimensionless
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type", "value_type"],
        value_range=(0.5, 1.0),  # b=0.5 diffusion, b=1.0 surface-controlled
    ),
    
    "double_layer_capacitance_mF_cm2": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mF/cm2",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type"],
        value_range=(0.01, 1000),
    ),
    
    "transference_number": MetricSpec(
        value_type="nullable_number",
        canonical_unit=None,  # dimensionless, 0-1
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type", "ion_species"],
        value_range=(0, 1),
    ),
    
    # =========================================================================
    # P3-1: NEW METRICS FROM GPT EVALUATION (2026-01-25)
    # =========================================================================
    
    # HER Metrics
    "her_potential_V": MetricSpec(
        value_type="nullable_number",
        canonical_unit="V",
        required_conditions=["cell_type", "areal_current_density_mA_cm2"],
        allowed_tags=["value_status", "source_type", "sample_type", "reference_electrode"],
        value_range=(-3.0, 0.5),
    ),
    
    "her_overpotential_mV": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mV",
        required_conditions=["cell_type", "areal_current_density_mA_cm2"],
        allowed_tags=["value_status", "source_type", "sample_type"],
        value_range=(0, 2000),
    ),
    
    # Deposition Metrics
    "deposition_current_density_mA_cm2": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mA/cm2",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type"],
        value_range=(0.01, 100),
    ),
    
    "deposition_time_s": MetricSpec(
        value_type="nullable_number",
        canonical_unit="s",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type"],
        value_range=(1, 100000),
    ),
    
    # Electrode Loading
    "cathode_loading_mg_cm2": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mg/cm2",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type", "cathode_material"],
        value_range=(0.1, 50),
    ),
    
    # Surface Properties (from COF paper evaluation)
    "surface_work_function_eV": MetricSpec(
        value_type="nullable_number",
        canonical_unit="eV",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type"],
        value_range=(3.0, 7.0),
    ),
    
    "contact_potential_difference_mV": MetricSpec(
        value_type="nullable_number",
        canonical_unit="mV",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type"],
        value_range=(-500, 500),
    ),
    
    "youngs_modulus_GPa": MetricSpec(
        value_type="nullable_number",
        canonical_unit="GPa",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "sample_type"],
        value_range=(0.001, 500),
    ),
    
    # Chronoamperometry
    "chronoamperometry_time_s": MetricSpec(
        value_type="nullable_number",
        canonical_unit="s",
        required_conditions=["cell_type", "overpotential_mV"],
        allowed_tags=["value_status", "source_type", "sample_type"],
        value_range=(1, 10000),
    ),
    
    # EIS Extended - R0 and Rsei
    "eis_R0_Ohm": MetricSpec(
        value_type="nullable_number",
        canonical_unit="Ω",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "before_after", "sample_type", "eis_metric_type", "eis_frequency_Hz"],
        evidence_requires=["doc", "section_path", "quote", "(figure_id|table_id)"],
        value_range=(0.01, 100000),
    ),
    
    "eis_Rsei_Ohm": MetricSpec(
        value_type="nullable_number",
        canonical_unit="Ω",
        required_conditions=["cell_type"],
        allowed_tags=["value_status", "source_type", "before_after", "sample_type", "eis_metric_type"],
        evidence_requires=["doc", "section_path", "quote", "(figure_id|table_id)"],
        value_range=(0.01, 100000),
    ),
}


# ============================================================================
# EXTRACTOR → METRIC MAPPING
# ============================================================================

EXTRACTOR_METRICS = {
    "EXTRACT_INPUT": [
        "protective_layer_material",
        "protective_layer_method", 
        "protective_layer_thickness_nm",
        "protective_layer_loading_mg_cm2",
        "deposition_potential_V_vs_ref",
        "deposition_time_min",
        "zn_adsorption_energy_eV",
        "zn_binding_energy_eV",
        "electrolyte_ionic_conductivity_mS_cm",
        "contact_angle_deg",
    ],
    "EXTRACT_EIS": [
        "eis_Rs_Ohm",
        "eis_Rct_Ohm",
        "eis_frequency_range_Hz",
        "eis_ac_amplitude_V",
    ],
    "EXTRACT_OVERPOTENTIAL": [
        "overpotential_mV",
        "nucleation_overpotential_mV",
        "deposition_overpotential_mV",
        "voltage_hysteresis_mV",
    ],
    "EXTRACT_CYCLING": [
        "cycle_life_hours",
        "cycle_life_cycles",
        "capacity_retention_pct",
        "coulombic_efficiency_pct",
        "areal_current_density_mA_cm2",
        "areal_capacity_mAh_cm2",
        "voltage_hysteresis_mV",
    ],
    "EXTRACT_CORROSION": [
        "corrosion_potential_V",
        "corrosion_current_uA_cm2",
        "corrosion_current_mA_cm2",
    ],
    "EXTRACT_RATE": [
        "specific_capacity_mAh_g",
        "areal_capacity_mAh_cm2",
        "energy_density_Wh_kg",
        "power_density_W_kg",
        "capacity_retention_pct",
        "coulombic_efficiency_pct",
    ],
    "EXTRACT_KINETICS": [
        "transference_number",
        "ion_diffusion_coeff_cm2_s",
        "capacitive_contribution_pct",
        "b_value_kinetic_exponent",
        "double_layer_capacitance_mF_cm2",
    ],
}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_metrics_for_extractor(extractor_name: str) -> List[str]:
    """Get list of allowed metrics for a specific extractor."""
    return EXTRACTOR_METRICS.get(extractor_name, [])


def get_required_conditions(metric: str) -> List[str]:
    """Get required condition keys for a metric."""
    spec = METRIC_REGISTRY.get(metric)
    if spec:
        return spec.required_conditions
    return []


def get_allowed_tags(metric: str) -> List[str]:
    """Get allowed tag keys for a metric."""
    spec = METRIC_REGISTRY.get(metric)
    if spec:
        return spec.allowed_tags
    return []


def is_valid_metric(metric: str) -> bool:
    """Check if metric name is in the registry."""
    return metric in METRIC_REGISTRY


def has_forbidden_pattern(metric: str) -> bool:
    """Check if metric contains forbidden patterns like '|'."""
    return "|" in metric


def get_all_registered_metrics() -> List[str]:
    """Get list of all registered metric names."""
    return list(METRIC_REGISTRY.keys())


def build_metric_list_for_prompt(extractor_name: str) -> str:
    """Build a formatted metric list for prompt injection."""
    metrics = get_metrics_for_extractor(extractor_name)
    if not metrics:
        metrics = get_all_registered_metrics()
    return ", ".join(f"`{m}`" for m in metrics)


def build_required_conditions_for_prompt(extractor_name: str) -> str:
    """Build a list of all required conditions for an extractor's metrics."""
    metrics = get_metrics_for_extractor(extractor_name)
    all_conditions = set()
    for m in metrics:
        all_conditions.update(get_required_conditions(m))
    # Add universal conditions
    all_conditions.update(CANONICAL_CONDITIONS.keys())
    return ", ".join(sorted(all_conditions))
