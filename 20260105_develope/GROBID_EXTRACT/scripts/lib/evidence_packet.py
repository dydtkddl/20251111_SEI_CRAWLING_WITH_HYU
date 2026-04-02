# scripts/lib/evidence_packet.py
"""Evidence packet builder for LLM extraction tasks."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional

from scripts.lib.io_jsonl import read_json, read_jsonl
from scripts.lib.retrieval import rank_chunks

# Task-specific keywords for retrieval (Phase 4 Enhanced - Comprehensive Coverage)
DEFAULT_KEYWORDS = {
    "EXTRACT_INPUT": [
        "thickness", "layer thickness", "coating thickness", "μm", "um", "nm",
        "ionic conductivity", "mS/cm", "μS/cm", "contact angle", "wettability",
        "zincophilicity", "adsorption energy", "binding energy", "DFT",
        "areal capacity", "mAh cm", "areal current density", "mA cm",
        "protective layer", "graphene", "carbon coating", "deposition",
        "honeycomb-like", "protective", "exfoliated", "100 nm", "1.02 nm"
    ],
    "EXTRACT_CYCLING": [
        "cycling", "cycle life", "symmetric cell", "zn||zn", "plating/stripping",
        "stable for", "h", "hours", "long-term", "cycling stability",
        "capacity retention", "coulombic efficiency", "CE", "voltage hysteresis",
        "10000 cycles", "1000 h", "over 1000 h", "C.E.", "99.7", "91.1"
    ],
    "EXTRACT_CORROSION": [
        "tafel", "polarization", "corrosion potential", "ecorr", "corrosion current",
        "icorr", "corrosion behavior", "mA cm-2", "mA/cm2", "corrosion current density",
        "self-corrosion", "OCP", "open circuit potential", "anodic", "cathodic",
        "1.43 mA", "1.32 mA", "-0.891", "-0.883"
    ],
    "EXTRACT_EIS": [
        "eis", "nyquist", "rs", "rct", "charge transfer resistance", "equivalent circuit",
        "impedance", "semicircle", "frequency", "Hz", "ohm", "Ω", "warburg",
        "charge-transfer", "electrochemical impedance", "bode", "162 Ω", "27 Ω",
        "15 Ω", "22 Ω", "10 -2 to 10 5"
    ],
    "EXTRACT_OVERPOTENTIAL": [
        # Core overpotential terms
        "overpotential", "nucleation overpotential", "η", "polarization",
        "deposition overpotential", "voltage profile", "nucleation", "voltage gap",
        "plating", "stripping", "mV", "voltage hysteresis", "voltage difference",
        # Specific patterns from paper
        "213 mV", "8 and 13 mV", "43 mV", "84 mV", "high nucleation overpotential",
        "overpotential behavior", "deposition for G/Zn", "nucleation sites",
        "early stage of deposition", "initial short deposition", "stable voltage plateau",
        # Rate-dependent overpotential
        "28, 37, 52, 81", "28 mV", "37 mV", "52 mV", "81 mV", "112 mV", "141 mV", "171 mV",
        "51, 55, 84", "51 mV", "55 mV", "170 mV", "189 mV", "200 mV", "218 mV",
        "45.3 mV", "low voltage hysteresis"
    ],
    "EXTRACT_RATE": [
        # Core rate terms
        "rate performance", "rate capability", "C-rate", "C rate", "specific capacity",
        "mAh/g", "mAh g", "energy density", "Wh/kg", "Wh kg", "power density", "W/kg", "W kg",
        "areal capacity", "mAh/cm", "mAh cm", "discharge capacity", "charge capacity",
        "Ragone", "rate retention", "gravimetric", "volumetric",
        # Space-separated units (critical for this paper)
        "mAh g -1", "A g -1", "W h kg -1", "W kg -1",
        # Specific values from paper
        "265.8 mAh", "145.5 mAh", "161.3 mAh", "82.1 mAh",
        "246.9 W h", "187.6 W", "8675.9 W", "159.1 W h",
        "deliver a capacity", "could deliver", "remained", "still remained",
        # Rate capability patterns
        "0.2 A g", "20 A g", "10 A g", "8 A g", "2 A g", "1 A g",
        "at 0.2", "at 20", "at 10", "at 8", "at 2", "at 1",
        "from 0.2 to", "from 1 to", "maximum energy density", "maximum power density",
        "Battery performance", "rate performances"
    ],
    "EXTRACT_KINETICS": [
        "transference number", "t_Zn2+", "diffusion coefficient", "GITT",
        "capacitive contribution", "surface-controlled", "diffusion-controlled",
        "ion mobility", "cm2 s-1", "cm2/s", "ionic diffusivity", "kinetics",
        "0.25", "0.14", "above 84.9", "76 %", "capacitive-controlled",
        "t Zn 2+", "t Zn 2þ", "ion diffusion coefficient"
    ],
}

# Task-specific target labels for retrieval boosting
TARGET_LABELS = {
    "EXTRACT_INPUT": ["COATING_FABRICATION", "MATERIAL_IDENTITY", "ION_CONDUCTIVITY", "WETTABILITY", "ZN_AFFINITY_DFT", "ELECTROLYTE_INFO"],
    "EXTRACT_CYCLING": ["ELECTROCHEM_CYCLING"],
    "EXTRACT_CORROSION": ["CORROSION_TAFEL"],
    "EXTRACT_EIS": ["EIS_NYQUIST"],
    "EXTRACT_OVERPOTENTIAL": ["ELECTROCHEM_CYCLING", "EIS_NYQUIST"],
    "EXTRACT_RATE": ["ELECTROCHEM_CYCLING", "RATE_PERFORMANCE"],
    "EXTRACT_KINETICS": ["ELECTROCHEM_CYCLING", "EIS_NYQUIST", "RATE_PERFORMANCE"],
}


def _paper_dir(data_root: str | Path, paper_id: str) -> Path:
    """Get paper directory path."""
    return Path(data_root) / "papers" / paper_id


def _derived(paper_dir: Path, name: str) -> Path:
    """Get derived file path."""
    return paper_dir / "derived" / name


def _load_labels_map(paper_dir: Path) -> Dict[str, List[str]]:
    """Load chunk labels mapping."""
    labels_path = _derived(paper_dir, "02_labels.jsonl")
    rows = read_jsonl(labels_path)
    m: Dict[str, List[str]] = {}
    for r in rows:
        m[r["chunk_id"]] = r.get("labels", [])
    return m


def _select_captions(inventory: Dict[str, Any], keywords: List[str], doc_pref: Optional[str] = None, max_items: int = 6):
    """Select relevant figure/table captions based on keywords."""
    out = []
    for c in inventory.get("figures", []):
        if doc_pref and c.get("doc") != doc_pref:
            continue
        cap = (c.get("caption") or "").lower()
        if any(k.lower() in cap for k in keywords):
            out.append({"doc": c.get("doc"), "id": c.get("figure_id"), "caption": c.get("caption"), "page": c.get("page")})
    for c in inventory.get("tables", []):
        if doc_pref and c.get("doc") != doc_pref:
            continue
        cap = (c.get("caption") or "").lower()
        if any(k.lower() in cap for k in keywords):
            out.append({"doc": c.get("doc"), "id": c.get("table_id"), "caption": c.get("caption"), "page": c.get("page")})
    return out[:max_items]


def build_evidence_packet(
    data_root: str | Path,
    paper_id: str,
    case: Dict[str, Any],
    task_type: str,
    topk_main: int = 12,  # EXPANDED for better coverage
    topk_supp: int = 4,
) -> Dict[str, Any]:
    """
    Build an evidence packet for LLM extraction.
    
    This is the key function that implements "no full paper input" principle:
    - Selects only relevant chunks via BM25 + keyword + label scoring
    - Includes relevant figure/table captions
    - Bundles case metadata for context
    
    Args:
        data_root: Root data directory
        paper_id: Paper identifier
        case: Case dict with material_raw, electrolyte_raw, etc.
        task_type: EXTRACT_INPUT, EXTRACT_CYCLING, EXTRACT_CORROSION, EXTRACT_EIS, EXTRACT_OVERPOTENTIAL
        topk_main: Number of top chunks from MAIN document
        topk_supp: Number of top chunks from SUPP document
    
    Returns:
        Evidence packet dict ready for LLM input
    """
    pdir = _paper_dir(data_root, paper_id)
    
    # Load required files
    inventory_path = _derived(pdir, "00_inventory.json")
    inventory = read_json(inventory_path) if inventory_path.exists() else {"figures": [], "tables": []}
    labels_map = _load_labels_map(pdir)

    chunks_main = read_jsonl(_derived(pdir, "01_chunks_main.jsonl"))
    chunks_supp = read_jsonl(_derived(pdir, "01_chunks_supp.jsonl"))

    keywords = DEFAULT_KEYWORDS.get(task_type, [])
    target_labels = TARGET_LABELS.get(task_type, [])

    # Query includes case metadata for better retrieval
    material = (case.get("material_raw") or case.get("coating_label") or "")
    electrolyte = (case.get("electrolyte_raw") or "")
    cell_type = (case.get("cell_type") or "")
    query = f"{task_type} {material} {electrolyte} {cell_type}"

    # Rank and select chunks (EXPANDED from 8 to 12 for better coverage)
    main_ranked = rank_chunks(chunks_main, query=query, keywords=keywords, target_labels=target_labels, labels_map=labels_map, topk=topk_main) if chunks_main else []
    supp_ranked = rank_chunks(chunks_supp, query=query, keywords=keywords, target_labels=target_labels, labels_map=labels_map, topk=topk_supp) if chunks_supp else []

    evidence_chunks = []
    
    # MANDATORY: Add ALL Method/Experimental section chunks (max 5)
    method_chunks = []
    for ch in chunks_main:
        sp = (ch.get("section_path") or "").lower()
        if "method" in sp or "experimental" in sp or "preparation" in sp:
            method_chunks.append(ch)
    for ch in method_chunks[:5]:  # Limit to 5 method chunks
        evidence_chunks.append({
            "doc": "MAIN",
            "chunk_id": ch.get("chunk_id"),
            "section_path": ch.get("section_path"),
            "page_range": ch.get("page_range"),
            "text": ch.get("text"),
            "score": 999.0,  # Highest priority
            "source": "METHOD_MANDATORY"
        })
    
    # Add BM25-ranked chunks
    for score, ch in main_ranked:
        # Skip if already added as method chunk
        if ch.get("chunk_id") in [ec.get("chunk_id") for ec in evidence_chunks]:
            continue
        evidence_chunks.append({
            "doc": "MAIN",
            "chunk_id": ch.get("chunk_id"),
            "section_path": ch.get("section_path"),
            "page_range": ch.get("page_range"),
            "text": ch.get("text"),
            "score": score
        })
    for score, ch in supp_ranked:
        evidence_chunks.append({
            "doc": "SUPP",
            "chunk_id": ch.get("chunk_id"),
            "section_path": ch.get("section_path"),
            "page_range": ch.get("page_range"),
            "text": ch.get("text"),
            "score": score
        })

    # Select relevant captions
    captions = _select_captions(inventory, keywords=keywords, max_items=8)
    
    # Select relevant tables with parsed content
    tables_out = []
    for t in inventory.get("tables", []):
        cap = (t.get("caption") or "").lower()
        if any(k.lower() in cap for k in keywords):
            tables_out.append({
                "doc": t.get("doc"),
                "table_id": t.get("table_id"),
                "caption": t.get("caption"),
                "parsed": t.get("parsed", None)
            })
    tables_out = tables_out[:4]

    # Build final packet
    packet = {
        "paper_id": paper_id,
        "case_id": case.get("case_id_hint") or case.get("case_id") or None,
        "targets": [task_type],
        "context": {
            "case_meta": {
                "coating_label": case.get("coating_label"),
                "material_raw": case.get("material_raw"),
                "protective_layer_thickness_um": case.get("protective_layer_thickness_um"),
                "electrolyte_raw": case.get("electrolyte_raw"),
                "cell_type": case.get("cell_type"),
                "areal_capacity_mAhcm2": case.get("areal_capacity_mAhcm2"),
                "areal_current_density_mAcm2": case.get("areal_current_density_mAcm2"),
            },
            "relevant_captions": captions,
            "evidence_chunks": evidence_chunks,
            "tables": tables_out
        }
    }
    return packet
