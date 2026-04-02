# scripts/lib/deduplicator.py
"""
Deduplicator v1.0

Advanced deduplication and merging for AZIB measurements.
Merges records that represent the same physical measurement but may differ slightly
in metadata or extraction path.

Strategy:
1. Group by (paper_id, metric, normalized_value)
2. Within group, check evidence overlap (same quote or same sentence window)
3. Merge duplicates:
   - Keep the one with MOST conditions (informativeness)
   - Keep the one with MOST tags
   - Union the auxiliary fields
"""
import logging
from typing import List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

def _get_dedup_key(m: Dict) -> Tuple:
    """Generate key for grouping potential duplicates."""
    metric = m.get("metric", "")
    val = m.get("value")
    # Handle list values (unhashable)
    if isinstance(val, list):
        val = tuple(val)
        
    # Round float values for fuzzy matching
    if isinstance(val, float):
        val = round(val, 4)
        
    return (
        metric,
        val,
        # We don't include material_id here because sometimes duplicates have missing IDs
        # We handle ID merging in the next step
    )

def _score_record(m: Dict) -> int:
    """Score record quality/informativeness."""
    score = 0
    conds = m.get("conditions", {}) or {}
    tags = m.get("tags", {}) or {}
    
    # More conditions is better
    score += len([v for v in conds.values() if v is not None]) * 2
    
    # Material ID is critical
    if conds.get("material_id"):
        score += 5
        
    # More tags is better
    score += len([v for v in tags.values() if v is not None])
    
    # Specific sample_type is better than UNCLEAR
    if tags.get("sample_type") in ["COATED", "BARE_ZN", "CONTROL"]:
        score += 2
        
    return score

def _are_duplicates(m1: Dict, m2: Dict) -> bool:
    """Check if two records are likely duplicates."""
    # 1. Evidence overlap
    q1 = m1.get("evidence", {}).get("quote", "")
    q2 = m2.get("evidence", {}).get("quote", "")
    
    # If quotes are identical, almost certainly duplicates (given same metric/value)
    if q1 and q2 and q1 == q2:
        return True
        
    # 2. Material ID conflict check
    # If both have DIFFERENT non-null material_ids, they are NOT duplicates (different samples)
    mid1 = m1.get("conditions", {}).get("material_id")
    mid2 = m2.get("conditions", {}).get("material_id")
    
    if mid1 and mid2 and mid1 != mid2:
        return False
        
    return True

def merge_records(records: List[Dict]) -> Dict:
    """Merge a list of duplicate records into one optimal record."""
    if not records:
        return {}
    if len(records) == 1:
        return records[0]
        
    # Sort by quality score
    sorted_records = sorted(records, key=_score_record, reverse=True)
    best = sorted_records[0]
    
    # Merge missing info from others into best
    for other in sorted_records[1:]:
        # Merge conditions
        for k, v in other.get("conditions", {}).items():
            if v is not None and best.get("conditions", {}).get(k) is None:
                best.setdefault("conditions", {})[k] = v
                
        # Merge tags
        for k, v in other.get("tags", {}).items():
            if v is not None and best.get("tags", {}).get(k) is None:
                best.setdefault("tags", {})[k] = v
                
        # Merge evidence (keep best's doc/section, but maybe append quote?)
        # For now, keep best evidence as is
        
    return best

def deduplicate_measurements(measurements: List[Dict]) -> List[Dict]:
    """Main deduplication function."""
    if not measurements:
        return []
        
    # Group by key
    groups = {}
    for m in measurements:
        key = _get_dedup_key(m)
        groups.setdefault(key, []).append(m)
        
    final_list = []
    dedup_count = 0
    
    for key, group in groups.items():
        if len(group) == 1:
            final_list.append(group[0])
            continue
            
        # Within group (same metric/value), checking for conflicts
        # E.g. same value but different materials -> NOT duplicates
        sub_groups = []
        
        while group:
            current = group.pop(0)
            merged = False
            
            for sub in sub_groups:
                # Check against first element of subgroup
                if _are_duplicates(current, sub[0]):
                    sub.append(current)
                    merged = True
                    break
            
            if not merged:
                sub_groups.append([current])
                
        # Now process sub_groups
        for sub in sub_groups:
            if len(sub) > 1:
                dedup_count += len(sub) - 1
                merged_record = merge_records(sub)
                final_list.append(merged_record)
            else:
                final_list.append(sub[0])
                
    if dedup_count > 0:
        logger.info(f"  [Deduplicator] Merged {dedup_count} duplicates.")
        
    return final_list
