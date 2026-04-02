# scripts/lib/contracts.py
# -*- coding: utf-8 -*-
"""
Stage4 Output Contract Definitions

Defines strict schema contracts that LLM extractors must follow.
Used for prompt injection and validation.
"""

# === PATCH 1: Stage4 강제 계약 문자열 ===

MEASUREMENT_SCHEMA_CONTRACT = r"""
You MUST output a JSON object with key "measurements": [ ... ].

Each measurement item MUST be a JSON object with:
- metric: string
- value: number | string | list[number] | null
- unit: string | null
- confidence: number (0..1)
- conditions: object (MUST exist; use {} if none)
- tags: object (MUST exist; use {} if none)
- evidence: object (MUST exist)

Evidence contract (MUST):
- doc: "MAIN" or "SUPP"
- section_path: non-empty string
- and at least ONE of:
  - quote: short snippet (<= 25 words, >= 20 chars)
  - chunk_id: "C-M-xxxxx" / "C-S-xxxxx"
  - figure_id or table_id (but if using only figure/table, include quote OR chunk_id too)

Null value rule:
- If value is null, tags MUST include:
  - "value_status": "FIGURE_DIGITIZE_REQUIRED" or "NOT_FOUND"
- And evidence MUST reference figure_id/table_id or chunk_id that explains why.

Do NOT omit conditions/tags/evidence. Never output null for them.
Return ONLY valid JSON.
"""

# === Allowed enums ===

ALLOWED_EVIDENCE_DOC = {"MAIN", "SUPP"}
ALLOWED_VALUE_STATUS = {"FIGURE_DIGITIZE_REQUIRED", "NOT_FOUND"}

# === Condition type slots (prevents unit confusion) ===

CONDITION_SLOTS = {
    # Environmental
    "temperature_C": float,
    "electrolyte": str,
    
    # Current/Rate (use exactly ONE per measurement)
    "areal_current_density_mA_cm2": float,
    "specific_current_A_g": float,
    "rate_C": float,
    
    # Capacity
    "areal_capacity_mAh_cm2": float,
    "specific_capacity_mAh_g": float,
    "cycle_window_h": float,
    
    # EIS-specific
    "frequency_range_Hz": list,  # [min, max]
    "applied_potential_V": float,
    "ac_amplitude_V": float,
    
    # General
    "cutoff_voltage_V": list,  # [low, high]
}

# Current type slots for mutual exclusivity check
CURRENT_TYPE_SLOTS = [
    "areal_current_density_mA_cm2",
    "specific_current_A_g", 
    "rate_C"
]

# ===================================================================
# 08_설계: METRIC_REGISTRY - Per-metric required conditions/tags/units
# ===================================================================

METRIC_REGISTRY = {
    # === OVERPOTENTIAL ===
    "overpotential_mV": {
        "required_tags": {
            "overpotential_type": ["NUCLEATION", "DEPOSITION", "HYSTERESIS", "SERIES", "APPLIED", "UNCLEAR"]
        },
        "required_conditions": {
            "current_density_mA_cm2": "number|null",
            "material": "string|null"
        },
        "allowed_units": ["mV"],
        "value_range": [0, 2000]
    },
    
    # === EIS METRICS ===
    "eis_Rct_Ohm": {
        "required_tags": {
            "eis_metric_type": ["Rct"],
            "before_after": ["BEFORE_COATING", "AFTER_COATING", "UNCLEAR"]
        },
        "required_conditions": {
            "frequency_range_Hz": "array|null",
            "ac_amplitude_V": "number|null"
        },
        "allowed_units": ["Ohm", "Ω"]
    },
    "eis_Rs_Ohm": {
        "required_tags": {
            "eis_metric_type": ["Rs"],
            "before_after": ["BEFORE_COATING", "AFTER_COATING", "UNCLEAR"]
        },
        "required_conditions": {
            "frequency_range_Hz": "array|null",
            "ac_amplitude_V": "number|null"
        },
        "allowed_units": ["Ohm", "Ω"]
    },
    "eis_Rsei_Ohm": {
        "required_tags": {
            "eis_metric_type": ["Rsei"],
            "before_after": ["BEFORE_COATING", "AFTER_COATING", "UNCLEAR"]
        },
        "required_conditions": {
            "frequency_range_Hz": "array|null",
            "ac_amplitude_V": "number|null"
        },
        "allowed_units": ["Ohm", "Ω"]
    },
    "eis_R0_Ohm": {
        "required_tags": {
            "eis_metric_type": ["R0"],
            "before_after": ["BEFORE_COATING", "AFTER_COATING", "UNCLEAR"]
        },
        "required_conditions": {
            "frequency_range_Hz": "array|null",
            "ac_amplitude_V": "number|null"
        },
        "allowed_units": ["Ohm", "Ω"]
    },
    
    # === CYCLING ===
    "galvanostatic_cycling_cycles": {
        "required_conditions": {
            "current_density_value": "number|null",
            "current_density_unit": "string|null",  # 'A g-1' vs 'mA cm-2'
            "temperature_C": "number|null"
        },
        "required_tags": {},
        "allowed_units": ["cycles"]
    },
    "galvanostatic_cycling_performance_h": {
        "required_conditions": {
            "current_density_value": "number|null",
            "current_density_unit": "string|null",
            "areal_capacity_mAh_cm2": "number|null"
        },
        "required_tags": {},
        "allowed_units": ["h"]
    },
    "capacity_retention_pct": {
        "required_conditions": {
            "cycle_number": "number|null",
            "current_density_value": "number|null",
            "current_density_unit": "string|null"
        },
        "required_tags": {},
        "allowed_units": ["%"],
        "value_range": [0, 100]
    },
    
    # === INPUT METRICS ===
    "ion_conductivity_mS_cm": {
        "required_tags": {
            "ionic_conductivity_scope": ["COATING", "ELECTROLYTE", "UNCLEAR"]
        },
        "required_conditions": {
            "temperature_C": "number|null"
        },
        "allowed_units": ["mS/cm"]
    },
    "protective_layer_thickness_um": {
        "required_conditions": {},
        "required_tags": {},
        "allowed_units": ["um", "μm"]
    },
    "zn_adsorption_energy_eV": {
        "required_tags": {
            "zn_adsorption_source": ["DFT", "EXPERIMENT", "UNCLEAR"]
        },
        "required_conditions": {},
        "allowed_units": ["eV"]
    },
    
    # === CORROSION ===
    "corrosion_current_density_uAcm2": {
        "required_conditions": {
            "reference_electrode": "string|null"
        },
        "required_tags": {
            "sample_type": ["COATED", "BARE_ZN", "CONTROL", "UNCLEAR"]
        },
        "allowed_units": ["uA/cm2", "μA/cm²"]
    },
    "corrosion_potential_V": {
        "required_conditions": {
            "reference_electrode": "string|null"
        },
        "required_tags": {
            "sample_type": ["COATED", "BARE_ZN", "CONTROL", "UNCLEAR"]
        },
        "allowed_units": ["V"]
    }
}

# ===================================================================
# 08_설계: BAD_QUOTE_PATTERNS - Quote quality override triggers
# ===================================================================

BAD_QUOTE_PATTERNS = [
    r"^\s*fig",                      # starts with "fig"
    r"^\s*\d+[a-z]?\.\s*$",          # just "4a." or "3."
    r"value extracted",              # "Value extracted from..."
    r"^4a\.$",                       # exact "4a."
    r"^7 fig",                       # "7 Fig..."
    r"^\s*table\s*\d",               # "Table 1"
    r"^\s*\d+\s*$",                  # just a number
]

# ===================================================================
# 10_설계: Contract v1.1 - KEY_ALIAS for canonical condition keys
# ===================================================================

# Alias -> Canonical mapping for condition keys
KEY_ALIAS = {
    # Current density aliases (all map to areal_current_density_mA_cm2)
    "current_density_mA_cm2": "areal_current_density_mA_cm2",
    "current_density_mAcm2": "areal_current_density_mA_cm2",
    "areal_current_density_mAcm2": "areal_current_density_mA_cm2",
    "current_density_mAcm-2": "areal_current_density_mA_cm2",
    
    # Capacity aliases
    "areal_capacity_mAhcm2": "areal_capacity_mAh_cm2",
    "areal_capacity_mAhcm-2": "areal_capacity_mAh_cm2",
    
    # Temperature aliases
    "temperature": "temperature_C",
    "temp": "temperature_C",
    "temp_C": "temperature_C",
    
    # Current density value/unit legacy format
    "current_density": "current_density_value",
}

# Extended value_status enum for v1.1
ALLOWED_VALUE_STATUS_V11 = {
    "FIGURE_DIGITIZE_REQUIRED",  # Value only in plot, needs digitization
    "NOT_FOUND",                  # Value not found in text/table/figure
    "OK_UNIT_UNCLEAR",            # Value found but unit is unclear
    "OK_CONTEXT_UNCLEAR",         # Value found but context is unclear
}

# Bad quote prefixes for action-oriented validation hints
BAD_QUOTE_PREFIXES = (
    "Value extracted",
    "Extracted from fig",
    "Value from figure",
    "From Table",
    "See Fig",
)

