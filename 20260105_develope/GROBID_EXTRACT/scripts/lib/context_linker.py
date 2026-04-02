# scripts/lib/context_linker.py
"""
Enterprise Context Linker: Connect fragmented sentences and clues across chunks.

This module implements cross-reference detection and context aggregation
to help LLM extractors understand relationships between scattered evidence.
"""
from __future__ import annotations
import re
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

from scripts.lib.io_jsonl import read_jsonl
from scripts.lib.retrieval import tokenize, build_bm25, bm25_score


# ============================================================================
# Reference Pattern Detection
# ============================================================================
FIGURE_REF_PATTERN = re.compile(
    r"(?:Fig(?:ure)?\.?\s*|Figs?\.\s*)(\d+[a-z]?(?:\s*[-–]\s*[a-z])?(?:\s*,\s*\d+[a-z]?)*)",
    re.IGNORECASE
)
TABLE_REF_PATTERN = re.compile(
    r"(?:Table\.?\s*)(\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+)*)",
    re.IGNORECASE
)
SECTION_REF_PATTERN = re.compile(
    r"(?:Section\s*|Supporting\s+Information\s*|SI\s*|ESI\s*)(\S+)?",
    re.IGNORECASE
)

# Material/Sample reference patterns (e.g., "Zn@GO", "the coated electrode")
SAMPLE_REF_PATTERNS = [
    re.compile(r"Zn@\w+", re.IGNORECASE),
    re.compile(r"(?:the\s+)?(?:modified|coated|protected|bare)\s+(?:Zn|zinc|anode|electrode)", re.IGNORECASE),
    re.compile(r"(?:sample|specimen)\s*[A-Z]?\d?", re.IGNORECASE),
]


def extract_references(text: str) -> Dict[str, List[str]]:
    """Extract all references from text."""
    refs = {
        "figures": [],
        "tables": [],
        "sections": [],
        "samples": []
    }
    
    for m in FIGURE_REF_PATTERN.finditer(text):
        refs["figures"].append(m.group(1))
    for m in TABLE_REF_PATTERN.finditer(text):
        refs["tables"].append(m.group(1))
    for m in SECTION_REF_PATTERN.finditer(text):
        refs["sections"].append(m.group(0))
    for pattern in SAMPLE_REF_PATTERNS:
        for m in pattern.finditer(text):
            refs["samples"].append(m.group(0))
    
    return refs


# ============================================================================
# Cross-Reference Link Builder
# ============================================================================
def build_cross_references(
    chunks: List[Dict[str, Any]],
    labels_map: Dict[str, List[str]],
    inventory: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Build cross-reference links between chunks.
    
    Returns list of link objects:
    {
        "source_chunk_id": str,
        "target_chunk_id": str,
        "link_type": str,
        "evidence": str,
        "confidence": float
    }
    """
    links = []
    
    # Index chunks by their references
    chunks_by_figure: Dict[str, List[str]] = defaultdict(list)
    chunks_by_table: Dict[str, List[str]] = defaultdict(list)
    chunks_by_sample: Dict[str, List[str]] = defaultdict(list)
    
    for ch in chunks:
        cid = ch.get("chunk_id")
        text = ch.get("text", "")
        refs = extract_references(text)
        
        for fig in refs["figures"]:
            chunks_by_figure[fig.lower()].append(cid)
        for tbl in refs["tables"]:
            chunks_by_table[tbl.lower()].append(cid)
        for sample in refs["samples"]:
            chunks_by_sample[sample.lower()].append(cid)
    
    # Link chunks that reference the same figure
    for fig, chunk_ids in chunks_by_figure.items():
        if len(chunk_ids) > 1:
            for i, src in enumerate(chunk_ids):
                for tgt in chunk_ids[i+1:]:
                    links.append({
                        "source_chunk_id": src,
                        "target_chunk_id": tgt,
                        "link_type": "FIGURE_REF",
                        "evidence": f"Both reference Figure {fig}",
                        "confidence": 0.8
                    })
    
    # Link chunks that reference the same table
    for tbl, chunk_ids in chunks_by_table.items():
        if len(chunk_ids) > 1:
            for i, src in enumerate(chunk_ids):
                for tgt in chunk_ids[i+1:]:
                    links.append({
                        "source_chunk_id": src,
                        "target_chunk_id": tgt,
                        "link_type": "TABLE_REF",
                        "evidence": f"Both reference Table {tbl}",
                        "confidence": 0.8
                    })
    
    # Link chunks with same sample references
    for sample, chunk_ids in chunks_by_sample.items():
        if len(chunk_ids) > 1:
            for i, src in enumerate(chunk_ids):
                for tgt in chunk_ids[i+1:]:
                    links.append({
                        "source_chunk_id": src,
                        "target_chunk_id": tgt,
                        "link_type": "SAME_CASE",
                        "evidence": f"Both mention '{sample}'",
                        "confidence": 0.6
                    })
    
    # Link Methods to Results (by label)
    method_chunks = [c["chunk_id"] for c in chunks if "method" in (c.get("section_path") or "").lower()]
    result_chunks = [c["chunk_id"] for c in chunks if "result" in (c.get("section_path") or "").lower()]
    
    for method_cid in method_chunks[:3]:  # Limit to avoid explosion
        for result_cid in result_chunks[:5]:
            links.append({
                "source_chunk_id": method_cid,
                "target_chunk_id": result_cid,
                "link_type": "RESULT_METHOD_LINK",
                "evidence": "Method-Result section relationship",
                "confidence": 0.5
            })
    
    return links


# ============================================================================
# Context Aggregator
# ============================================================================
def aggregate_context_for_extraction(
    chunks: List[Dict[str, Any]],
    case: Dict[str, Any],
    task_type: str,
    cross_refs: List[Dict[str, Any]],
    labels_map: Dict[str, List[str]],
    inventory: Dict[str, Any],
    max_tokens: int = 4000,
    topk_override: int = 0  # 19_설계 Phase 4: Allow fallback with more chunks
) -> Dict[str, Any]:
    """
    Aggregate all relevant context for extraction.
    
    This is the key function for "connecting fragmented sentences".
    It collects:
    1. Primary chunks (directly relevant)
    2. Linked chunks (via cross-references)
    3. Related captions and tables
    4. Condition context (current density, cell type from other chunks)
    
    Returns an enriched context packet.
    """
    from scripts.lib.evidence_packet import DEFAULT_KEYWORDS, TARGET_LABELS, rank_chunks
    
    # Ensure case is a dict
    if not isinstance(case, dict):
        case = {}
    
    keywords = DEFAULT_KEYWORDS.get(task_type, [])
    target_labels = TARGET_LABELS.get(task_type, [])
    
    # Build query from case metadata
    material = case.get("material_raw") or case.get("coating_label") or ""
    electrolyte = case.get("electrolyte_raw") or ""
    query = f"{task_type} {material} {electrolyte}"
    
    # Get primary ranked chunks
    # 19_설계 Phase 4: Use topk_override if provided (for fallback retries)
    topk = topk_override if topk_override > 0 else 8
    primary_ranked = rank_chunks(
        chunks, query=query, keywords=keywords,
        target_labels=target_labels, labels_map=labels_map, topk=topk
    )
    primary_ids = {ch["chunk_id"] for _, ch in primary_ranked}
    
    # 19_설계 Phase 4.5: MANDATORY section inclusion for specific extractors
    MANDATORY_SECTIONS = {
        "EXTRACT_RATE": ["battery performance", "rate performance", "ragone"],
        "EXTRACT_OVERPOTENTIAL": ["anode stability", "overpotential", "nucleation"],
        "EXTRACT_CYCLING": ["cycling", "stability", "long-term"],
    }
    mandatory_section_patterns = MANDATORY_SECTIONS.get(task_type, [])
    mandatory_chunks = []
    if mandatory_section_patterns:
        for ch in chunks:
            sp = (ch.get("section_path") or "").lower()
            if any(pat in sp for pat in mandatory_section_patterns):
                if ch["chunk_id"] not in primary_ids:
                    mandatory_chunks.append(ch)
                    primary_ids.add(ch["chunk_id"])
    
    # Get linked chunks via cross-references
    linked_ids: Set[str] = set()
    for link in cross_refs:
        if link["source_chunk_id"] in primary_ids:
            linked_ids.add(link["target_chunk_id"])
        if link["target_chunk_id"] in primary_ids:
            linked_ids.add(link["source_chunk_id"])
    
    # Fetch linked chunks
    linked_chunks = []
    for ch in chunks:
        if ch["chunk_id"] in linked_ids and ch["chunk_id"] not in primary_ids:
            linked_chunks.append(ch)
    
    # Build context sections
    primary_texts = []
    
    # Add mandatory section chunks first (highest priority)
    for ch in mandatory_chunks:
        primary_texts.append({
            "chunk_id": ch["chunk_id"],
            "doc": ch.get("doc", "MAIN"),
            "section_path": ch.get("section_path", ""),
            "text": ch.get("text", ""),
            "relevance_score": 999.0,  # Highest priority
            "labels": labels_map.get(ch["chunk_id"], []),
            "source": "MANDATORY_SECTION"
        })
    
    # Add BM25-ranked chunks
    for score, ch in primary_ranked:
        if ch["chunk_id"] not in [c["chunk_id"] for c in primary_texts]:  # Avoid duplicates
            primary_texts.append({
                "chunk_id": ch["chunk_id"],
                "doc": ch.get("doc", "MAIN"),
                "section_path": ch.get("section_path", ""),
                "text": ch.get("text", ""),
                "relevance_score": score,
                "labels": labels_map.get(ch["chunk_id"], [])
            })
    
    linked_texts = []
    for ch in linked_chunks[:4]:  # Limit linked context
        linked_texts.append({
            "chunk_id": ch["chunk_id"],
            "doc": ch.get("doc", "MAIN"),
            "section_path": ch.get("section_path", ""),
            "text": ch.get("text", ""),
            "link_reason": "Cross-referenced by primary chunks"
        })
    
    # Get relevant captions
    captions = []
    for fig in inventory.get("figures", []):
        cap = (fig.get("caption") or "").lower()
        if any(k.lower() in cap for k in keywords):
            captions.append({
                "type": "figure",
                "id": fig.get("figure_id"),
                "caption": fig.get("caption"),
                "doc": fig.get("doc", "MAIN")
            })
    for tbl in inventory.get("tables", []):
        cap = (tbl.get("caption") or "").lower()
        if any(k.lower() in cap for k in keywords):
            captions.append({
                "type": "table",
                "id": tbl.get("table_id"),
                "caption": tbl.get("caption"),
                "doc": tbl.get("doc", "MAIN"),
                "parsed": tbl.get("parsed")
            })
    
    # Extract condition hints from ALL chunks (not just primary)
    condition_hints = extract_condition_hints(chunks, case)
    
    return {
        "paper_id": case.get("paper_id"),
        "case_id": case.get("case_id") or case.get("case_id_hint"),
        "task_type": task_type,
        "case_metadata": {
            "coating_label": case.get("coating_label"),
            "material_raw": case.get("material_raw"),
            "thickness_um": case.get("protective_layer_thickness_um"),
            "electrolyte_raw": case.get("electrolyte_raw"),
            "cell_type": case.get("cell_type"),
            "areal_capacity_mAhcm2": case.get("areal_capacity_mAhcm2"),
            "areal_current_density_mAcm2": case.get("areal_current_density_mAcm2"),
        },
        "primary_evidence": primary_texts,
        "linked_evidence": linked_texts,
        "relevant_captions": captions[:10],
        "condition_hints": condition_hints,
        "cross_reference_summary": f"{len(cross_refs)} links detected in paper"
    }


def extract_condition_hints(chunks: List[Dict[str, Any]], case: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scan all chunks for experimental conditions that might be mentioned
    separately from the main values.
    """
    hints = {
        "current_densities_mentioned": [],
        "capacities_mentioned": [],
        "temperatures_mentioned": [],
        "electrolytes_mentioned": [],
        "cell_types_mentioned": []
    }
    
    current_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mA\s*(?:cm|/cm))", re.IGNORECASE)
    capacity_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mAh\s*(?:cm|/cm))", re.IGNORECASE)
    temp_pattern = re.compile(r"(\d+)\s*°?\s*C(?:elsius)?", re.IGNORECASE)
    
    for ch in chunks:
        text = ch.get("text", "")
        
        for m in current_pattern.finditer(text):
            hints["current_densities_mentioned"].append(float(m.group(1)))
        for m in capacity_pattern.finditer(text):
            hints["capacities_mentioned"].append(float(m.group(1)))
        for m in temp_pattern.finditer(text):
            hints["temperatures_mentioned"].append(int(m.group(1)))
        
        if re.search(r"symmetric\s+cell", text, re.IGNORECASE):
            hints["cell_types_mentioned"].append("SYMMETRIC")
        if re.search(r"full\s+cell", text, re.IGNORECASE):
            hints["cell_types_mentioned"].append("FULL_CELL")
    
    # Deduplicate
    for k in hints:
        hints[k] = list(set(hints[k]))[:5]
    
    return hints
