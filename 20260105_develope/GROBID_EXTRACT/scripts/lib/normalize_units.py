# scripts/lib/normalize_units.py
"""Unit normalization for extracted measurements."""
from __future__ import annotations
from typing import Any, Dict, List


def _to_float(x):
    """Safely convert to float."""
    try:
        return float(x)
    except Exception:
        return None


# Mapping for metric name normalization
# NOTE: Never map volumetric (Wh/L) to gravimetric (Wh/kg) - changes physical meaning!
METRIC_NAME_MAP = {
    "capacity_retention_percentage": "capacity_retention_pct",
    "specific_energy": "energy_density_Wh_kg",
    # SAFE: Preserve volumetric metrics with their correct names
    "energy_density_wh_l": "energy_density_Wh_L",   # Volumetric - DO NOT convert to kg
    "power_density_w_l": "power_density_W_L",       # Volumetric - DO NOT convert to kg
    "voltage_hysteresis": "overpotential_mV",       # Normalized to overpotential with tag
}

# Standard units for each metric
METRIC_UNIT_MAP = {
    "energy_density_Wh_L": "Wh/L",
    "power_density_W_L": "W/L",
    "energy_density_Wh_kg": "Wh/kg",
    "power_density_W_kg": "W/kg",
}


def normalize_one(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize units for a single measurement.
    
    Conversions:
    - ionic conductivity: µS/cm -> mS/cm
    - thickness: various µm notations -> um
    - all metrics get standardized unit strings
    """
    mm = dict(m)
    orig_metric = mm.get("metric") or ""
    
    # Handle pipe-delimited metrics (common LLM artifact)
    if "|" in orig_metric:
        orig_metric = orig_metric.split("|")[0].strip()
        mm["metric"] = orig_metric

    # Normalize metric name mapping
    if orig_metric in METRIC_NAME_MAP:
        mm["metric"] = METRIC_NAME_MAP[orig_metric]
    
    # Reslotting based on units to fix LLM confusion
    unit = (mm.get("unit") or "").strip()
    current_metric = mm.get("metric")
    
    if current_metric == "galvanostatic_cycling_performance_h" and unit.lower().strip() == "cycles":
        mm["metric"] = "galvanostatic_cycling_cycles"
    elif current_metric == "galvanostatic_cycling_cycles" and ("h" in unit.lower() or "hour" in unit.lower()):
        mm["metric"] = "galvanostatic_cycling_performance_h"
    
    metric = mm.get("metric")
    val = mm.get("value")
    unit = (mm.get("unit") or "").strip()

    fv = _to_float(val)

    # ionic conductivity: µS/cm -> mS/cm
    if metric == "ion_conductivity_mS_cm" and fv is not None:
        u = unit.lower().replace(" ", "")
        if "µs/cm" in u or "μs/cm" in u or "us/cm" in u:
            mm["value"] = fv / 1000.0
            mm["unit"] = "mS/cm"
        elif "ms/cm" in u or unit == "" or unit.lower() == "ms/cm":
            mm["value"] = fv
            mm["unit"] = "mS/cm"

    # thickness: um / µm / ㎛ -> um
    if metric == "protective_layer_thickness_um" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "um"

    # areal capacity
    if metric == "areal_capacity_mAhcm2" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "mAh/cm2"
    
    # areal current density
    if metric == "areal_current_density_mAcm2" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "mA/cm2"

    # corrosion current density
    if metric == "corrosion_current_density_uAcm2" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "uA/cm2"

    # corrosion potential
    if metric == "corrosion_potential_V" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "V"

    # EIS metrics
    if metric in ("eis_Rs_Ohm", "eis_Rct_Ohm", "eis_Rsei_Ohm", "electrochemical_impedance_Ohm") and fv is not None:
        mm["value"] = fv
        mm["unit"] = "Ohm"

    # overpotential
    if metric == "overpotential_mV" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "mV"

    # cycling performance
    if metric == "galvanostatic_cycling_performance_h" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "h"

    # contact angle
    if metric == "contact_angle_deg" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "deg"

    # specific capacity
    if metric == "specific_capacity_mAh_g" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "mAh/g"

    # energy density (Wh/kg)
    if metric == "energy_density_Wh_kg" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "Wh/kg"

    # power density (W/kg)
    if metric == "power_density_W_kg" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "W/kg"

    # Volumetric energy density (Wh/L) - preserve basis
    if metric == "energy_density_Wh_L" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "Wh/L"
        tags = dict(mm.get("tags") or {})
        tags["basis"] = "VOLUMETRIC"
        mm["tags"] = tags

    # Volumetric power density (W/L) - preserve basis
    if metric == "power_density_W_L" and fv is not None:
        mm["value"] = fv
        mm["unit"] = "W/L"
        tags = dict(mm.get("tags") or {})
        tags["basis"] = "VOLUMETRIC"
        mm["tags"] = tags

    return mm


from scripts.lib.post_process.result_aligner import align_measurements

def normalize_measurements(measurements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize units for all measurements with algorithmic alignment."""
    # Step 1: Normalize units individually
    normalized = [normalize_one(m) for m in measurements]
    normalized = [m for m in normalized if m] # filter None
    
    # Step 2: Algorithmic alignment (Fix respectively patterns)
    try:
        aligned = align_measurements(normalized)
        return aligned
    except Exception as e:
        # Fallback if alignment fails
        print(f"Warning: Alignment failed: {e}")
        return normalized
