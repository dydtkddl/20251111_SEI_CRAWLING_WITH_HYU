# scripts/lib/retrieval.py
"""BM25 + keyword/section/label weighted chunk retrieval."""
from __future__ import annotations
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

_TOKEN_RE = re.compile(r"[A-Za-z0-9\-\+\.\%µμ/]+", re.UNICODE)


def tokenize(text: str) -> List[str]:
    """Tokenize text into lowercase words."""
    text = text.lower()
    return _TOKEN_RE.findall(text)


@dataclass
class BM25Index:
    """BM25 index structure."""
    docs: List[List[str]]
    idf: Dict[str, float]
    avgdl: float
    doc_lens: List[int]
    k1: float = 1.5
    b: float = 0.75


def build_bm25(texts: List[str], k1: float = 1.5, b: float = 0.75) -> BM25Index:
    """Build a BM25 index from a list of texts."""
    docs = [tokenize(t) for t in texts]
    N = len(docs)
    df: Dict[str, int] = {}
    for doc in docs:
        seen = set(doc)
        for w in seen:
            df[w] = df.get(w, 0) + 1
    idf: Dict[str, float] = {}
    for w, n in df.items():
        idf[w] = math.log(1.0 + (N - n + 0.5) / (n + 0.5))
    doc_lens = [len(d) for d in docs]
    avgdl = sum(doc_lens) / max(1, N)
    return BM25Index(docs=docs, idf=idf, avgdl=avgdl, doc_lens=doc_lens, k1=k1, b=b)


def bm25_score(index: BM25Index, query: str) -> List[float]:
    """Score all documents against a query using BM25."""
    q = tokenize(query)
    scores = [0.0] * len(index.docs)
    for i, doc in enumerate(index.docs):
        dl = index.doc_lens[i]
        tf: Dict[str, int] = {}
        for w in doc:
            tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w in q:
            if w not in tf:
                continue
            idf = index.idf.get(w, 0.0)
            f = tf[w]
            denom = f + index.k1 * (1 - index.b + index.b * dl / (index.avgdl + 1e-9))
            s += idf * (f * (index.k1 + 1)) / (denom + 1e-9)
        scores[i] = s
    return scores


def keyword_hits(text: str, keywords: List[str]) -> int:
    """Count how many keywords appear in the text."""
    t = text.lower()
    return sum(1 for k in keywords if k.lower() in t)


def section_bonus(section_path: str) -> float:
    """
    Calculate bonus score based on section path.
    
    Priority order:
    1. Results/Discussion (actual data) - 3.0x
    2. Methods/Experimental (conditions) - 2.0x
    3. Characterization subsections - 1.5x
    4. Others - minimal bonus
    """
    sp = (section_path or "").lower()
    bonus = 0.0
    
    # HIGHEST PRIORITY: Results and Discussion sections
    if "result" in sp or "discussion" in sp:
        bonus += 3.0
    
    # HIGH PRIORITY: Methods and Experimental sections
    if "method" in sp or "experimental" in sp or "preparation" in sp:
        bonus += 2.0
    
    # MEDIUM PRIORITY: Specific characterization subsections
    if "electrochem" in sp or "nyquist" in sp or "eis" in sp:
        bonus += 1.5
    if "tafel" in sp or "polarization" in sp or "lsp" in sp:
        bonus += 1.5
    if "sem" in sp or "xrd" in sp or "ftir" in sp:
        bonus += 1.0
    if "contact angle" in sp or "wettability" in sp:
        bonus += 1.0
    
    # SUPPLEMENTARY: Supporting information
    if "support" in sp or "supp" in sp:
        bonus += 0.8
    
    # PENALTY: Introduction and Conclusion have less useful data
    if "introduction" in sp:
        bonus -= 0.5
    if "conclusion" in sp or "summary" in sp:
        bonus -= 0.3
    
    return bonus


def label_bonus(labels: List[str], target_labels: List[str]) -> float:
    """Calculate bonus score based on label matches."""
    if not labels:
        return 0.0
    s = 0.0
    L = set(labels)
    for tl in target_labels:
        if tl in L:
            s += 2.0
    return s


def rank_chunks(
    chunks: List[Dict[str, Any]],
    query: str,
    keywords: List[str],
    target_labels: List[str],
    labels_map: Dict[str, List[str]],
    topk: int = 8
) -> List[Tuple[float, Dict[str, Any]]]:
    """
    Rank chunks by combined BM25 + keyword + section + label scores.
    
    Args:
        chunks: List of chunk dicts with 'text', 'chunk_id', 'section_path'
        query: Search query string
        keywords: List of domain-specific keywords
        target_labels: List of target labels to boost
        labels_map: Mapping from chunk_id to assigned labels
        topk: Number of top results to return
    
    Returns:
        List of (score, chunk) tuples, sorted by score descending
    """
    texts = [c.get("text", "") for c in chunks]
    idx = build_bm25(texts)
    base_scores = bm25_score(idx, query)

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for s, ch in zip(base_scores, chunks):
        cid = ch.get("chunk_id")
        lbs = labels_map.get(cid, [])
        score = 0.0
        score += 2.5 * s
        score += 0.8 * keyword_hits(ch.get("text", ""), keywords)
        score += section_bonus(ch.get("section_path", ""))
        score += label_bonus(lbs, target_labels)
        scored.append((score, ch))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:topk]
