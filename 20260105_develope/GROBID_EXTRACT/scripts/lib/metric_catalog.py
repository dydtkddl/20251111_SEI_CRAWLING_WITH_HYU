# scripts/lib/metric_catalog.py
"""
Unified Metric Catalog - Single Source of Truth

Per 15_설계.md Section 4 (Day 1):
Unifies VALID_METRICS, METRIC_REGISTRY, RANGES, and aliases into a single source.

This prevents "Unknown metric" warnings and ensures consistent validation
across extractors, QC, and post-processing.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Set, Optional, Tuple, List


@dataclass
class MetricSpec:
    """Specification for a single metric."""
    canonical_name: str
    category: str  # INPUT, CYCLING, EIS, DFT, CORROSION, OVERPOTENTIAL
    allowed_units: Set[str]
    default_unit: str
    value_range: Optional[Tuple[float, float]] = None
    required_conditions: Set[str] = field(default_factory=set)
    required_tags: Set[str] = field(default_factory=set)
    aliases: Set[str] = field(default_factory=set)
    description: str = ""
    is_setting: bool = False  # True for EIS frequency/amplitude - should be injected as conditions


# =============================================================================
# UNIFIED METRIC CATALOG
# =============================================================================
METRIC_CATALOG: Dict[str, MetricSpec] = {
    # === INPUT METRICS ===
    "protective_layer_material": MetricSpec(
        canonical_name="protective_layer_material",
        category="INPUT",
        allowed_units={"", None},
        default_unit="",
        description="Protective layer material name (e.g., graphene, ZnO)",
        aliases={"coating_material", "layer_material"}
    ),
    "protective_layer_deposition_method": MetricSpec(
        canonical_name="protective_layer_deposition_method",
        category="INPUT",
        allowed_units={"", None},
        default_unit="",
        description="Coating deposition method (e.g., electrodeposition)",
        aliases={"protective_layer_method", "coating_method", "deposition_method"}
    ),
    "protective_layer_thickness_nm": MetricSpec(
        canonical_name="protective_layer_thickness_nm",
        category="INPUT",
        allowed_units={"nm"},
        default_unit="nm",
        value_range=(0.1, 10000),
        description="Protective layer thickness in nanometers",
        aliases={"graphene_sheet_thickness_nm", "layer_thickness_nm"}
    ),
    "protective_layer_thickness_um": MetricSpec(
        canonical_name="protective_layer_thickness_um",
        category="INPUT",
        allowed_units={"um", "μm"},
        default_unit="um",
        value_range=(0.001, 1000),
        description="Protective layer thickness in micrometers"
    ),
    "protective_layer_loading_mg_cm2": MetricSpec(
        canonical_name="protective_layer_loading_mg_cm2",
        category="INPUT",
        allowed_units={"mg/cm2", "mg cm-2"},
        default_unit="mg/cm2",
        value_range=(0.001, 100),
        description="Areal loading of protective layer",
        aliases={"coating_loading_mg_cm2"}
    ),
    "deposition_potential_V": MetricSpec(
        canonical_name="deposition_potential_V",
        category="INPUT",
        allowed_units={"V"},
        default_unit="V",
        value_range=(-5, 5),
        description="Electrodeposition potential",
        aliases={"deposition_potential_V_vs_ref", "coating_potential_V", 
                 "protective_layer_deposition_potential_V_vs_AgAgCl"}
    ),
    "deposition_time_min": MetricSpec(
        canonical_name="deposition_time_min",
        category="INPUT",
        allowed_units={"min"},
        default_unit="min",
        value_range=(0.1, 1440),
        description="Electrodeposition time",
        aliases={"coating_time_min", "protective_layer_deposition_time_min"}
    ),
    "ion_conductivity_mS_cm": MetricSpec(
        canonical_name="ion_conductivity_mS_cm",
        category="INPUT",
        allowed_units={"mS/cm", "mS cm-1"},
        default_unit="mS/cm",
        value_range=(0.001, 1000),
        required_tags={"ionic_conductivity_scope"},
        description="Ionic conductivity",
        aliases={"electrolyte_ionic_conductivity_mS_cm"}
    ),
    "contact_angle_deg": MetricSpec(
        canonical_name="contact_angle_deg",
        category="INPUT",
        allowed_units={"deg", "°"},
        default_unit="deg",
        value_range=(0, 180),
        description="Contact angle measurement"
    ),
    
    # === EIS METRICS ===
    "eis_Rct_Ohm": MetricSpec(
        canonical_name="eis_Rct_Ohm",
        category="EIS",
        allowed_units={"Ohm", "Ω", "ohm"},
        default_unit="Ohm",
        value_range=(0.01, 1e5),
        required_tags={"eis_metric_type", "before_after"},
        description="Charge transfer resistance"
    ),
    "eis_Rs_Ohm": MetricSpec(
        canonical_name="eis_Rs_Ohm",
        category="EIS",
        allowed_units={"Ohm", "Ω", "ohm"},
        default_unit="Ohm",
        value_range=(0.01, 1000),
        required_tags={"eis_metric_type", "before_after"},
        description="Solution/electrolyte resistance"
    ),
    "eis_R0_Ohm": MetricSpec(
        canonical_name="eis_R0_Ohm",
        category="EIS",
        allowed_units={"Ohm", "Ω", "ohm"},
        default_unit="Ohm",
        value_range=(0.01, 1000),
        description="Initial/bulk resistance"
    ),
    "eis_Rsei_Ohm": MetricSpec(
        canonical_name="eis_Rsei_Ohm",
        category="EIS",
        allowed_units={"Ohm", "Ω", "ohm"},
        default_unit="Ohm",
        value_range=(0.01, 1e5),
        description="SEI layer resistance"
    ),
    # EIS settings - marked as is_setting=True, should not be standalone metrics
    "eis_frequency_range_Hz": MetricSpec(
        canonical_name="eis_frequency_range_Hz",
        category="EIS",
        allowed_units={"Hz"},
        default_unit="Hz",
        is_setting=True,
        description="EIS frequency range - should be condition, not metric"
    ),
    "eis_ac_amplitude_V": MetricSpec(
        canonical_name="eis_ac_amplitude_V",
        category="EIS",
        allowed_units={"V", "mV"},
        default_unit="V",
        is_setting=True,
        description="EIS AC amplitude - should be condition, not metric"
    ),
    
    # === DFT METRICS ===
    "zn_adsorption_energy_eV": MetricSpec(
        canonical_name="zn_adsorption_energy_eV",
        category="DFT",
        allowed_units={"eV"},
        default_unit="eV",
        value_range=(-10, 0),
        required_tags={"zn_adsorption_source"},
        description="Zn adsorption energy from DFT",
        aliases={"zn_binding_energy_eV"}
    ),
    
    # === CYCLING METRICS ===
    "galvanostatic_cycling_cycles": MetricSpec(
        canonical_name="galvanostatic_cycling_cycles",
        category="CYCLING",
        allowed_units={"cycles", ""},
        default_unit="cycles",
        value_range=(1, 1000000),
        description="Number of cycles achieved"
    ),
    "galvanostatic_cycling_performance_h": MetricSpec(
        canonical_name="galvanostatic_cycling_performance_h",
        category="CYCLING",
        allowed_units={"h", "hours"},
        default_unit="h",
        value_range=(0.1, 50000),
        description="Cycling lifetime in hours"
    ),
    "galvanostatic_cycling_capacity_retention": MetricSpec(
        canonical_name="galvanostatic_cycling_capacity_retention",
        category="CYCLING",
        allowed_units={"%", ""},
        default_unit="%",
        value_range=(0, 100),
        description="Capacity retention percentage"
    ),
    "specific_capacity_mAh_g": MetricSpec(
        canonical_name="specific_capacity_mAh_g",
        category="CYCLING",
        allowed_units={"mAh/g", "mAh g-1"},
        default_unit="mAh/g",
        value_range=(1, 1000),
        description="Gravimetric specific capacity"
    ),
    "areal_capacity_mAh_cm2": MetricSpec(
        canonical_name="areal_capacity_mAh_cm2",
        category="CYCLING",
        allowed_units={"mAh/cm2", "mAh cm-2"},
        default_unit="mAh/cm2",
        value_range=(0.01, 100),
        description="Areal capacity"
    ),
    
    # === OVERPOTENTIAL ===
    "overpotential_mV": MetricSpec(
        canonical_name="overpotential_mV",
        category="OVERPOTENTIAL",
        allowed_units={"mV"},
        default_unit="mV",
        value_range=(0, 1000),
        required_tags={"overpotential_type"},
        description="Overpotential (nucleation, deposition, etc.)"
    ),
    
    # === CORROSION ===
    "corrosion_potential_V": MetricSpec(
        canonical_name="corrosion_potential_V",
        category="CORROSION",
        allowed_units={"V"},
        default_unit="V",
        value_range=(-3.0, 1.0),
        description="Corrosion potential"
    ),
    "corrosion_current_density_uA_cm2": MetricSpec(
        canonical_name="corrosion_current_density_uA_cm2",
        category="CORROSION",
        allowed_units={"uA/cm2", "μA/cm2", "μA cm-2"},
        default_unit="uA/cm2",
        value_range=(0.001, 10000),
        description="Corrosion current density"
    ),
    
    # === CYCLING METRICS (Extended - 17_설계) ===
    "cycle_life_hours": MetricSpec(
        canonical_name="cycle_life_hours",
        category="CYCLING",
        allowed_units={"h", "hours"},
        default_unit="h",
        value_range=(1, 50000),
        description="Cycling lifetime in hours (symmetric cell)",
        aliases={"cycling_hours", "lifespan_hours", "galvanostatic_cycling_performance_h"}
    ),
    "cycle_life_cycles": MetricSpec(
        canonical_name="cycle_life_cycles",
        category="CYCLING",
        allowed_units={"cycles", ""},
        default_unit="cycles",
        value_range=(1, 100000),
        description="Number of cycles achieved",
        aliases={"total_cycles", "cycling_cycles", "galvanostatic_cycling_cycles"}
    ),
    "capacity_retention_pct": MetricSpec(
        canonical_name="capacity_retention_pct",
        category="CYCLING",
        allowed_units={"%"},
        default_unit="%",
        value_range=(0, 100),
        description="Capacity retention percentage",
        aliases={"galvanostatic_cycling_capacity_retention"}
    ),
    "coulombic_efficiency_pct": MetricSpec(
        canonical_name="coulombic_efficiency_pct",
        category="CYCLING",
        allowed_units={"%"},
        default_unit="%",
        value_range=(50, 100),
        description="Coulombic efficiency"
    ),
    
    # === RATE PERFORMANCE (NEW - 17_설계) ===
    "energy_density_Wh_kg": MetricSpec(
        canonical_name="energy_density_Wh_kg",
        category="RATE",
        allowed_units={"Wh/kg", "Wh kg-1"},
        default_unit="Wh/kg",
        value_range=(1, 500),
        description="Gravimetric energy density"
    ),
    "power_density_W_kg": MetricSpec(
        canonical_name="power_density_W_kg",
        category="RATE",
        allowed_units={"W/kg", "W kg-1"},
        default_unit="W/kg",
        value_range=(10, 50000),
        description="Power density"
    ),
    
    # === CORROSION (Extended - 17_설계) ===
    "corrosion_current_density_mA_cm2": MetricSpec(
        canonical_name="corrosion_current_density_mA_cm2",
        category="CORROSION",
        allowed_units={"mA/cm2", "mA cm-2"},
        default_unit="mA/cm2",
        value_range=(0.001, 100),
        description="Corrosion current density in mA/cm2"
    ),
    
    # === OVERPOTENTIAL (Extended - 17_설계) ===
    "nucleation_overpotential_mV": MetricSpec(
        canonical_name="nucleation_overpotential_mV",
        category="OVERPOTENTIAL",
        allowed_units={"mV"},
        default_unit="mV",
        value_range=(0, 500),
        description="Nucleation overpotential"
    ),
    "deposition_overpotential_mV": MetricSpec(
        canonical_name="deposition_overpotential_mV",
        category="OVERPOTENTIAL",
        allowed_units={"mV"},
        default_unit="mV",
        value_range=(0, 300),
        description="Steady-state deposition overpotential"
    ),
    "voltage_hysteresis_mV": MetricSpec(
        canonical_name="voltage_hysteresis_mV",
        category="OVERPOTENTIAL",
        allowed_units={"mV"},
        default_unit="mV",
        value_range=(0, 500),
        description="Voltage hysteresis during cycling"
    ),
    
    # === KINETICS (NEW - 17_설계) ===
    "diffusion_coefficient_cm2_s": MetricSpec(
        canonical_name="diffusion_coefficient_cm2_s",
        category="KINETICS",
        allowed_units={"cm2/s", "cm2 s-1"},
        default_unit="cm2/s",
        value_range=(1e-15, 1e-6),
        description="Ion diffusion coefficient"
    ),
    "transference_number": MetricSpec(
        canonical_name="transference_number",
        category="KINETICS",
        allowed_units={"", None},
        default_unit="",
        value_range=(0, 1),
        description="Zn2+ transference number"
    ),
}


# =============================================================================
# AUTO-GENERATED CONSTANTS FROM CATALOG
# =============================================================================

# Valid metric names
VALID_METRICS: Set[str] = set(METRIC_CATALOG.keys())

# Alias mapping: alias -> canonical name
METRIC_ALIASES: Dict[str, str] = {}
for spec in METRIC_CATALOG.values():
    for alias in spec.aliases:
        METRIC_ALIASES[alias] = spec.canonical_name

# Range limits for QC
RANGES: Dict[str, Tuple[float, float]] = {
    name: spec.value_range 
    for name, spec in METRIC_CATALOG.items() 
    if spec.value_range
}

# Settings that should be injected as conditions, not stored as metrics
EIS_SETTINGS: Set[str] = {
    name for name, spec in METRIC_CATALOG.items() if spec.is_setting
}

# Category -> metric names mapping
CATEGORY_TO_METRICS: Dict[str, Set[str]] = {}
for name, spec in METRIC_CATALOG.items():
    CATEGORY_TO_METRICS.setdefault(spec.category, set()).add(name)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def canonicalize_metric(metric: str) -> str:
    """Convert metric name to canonical form using aliases."""
    if metric in VALID_METRICS:
        return metric
    return METRIC_ALIASES.get(metric, metric)


def is_valid_metric(metric: str) -> bool:
    """Check if metric is valid (known or has alias)."""
    return metric in VALID_METRICS or metric in METRIC_ALIASES


def get_metric_spec(metric: str) -> Optional[MetricSpec]:
    """Get MetricSpec for a metric (resolving aliases)."""
    canonical = canonicalize_metric(metric)
    return METRIC_CATALOG.get(canonical)


def is_eis_setting(metric: str) -> bool:
    """Check if metric is an EIS setting that should be a condition."""
    return metric in EIS_SETTINGS
