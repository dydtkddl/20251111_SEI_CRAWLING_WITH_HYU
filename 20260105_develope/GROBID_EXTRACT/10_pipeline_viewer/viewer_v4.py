"""
Pipeline Viewer v4.0 - Measurement Review System
Focused viewer for Contract v1.1 measurements with QC review capabilities

Features:
- Paper list with measurement counts
- Measurement table with filtering by metric/extractor
- Evidence quote highlighting
- QC flag visualization
- Registry validation status
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ============================================================================
# Configuration
# ============================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("MeasurementViewer")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR.parent / "data" / "papers"

# Create FastAPI app
app = FastAPI(
    title="Measurement Viewer v4.0",
    description="Contract v1.1 Measurement Review System",
    version="4.0.0"
)

# Mount static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================================
# Data Loading Functions
# ============================================================================

def load_jsonl(filepath: Path) -> List[Dict[str, Any]]:
    """Load JSONL file into list of dicts."""
    items = []
    if not filepath.exists():
        return items
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('.'):
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        logger.error(f"Error loading {filepath}: {e}")
    return items


def load_json(filepath: Path) -> Dict[str, Any]:
    """Load JSON file."""
    if not filepath.exists():
        return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {filepath}: {e}")
        return {}


def get_all_papers() -> List[Dict[str, Any]]:
    """Get list of all papers with summary info."""
    papers = []
    
    if not DATA_DIR.exists():
        return papers
    
    for paper_dir in sorted(DATA_DIR.iterdir()):
        if not paper_dir.is_dir():
            continue
        
        paper_id = paper_dir.name
        derived_dir = paper_dir / "derived"
        
        if not derived_dir.exists():
            continue
        
        # Load measurements to count
        raw_path = derived_dir / "06_measurements_raw.jsonl"
        measurements = load_jsonl(raw_path)
        
        # Load QC report
        qc_path = derived_dir / "09_qc_report.json"
        qc_report = load_json(qc_path)
        
        # Count by metric
        metric_counts = {}
        extractor_counts = {}
        for m in measurements:
            metric = m.get("metric", "unknown")
            extractor = m.get("extractor_id", "unknown")
            metric_counts[metric] = metric_counts.get(metric, 0) + 1
            extractor_counts[extractor] = extractor_counts.get(extractor, 0) + 1
        
        # Count QC flags
        qc_flags_count = 0
        if qc_report:
            flags = qc_report.get("qc_flags", [])
            if isinstance(flags, list):
                qc_flags_count = len(flags)
        
        papers.append({
            "paper_id": paper_id,
            "measurement_count": len(measurements),
            "metric_counts": metric_counts,
            "extractor_counts": extractor_counts,
            "qc_flags_count": qc_flags_count,
            "has_qc_report": qc_path.exists(),
        })
    
    return papers


def get_paper_measurements(paper_id: str) -> Dict[str, Any]:
    """Get all measurements for a paper."""
    paper_dir = DATA_DIR / paper_id
    derived_dir = paper_dir / "derived"
    
    if not derived_dir.exists():
        raise HTTPException(status_code=404, detail=f"Paper not found: {paper_id}")
    
    # Load all measurement files
    raw = load_jsonl(derived_dir / "06_measurements_raw.jsonl")
    organized = load_jsonl(derived_dir / "07_measurements_organized.jsonl")
    normalized = load_jsonl(derived_dir / "08_measurements_normalized.jsonl")
    final = load_jsonl(derived_dir / "10_measurements_final.jsonl")
    digitize_tasks = load_jsonl(derived_dir / "10_tasks_digitize.jsonl")
    
    # Load QC report
    qc_report = load_json(derived_dir / "09_qc_report.json")
    
    # Load inventory
    inventory = load_json(derived_dir / "00_inventory.json")
    
    return {
        "paper_id": paper_id,
        "inventory": inventory,
        "measurements": {
            "raw": raw,
            "organized": organized,
            "normalized": normalized,
            "final": final,
        },
        "digitize_tasks": digitize_tasks,
        "qc_report": qc_report,
    }


# ============================================================================
# API Endpoints
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def root():
    """Main viewer page."""
    return get_main_html()


@app.get("/api/papers")
async def api_get_papers():
    """Get list of all papers."""
    return {"papers": get_all_papers()}


@app.get("/api/papers/{paper_id}")
async def api_get_paper(paper_id: str):
    """Get full paper data."""
    return get_paper_measurements(paper_id)


@app.get("/api/papers/{paper_id}/measurements")
async def api_get_measurements(
    paper_id: str,
    stage: str = Query("raw", regex="^(raw|organized|normalized|final)$"),
    metric: Optional[str] = None,
    extractor: Optional[str] = None,
):
    """Get measurements with optional filtering."""
    data = get_paper_measurements(paper_id)
    measurements = data["measurements"].get(stage, [])
    
    # Filter by metric
    if metric:
        measurements = [m for m in measurements if m.get("metric") == metric]
    
    # Filter by extractor
    if extractor:
        measurements = [m for m in measurements if extractor in m.get("extractor_id", "")]
    
    return {
        "paper_id": paper_id,
        "stage": stage,
        "count": len(measurements),
        "measurements": measurements,
    }


@app.get("/api/papers/{paper_id}/qc")
async def api_get_qc(paper_id: str):
    """Get QC report for paper."""
    data = get_paper_measurements(paper_id)
    return {
        "paper_id": paper_id,
        "qc_report": data.get("qc_report", {}),
    }


@app.get("/api/stats")
async def api_get_stats():
    """Get overall statistics."""
    papers = get_all_papers()
    
    total_measurements = sum(p["measurement_count"] for p in papers)
    total_qc_flags = sum(p["qc_flags_count"] for p in papers)
    
    # Aggregate metric counts
    all_metrics = {}
    all_extractors = {}
    for p in papers:
        for metric, count in p["metric_counts"].items():
            all_metrics[metric] = all_metrics.get(metric, 0) + count
        for ext, count in p["extractor_counts"].items():
            all_extractors[ext] = all_extractors.get(ext, 0) + count
    
    return {
        "total_papers": len(papers),
        "total_measurements": total_measurements,
        "total_qc_flags": total_qc_flags,
        "metrics": dict(sorted(all_metrics.items(), key=lambda x: -x[1])),
        "extractors": dict(sorted(all_extractors.items(), key=lambda x: -x[1])),
    }


# ============================================================================
# HTML Template
# ============================================================================

def get_main_html() -> str:
    """Generate main HTML page."""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Measurement Viewer v4.0</title>
    <style>
        :root {
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-tertiary: #21262d;
            --text-primary: #c9d1d9;
            --text-secondary: #8b949e;
            --accent: #58a6ff;
            --accent-green: #3fb950;
            --accent-red: #f85149;
            --accent-yellow: #d29922;
            --border: #30363d;
        }
        
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.5;
        }
        
        .container {
            max-width: 1600px;
            margin: 0 auto;
            padding: 20px;
        }
        
        header {
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border);
            padding: 16px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        header h1 {
            font-size: 1.4rem;
            font-weight: 600;
            color: var(--accent);
        }
        
        .stats-bar {
            display: flex;
            gap: 24px;
        }
        
        .stat-item {
            text-align: center;
        }
        
        .stat-value {
            font-size: 1.5rem;
            font-weight: 700;
            color: var(--accent-green);
        }
        
        .stat-label {
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
        }
        
        .main-grid {
            display: grid;
            grid-template-columns: 320px 1fr;
            gap: 20px;
            margin-top: 20px;
        }
        
        .sidebar {
            background: var(--bg-secondary);
            border-radius: 8px;
            border: 1px solid var(--border);
            overflow: hidden;
        }
        
        .sidebar-header {
            padding: 12px 16px;
            background: var(--bg-tertiary);
            border-bottom: 1px solid var(--border);
            font-weight: 600;
        }
        
        .paper-list {
            max-height: calc(100vh - 200px);
            overflow-y: auto;
        }
        
        .paper-item {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
            cursor: pointer;
            transition: background 0.2s;
        }
        
        .paper-item:hover {
            background: var(--bg-tertiary);
        }
        
        .paper-item.active {
            background: var(--bg-tertiary);
            border-left: 3px solid var(--accent);
        }
        
        .paper-id {
            font-family: monospace;
            font-size: 0.85rem;
            color: var(--accent);
        }
        
        .paper-meta {
            display: flex;
            gap: 12px;
            margin-top: 4px;
            font-size: 0.75rem;
            color: var(--text-secondary);
        }
        
        .badge {
            display: inline-flex;
            align-items: center;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 0.7rem;
            font-weight: 600;
        }
        
        .badge-green { background: rgba(63, 185, 80, 0.2); color: var(--accent-green); }
        .badge-yellow { background: rgba(210, 153, 34, 0.2); color: var(--accent-yellow); }
        .badge-red { background: rgba(248, 81, 73, 0.2); color: var(--accent-red); }
        
        .content {
            background: var(--bg-secondary);
            border-radius: 8px;
            border: 1px solid var(--border);
            overflow: hidden;
        }
        
        .content-header {
            padding: 16px 20px;
            background: var(--bg-tertiary);
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .tabs {
            display: flex;
            gap: 8px;
        }
        
        .tab {
            padding: 8px 16px;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: var(--text-secondary);
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .tab:hover { background: var(--bg-tertiary); }
        .tab.active { background: var(--accent); color: white; border-color: var(--accent); }
        
        .filter-bar {
            padding: 12px 20px;
            background: var(--bg-tertiary);
            border-bottom: 1px solid var(--border);
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }
        
        .filter-bar select, .filter-bar input {
            padding: 8px 12px;
            background: var(--bg-primary);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: var(--text-primary);
            font-size: 0.85rem;
        }
        
        .measurement-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .measurement-table th {
            padding: 12px 16px;
            text-align: left;
            background: var(--bg-tertiary);
            border-bottom: 1px solid var(--border);
            font-size: 0.75rem;
            text-transform: uppercase;
            color: var(--text-secondary);
            position: sticky;
            top: 0;
        }
        
        .measurement-table td {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
            font-size: 0.85rem;
            vertical-align: top;
        }
        
        .measurement-table tr:hover {
            background: var(--bg-tertiary);
        }
        
        .metric-name {
            font-family: monospace;
            color: var(--accent);
            font-weight: 600;
        }
        
        .value-cell {
            font-weight: 700;
            color: var(--accent-green);
        }
        
        .value-null {
            color: var(--accent-yellow);
            font-style: italic;
        }
        
        .evidence-quote {
            font-style: italic;
            color: var(--text-secondary);
            max-width: 400px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .tag {
            display: inline-block;
            padding: 2px 6px;
            background: var(--bg-primary);
            border-radius: 4px;
            font-size: 0.7rem;
            margin: 2px;
        }
        
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        
        .modal.active { display: flex; }
        
        .modal-content {
            background: var(--bg-secondary);
            border-radius: 12px;
            border: 1px solid var(--border);
            max-width: 800px;
            max-height: 80vh;
            width: 90%;
            overflow: auto;
        }
        
        .modal-header {
            padding: 16px 20px;
            background: var(--bg-tertiary);
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .modal-body {
            padding: 20px;
        }
        
        .modal-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.5rem;
            cursor: pointer;
        }
        
        pre {
            background: var(--bg-primary);
            padding: 16px;
            border-radius: 8px;
            overflow-x: auto;
            font-size: 0.8rem;
        }
        
        .loading {
            text-align: center;
            padding: 40px;
            color: var(--text-secondary);
        }
        
        @media (max-width: 900px) {
            .main-grid {
                grid-template-columns: 1fr;
            }
            .sidebar { order: 2; }
            .content { order: 1; }
        }
    </style>
</head>
<body>
    <header>
        <h1>📊 Measurement Viewer v4.0</h1>
        <div class="stats-bar" id="statsBar">
            <div class="stat-item">
                <div class="stat-value" id="statPapers">-</div>
                <div class="stat-label">Papers</div>
            </div>
            <div class="stat-item">
                <div class="stat-value" id="statMeasurements">-</div>
                <div class="stat-label">Measurements</div>
            </div>
            <div class="stat-item">
                <div class="stat-value" id="statQCFlags">-</div>
                <div class="stat-label">QC Flags</div>
            </div>
        </div>
    </header>
    
    <div class="container">
        <div class="main-grid">
            <div class="sidebar">
                <div class="sidebar-header">📁 Papers</div>
                <div class="paper-list" id="paperList">
                    <div class="loading">Loading papers...</div>
                </div>
            </div>
            
            <div class="content">
                <div class="content-header">
                    <h2 id="contentTitle">Select a paper</h2>
                    <div class="tabs">
                        <button class="tab active" data-stage="raw">Raw</button>
                        <button class="tab" data-stage="organized">Organized</button>
                        <button class="tab" data-stage="normalized">Normalized</button>
                        <button class="tab" data-stage="final">Final</button>
                    </div>
                </div>
                
                <div class="filter-bar">
                    <select id="metricFilter">
                        <option value="">All Metrics</option>
                    </select>
                    <select id="extractorFilter">
                        <option value="">All Extractors</option>
                    </select>
                    <input type="text" id="searchInput" placeholder="Search in quotes...">
                </div>
                
                <div id="measurementContainer">
                    <table class="measurement-table">
                        <thead>
                            <tr>
                                <th>Metric</th>
                                <th>Value</th>
                                <th>Unit</th>
                                <th>Confidence</th>
                                <th>Tags</th>
                                <th>Evidence</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody id="measurementBody">
                            <tr><td colspan="7" class="loading">Select a paper to view measurements</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    
    <div class="modal" id="detailModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>Measurement Details</h3>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body">
                <pre id="modalContent"></pre>
            </div>
        </div>
    </div>
    
    <script>
        // State
        let papers = [];
        let currentPaper = null;
        let currentStage = 'raw';
        let measurements = [];
        
        // Initialize
        document.addEventListener('DOMContentLoaded', async () => {
            await loadStats();
            await loadPapers();
            setupEventListeners();
        });
        
        // Load stats
        async function loadStats() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();
                document.getElementById('statPapers').textContent = data.total_papers;
                document.getElementById('statMeasurements').textContent = data.total_measurements;
                document.getElementById('statQCFlags').textContent = data.total_qc_flags;
            } catch (e) {
                console.error('Failed to load stats:', e);
            }
        }
        
        // Load papers
        async function loadPapers() {
            try {
                const res = await fetch('/api/papers');
                const data = await res.json();
                papers = data.papers;
                renderPaperList();
            } catch (e) {
                console.error('Failed to load papers:', e);
            }
        }
        
        // Render paper list
        function renderPaperList() {
            const container = document.getElementById('paperList');
            container.innerHTML = papers.map(p => `
                <div class="paper-item" data-paper="${p.paper_id}">
                    <div class="paper-id">${p.paper_id}</div>
                    <div class="paper-meta">
                        <span class="badge badge-green">${p.measurement_count} measurements</span>
                        ${p.qc_flags_count > 0 ? `<span class="badge badge-yellow">${p.qc_flags_count} flags</span>` : ''}
                    </div>
                </div>
            `).join('');
        }
        
        // Load measurements for paper
        async function loadMeasurements(paperId, stage = 'raw') {
            try {
                const res = await fetch(`/api/papers/${paperId}/measurements?stage=${stage}`);
                const data = await res.json();
                measurements = data.measurements;
                updateFilters();
                renderMeasurements();
            } catch (e) {
                console.error('Failed to load measurements:', e);
            }
        }
        
        // Update filter dropdowns
        function updateFilters() {
            const metrics = [...new Set(measurements.map(m => m.metric))].sort();
            const extractors = [...new Set(measurements.map(m => m.extractor_id).filter(Boolean))].sort();
            
            document.getElementById('metricFilter').innerHTML = 
                '<option value="">All Metrics</option>' +
                metrics.map(m => `<option value="${m}">${m}</option>`).join('');
            
            document.getElementById('extractorFilter').innerHTML = 
                '<option value="">All Extractors</option>' +
                extractors.map(e => `<option value="${e}">${e.split('_').slice(-1)[0]}</option>`).join('');
        }
        
        // Render measurements table
        function renderMeasurements() {
            const metricFilter = document.getElementById('metricFilter').value;
            const extractorFilter = document.getElementById('extractorFilter').value;
            const searchTerm = document.getElementById('searchInput').value.toLowerCase();
            
            let filtered = measurements;
            
            if (metricFilter) {
                filtered = filtered.filter(m => m.metric === metricFilter);
            }
            if (extractorFilter) {
                filtered = filtered.filter(m => m.extractor_id && m.extractor_id.includes(extractorFilter));
            }
            if (searchTerm) {
                filtered = filtered.filter(m => {
                    const quote = m.evidence?.quote || '';
                    return quote.toLowerCase().includes(searchTerm);
                });
            }
            
            const tbody = document.getElementById('measurementBody');
            
            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="loading">No measurements found</td></tr>';
                return;
            }
            
            tbody.innerHTML = filtered.map((m, i) => {
                const tags = m.tags || {};
                const tagHtml = Object.entries(tags)
                    .map(([k, v]) => `<span class="tag">${k}: ${v}</span>`)
                    .join('');
                
                const value = m.value;
                const valueHtml = value === null 
                    ? '<span class="value-null">null</span>' 
                    : `<span class="value-cell">${JSON.stringify(value)}</span>`;
                
                const quote = m.evidence?.quote || '-';
                const section = m.evidence?.section_path || '-';
                
                return `
                    <tr>
                        <td class="metric-name">${m.metric}</td>
                        <td>${valueHtml}</td>
                        <td>${m.unit || '-'}</td>
                        <td>${m.confidence?.toFixed(2) || '-'}</td>
                        <td>${tagHtml || '-'}</td>
                        <td>
                            <div style="font-size:0.7rem;color:var(--accent)">[${section}]</div>
                            <div class="evidence-quote" title="${quote}">${quote}</div>
                        </td>
                        <td>
                            <button class="tab" onclick="showDetail(${i})">View</button>
                        </td>
                    </tr>
                `;
            }).join('');
        }
        
        // Show detail modal
        function showDetail(index) {
            const m = measurements[index];
            document.getElementById('modalContent').textContent = JSON.stringify(m, null, 2);
            document.getElementById('detailModal').classList.add('active');
        }
        
        // Close modal
        function closeModal() {
            document.getElementById('detailModal').classList.remove('active');
        }
        
        // Setup event listeners
        function setupEventListeners() {
            // Paper selection
            document.getElementById('paperList').addEventListener('click', (e) => {
                const item = e.target.closest('.paper-item');
                if (item) {
                    const paperId = item.dataset.paper;
                    document.querySelectorAll('.paper-item').forEach(p => p.classList.remove('active'));
                    item.classList.add('active');
                    currentPaper = paperId;
                    document.getElementById('contentTitle').textContent = paperId;
                    loadMeasurements(paperId, currentStage);
                }
            });
            
            // Tab selection
            document.querySelectorAll('.tab[data-stage]').forEach(tab => {
                tab.addEventListener('click', () => {
                    document.querySelectorAll('.tab[data-stage]').forEach(t => t.classList.remove('active'));
                    tab.classList.add('active');
                    currentStage = tab.dataset.stage;
                    if (currentPaper) {
                        loadMeasurements(currentPaper, currentStage);
                    }
                });
            });
            
            // Filters
            document.getElementById('metricFilter').addEventListener('change', renderMeasurements);
            document.getElementById('extractorFilter').addEventListener('change', renderMeasurements);
            document.getElementById('searchInput').addEventListener('input', renderMeasurements);
            
            // Modal close on backdrop
            document.getElementById('detailModal').addEventListener('click', (e) => {
                if (e.target === document.getElementById('detailModal')) {
                    closeModal();
                }
            });
            
            // ESC to close modal
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') closeModal();
            });
        }
    </script>
</body>
</html>'''


# ============================================================================
# Run
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
