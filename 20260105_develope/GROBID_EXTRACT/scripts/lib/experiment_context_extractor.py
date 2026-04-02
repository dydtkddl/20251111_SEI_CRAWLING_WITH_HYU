# scripts/lib/experiment_context_extractor.py
"""
Experiment Context Extractor v1.0

Uses LLM to extract experiment_context for each measurement:
- experiment_type: CYCLING, EIS, RATE, etc.
- purpose: Why this experiment was done
- key_finding: Main conclusion
- improvement: Quantitative improvement vs control

This runs as a post-processing step after measurements are organized.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from scripts.lib.llm_client import call_llm_json, get_model_name
from scripts.lib.io_jsonl import read_jsonl

logger = logging.getLogger(__name__)


# ============================================================================
# PROMPT TEMPLATE
# ============================================================================

EXPERIMENT_CONTEXT_PROMPT = """
# SYSTEM: Battery Research Experiment Context Extractor

You are analyzing AZIB (Aqueous Zinc-Ion Battery) research papers to extract the PURPOSE and KEY FINDINGS for each experimental material/sample.

## EXTRACTION QUALITY GUIDELINES

### PURPOSE - Be Specific and Mechanism-Focused
❌ BAD: "Compare eis_Rct_Ohm between coated and bare Zn"
✅ GOOD: "Evaluate charge transfer kinetics improvement by graphene coating to suppress dendrite growth"

❌ BAD: "Test cycling performance"
✅ GOOD: "Assess long-term cycling stability and reversibility of Zn plating/stripping under high current density"

### KEY_FINDING - Include Mechanism and Numbers
❌ BAD: "Rct = 27Ω"
✅ GOOD: "Graphene coating reduced Rct by 83% (27Ω vs 162Ω) due to enhanced ion transport and reduced interfacial resistance"

❌ BAD: "Good cycling stability"
✅ GOOD: "G/Zn achieved 1000h stable cycling at 1 mA/cm² with only 45.3mV overpotential, attributed to uniform Zn deposition"

### IMPROVEMENT - Always Quantify
❌ BAD: "Better than bare Zn"
✅ GOOD: "8.3x longer cycle life (1000h vs 120h), 83% lower Rct, 62% reduced nucleation overpotential"

## EXPERIMENT TYPES
- CYCLING: Long-term cycling, capacity retention, coulombic efficiency
- EIS: Impedance spectroscopy (Rct, Rs, R0)
- RATE: Rate capability, specific capacity at different C-rates
- OVERPOTENTIAL: Nucleation/deposition overpotential, voltage hysteresis
- KINETICS: Ion diffusion, transference number, b-value
- CORROSION: Tafel analysis, corrosion current/potential
- DFT: Adsorption energy, binding energy calculations
- CV: Cyclic voltammetry, capacitive contribution

## EVIDENCE TEXT
{evidence_text}

## MATERIALS TO EXTRACT
{material_list}

## OUTPUT FORMAT (STRICT JSON)
```json
{{
  "experiment_contexts": [
    {{
      "material_id": "G/Zn",
      "experiment_type": "CYCLING",
      "purpose": "Evaluate long-term Zn plating/stripping reversibility with graphene protective layer",
      "key_finding": "G/Zn achieved 1000h stable cycling at 1mA/cm² with 45.3mV overpotential, showing uniform dendrite-free Zn deposition",
      "improvement": "8.3x longer cycle life than bare Zn (1000h vs 120h)"
    }},
    {{
      "material_id": "G/Zn",
      "experiment_type": "EIS",
      "purpose": "Analyze charge transfer kinetics improvement by graphene coating",
      "key_finding": "Graphene reduced Rct from 162Ω to 27Ω (83% decrease), indicating faster charge transfer at electrode-electrolyte interface",
      "improvement": "83% lower Rct compared to bare Zn"
    }},
    {{
      "material_id": "bare Zn",
      "experiment_type": "CYCLING",
      "purpose": "Establish baseline performance for uncoated zinc anode (control)",
      "key_finding": "Bare Zn failed after 120h due to dendrite-induced short circuit and accumulated dead Zn",
      "improvement": null
    }}
  ]
}}
```

## RULES
1. Extract BOTH treated (coated) and control (bare Zn) samples
2. Be SPECIFIC about mechanisms (dendrite suppression, ion transport, SEI formation)
3. Include NUMBERS in key_finding (%, mV, Ω, h, cycles)
4. Improvement is ONLY for treated samples comparing to control
5. One entry per material × experiment_type combination

Return ONLY valid JSON starting with {{ character. No markdown, no explanation.
"""


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def extract_unique_materials(measurements: List[Dict]) -> List[str]:
    """Extract unique material_ids from measurements."""
    materials = set()
    for m in measurements:
        mat_id = m.get("conditions", {}).get("material_id")
        if mat_id:
            materials.add(mat_id)
        
        # Also check tags for sample_type
        sample_type = m.get("tags", {}).get("sample_type")
        if sample_type:
            materials.add(sample_type)
    
    return sorted(materials)


def extract_evidence_texts(measurements: List[Dict]) -> str:
    """Extract unique evidence quotes from measurements."""
    quotes = []
    seen = set()
    
    for m in measurements:
        evidence = m.get("evidence", {})
        quote = evidence.get("quote", "")
        section = evidence.get("section_path", "")
        
        if quote and quote not in seen:
            seen.add(quote)
            quotes.append(f"[{section}] {quote}")
        
        # Also check context_paragraph if available
        ctx_para = m.get("context_paragraph", "")
        if ctx_para and ctx_para not in seen and len(ctx_para) > len(quote):
            seen.add(ctx_para)
            quotes.append(f"[{section}] {ctx_para}")
    
    return "\n\n".join(quotes[:30])  # Limit to avoid token overflow


def infer_experiment_type(metric: str) -> str:
    """Infer experiment type from metric name."""
    metric_lower = metric.lower()
    
    if "cycle" in metric_lower or "retention" in metric_lower or "coulombic" in metric_lower:
        return "CYCLING"
    elif "eis" in metric_lower or "rct" in metric_lower or "rs_" in metric_lower:
        return "EIS"
    elif "capacity" in metric_lower and "rate" not in metric_lower:
        return "RATE"
    elif "overpotential" in metric_lower or "nucleation" in metric_lower:
        return "OVERPOTENTIAL"
    elif "diffusion" in metric_lower or "transference" in metric_lower or "b_value" in metric_lower:
        return "KINETICS"
    elif "corrosion" in metric_lower:
        return "CORROSION"
    elif "adsorption" in metric_lower or "binding" in metric_lower:
        return "DFT"
    elif "contact_angle" in metric_lower:
        return "CONTACT_ANGLE"
    elif "thickness" in metric_lower or "layer" in metric_lower:
        return "INPUT"
    
    return "OTHER"


# ============================================================================
# MAIN EXTRACTION FUNCTION
# ============================================================================

def extract_experiment_contexts_llm(
    measurements: List[Dict],
    model: str = "gemini-2.5-flash"
) -> List[Dict]:
    """
    Use LLM to extract experiment_context for each unique material.
    
    Args:
        measurements: List of measurement dicts
        model: LLM model to use
        
    Returns:
        List of experiment_context dicts
    """
    if not measurements:
        return []
    
    # Extract unique materials and evidence
    materials = extract_unique_materials(measurements)
    evidence_text = extract_evidence_texts(measurements)
    
    if not materials or not evidence_text:
        return []
    
    # Build prompt
    prompt = EXPERIMENT_CONTEXT_PROMPT.format(
        evidence_text=evidence_text,
        material_list=", ".join(materials)
    )
    
    try:
        result = call_llm_json(
            prompt=prompt,
            model_name=model,
            task="experiment_context"
        )
        
        if result and "experiment_contexts" in result:
            return result["experiment_contexts"]
        
    except Exception as e:
        logger.warning(f"LLM experiment_context extraction failed: {e}")
    
    return []


def merge_contexts_to_measurements(
    measurements: List[Dict],
    contexts: List[Dict]
) -> List[Dict]:
    """
    Merge extracted experiment_contexts into measurements.
    
    Matches by material_id and inferred experiment_type.
    """
    # Build context lookup: (material_id, experiment_type) -> context
    context_lookup = {}
    for ctx in contexts:
        mat_id = ctx.get("material_id", "").lower()
        exp_type = ctx.get("experiment_type", "").upper()
        key = (mat_id, exp_type)
        context_lookup[key] = ctx
    
    # Match and merge
    for m in measurements:
        mat_id = m.get("conditions", {}).get("material_id", "")
        sample_type = m.get("tags", {}).get("sample_type", "")
        metric = m.get("metric", "")
        exp_type = infer_experiment_type(metric)
        
        # Try material_id first
        ctx = None
        if mat_id:
            ctx = context_lookup.get((mat_id.lower(), exp_type))
        
        # Fallback to sample_type
        if not ctx and sample_type:
            ctx = context_lookup.get((sample_type.lower(), exp_type))
        
        # Apply context if found
        if ctx:
            m["experiment_context"] = {
                "experiment_type": ctx.get("experiment_type"),
                "purpose": ctx.get("purpose"),
                "key_finding": ctx.get("key_finding"),
                "improvement": ctx.get("improvement"),
            }
    
    return measurements


# ============================================================================
# RULE-BASED FALLBACK
# ============================================================================

def generate_context_from_comparison(m: Dict) -> Optional[Dict]:
    """
    Generate experiment_context from comparison_group if LLM fails.
    """
    cmp_group = m.get("comparison_group", {})
    if not cmp_group:
        return None
    
    role = cmp_group.get("role", "")
    paired = cmp_group.get("paired_with", [])
    metric = m.get("metric", "")
    value = m.get("value")
    
    if not paired:
        return None
    
    # Find the comparison partner
    for pair in paired:
        if pair.get("relationship") == "COATED_VS_BARE":
            other_value = pair.get("value")
            
            if value is not None and other_value is not None:
                try:
                    val = float(value)
                    other = float(other_value)
                    
                    if other != 0:
                        pct_change = ((other - val) / other) * 100
                        
                        if role == "TREATED":
                            direction = "lower" if pct_change > 0 else "higher"
                            improvement = f"{abs(pct_change):.1f}% {direction} than control"
                        else:
                            improvement = None
                        
                        return {
                            "experiment_type": infer_experiment_type(metric),
                            "purpose": f"Compare {metric} between coated and bare Zn",
                            "key_finding": f"{metric} = {value} ({role})",
                            "improvement": improvement,
                        }
                except (ValueError, TypeError):
                    pass
    
    return None


def add_experiment_context_fallback(measurements: List[Dict]) -> List[Dict]:
    """
    Add experiment_context using rule-based fallback for measurements without context.
    """
    for m in measurements:
        if not m.get("experiment_context"):
            ctx = generate_context_from_comparison(m)
            if ctx:
                m["experiment_context"] = ctx
    
    return measurements


# ============================================================================
# PUBLIC API
# ============================================================================

def extract_and_merge_experiment_context(
    measurements: List[Dict],
    use_llm: bool = True,
    model: str = "gemini-2.5-flash"
) -> List[Dict]:
    """
    Main function to extract and merge experiment_context.
    
    1. Tries LLM extraction first
    2. Falls back to rule-based extraction from comparison_group
    
    Args:
        measurements: List of measurement dicts
        use_llm: Whether to use LLM (default True)
        model: LLM model to use
        
    Returns:
        Measurements with experiment_context added
    """
    if use_llm:
        contexts = extract_experiment_contexts_llm(measurements, model)
        if contexts:
            measurements = merge_contexts_to_measurements(measurements, contexts)
            logger.info(f"  LLM added experiment_context for {len(contexts)} entries")
    
    # Apply fallback for remaining
    measurements = add_experiment_context_fallback(measurements)
    
    # Count how many have context
    ctx_count = sum(1 for m in measurements if m.get("experiment_context"))
    logger.info(f"  Total measurements with experiment_context: {ctx_count}/{len(measurements)}")
    
    return measurements
