# scripts/lib/post_process/result_aligner.py
"""
Result Aligner v1.0

Algorithmic post-processor to fix "respectively" mapping errors.
LLMs often fail to map N-th value to N-th entity in a list.
This module parses the quote directly and enforces positional alignment.
"""
import re
import logging
from typing import List, Dict, Any, Tuple
import copy

logger = logging.getLogger(__name__)

# Patterns that trigger alignment
ALIGNMENT_TRIGGER_PATTERNS = [
    r"respectively",
    r"values were",
    r"increased from .* to",
    r"decreased from .* to",
    r"range of .* to",
]

# Entity patterns (should match case_builder patterns)
ENTITY_PATTERNS = [
    r"TpPa(?:@Zn)?",
    r"TpBD(?:@Zn)?",
    r"TpDATP(?:@Zn)?",
    r"bare\s*Zn(?: metal)?",
    r"Zn@C",
    r"G/Zn",
    r"PANI(?:/G/CC\|\|G/Zn)?",
]

def extract_entities_from_quote(quote: str) -> List[Tuple[int, str]]:
    """Find all entity mentions in quote with positions."""
    entities = []
    for pattern in ENTITY_PATTERNS:
        for match in re.finditer(pattern, quote, re.IGNORECASE):
            # Clean up entity name for matching
            name = match.group(0)
            # Normalize bare Zn
            if "bare" in name.lower():
                name = "bare Zn"
            elif "@" in name:
                name = name.split("@")[0] + "@Zn" # Standardization
                
            entities.append((match.start(), name))
            
    # Sort by position
    entities.sort(key=lambda x: x[0])
    
    # Dedup by position (avoid overlapping matches)
    unique_entities = []
    last_end = -1
    for start, name in entities:
        if start > last_end:
            unique_entities.append(name)
            last_end = start + len(name) # Approx
            
    return unique_entities

def extract_values_from_quote(quote: str) -> List[float]:
    """Find all numeric values in quote."""
    # Find numbers that look like data (integer or float, exclude dates/years if possible)
    # Matches: 123, 12.34, -12.34, 1.23e-4
    pattern = r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'
    
    values = []
    for match in re.finditer(pattern, quote):
        try:
            val = float(match.group(0))
            # Filter obvious specific years (2020-2030) or small integers if unlikely
            if 1990 < val < 2030 and val.is_integer():
                continue
            values.append(val)
        except ValueError:
            pass
            
    return values

def align_measurements(measurements: List[Dict]) -> List[Dict]:
    """
    Main function to align measurements based on quote structure.
    Target: S2095495624007885 type errors.
    """
    # Group by quote to process per-sentence
    by_quote = {}
    for m in measurements:
        quote = m.get("evidence", {}).get("quote", "")
        if not quote:
            continue
            
        # Check trigger
        if not any(re.search(p, quote, re.IGNORECASE) for p in ALIGNMENT_TRIGGER_PATTERNS):
            continue
            
        if len(quote) < 10: 
            continue
            
        by_quote.setdefault(quote, []).append(m)
        
    aligned_count = 0
    
    for quote, group in by_quote.items():
        # Only process if we have multiple measurements for this quote
        if len(group) < 2:
            continue
            
        # Extract lists from quote
        entities = extract_entities_from_quote(quote)
        values = extract_values_from_quote(quote)
        
        # If counts match or are close, try alignment
        if len(entities) >= 2 and len(values) >= 2:
            # Simple case: Entity list length == Value list length
            # Or if we have enough entities to cover the values
            
            # Sort measurements by value to help matching
            # (Assumes LLM got the value right at least)
            
            # Create a map of Value -> Entity (positional)
            # E.g. 1st value -> 1st entity
            
            # Use strict positional mapping
            min_len = min(len(entities), len(values))
            
            # Map: value_float -> proper_entity_name
            alignment_map = {}
            for i in range(min_len):
                alignment_map[values[i]] = entities[i]
                
            # Apply to measurements
            for m in group:
                val = m.get("value")
                if isinstance(val, (int, float)):
                    # Find closest value in our extracted list (handle float precision)
                    closest_val = None
                    min_diff = float('inf')
                    
                    for v in values:
                        diff = abs(v - val)
                        if diff < 0.001: # Tolerance
                            closest_val = v
                            break
                    
                    if closest_val is not None and closest_val in alignment_map:
                        target_entity = alignment_map[closest_val]
                        
                        # UPDATE THE MEASUREMENT
                        old_entity = m.get("conditions", {}).get("material_id", "Unknown")
                        
                        # Only update if different and target is valid
                        if target_entity and target_entity != old_entity:
                            m.setdefault("conditions", {})["material_id"] = target_entity
                            
                            # Update sample_type accordingly
                            if "bare" in target_entity.lower():
                                m.setdefault("tags", {})["sample_type"] = "BARE_ZN"
                            else:
                                m.setdefault("tags", {})["sample_type"] = "COATED"
                                
                            m["_aligned_by"] = "algorithmic_positional"
                            aligned_count += 1
                            logger.info(f"  Aligned: {val} -> {target_entity} (was {old_entity})")
                            
    if aligned_count > 0:
        logger.info(f"  [Result Aligner] Fixed {aligned_count} records using positional matching.")
        
    return measurements
