#!/usr/bin/env python3
"""
02_marker_pdf_convert.py
========================
Marker 기반 PDF → Markdown 변환 파이프라인
- Elsevier 논문 본문 PDF 및 Supplementary PDF 지원
- Ray 분산 처리 + GPU 공유 모델 로딩
- Rich 기반 실시간 모니터링
- PII별 개별 .md 파일 저장 + 메타데이터 CSV
- Resume 기능 (중단 후 재시작 시 완료분 스킵)
- 에러 유형별 분기 핸들링 및 OOM 재시도

사용법:
    ENV_NAME=colab_a100 ROOT_DIR=/workspace python 02_marker_pdf_convert.py
    ENV_NAME=rtx4090 ROOT_DIR=/data python 02_marker_pdf_convert.py

환경변수:
    ENV_NAME  : 실행 환경 이름 (colab_a100, rtx4090, rtx5090, l4x2)
    ROOT_DIR  : 프로젝트 루트 디렉토리
"""

import os
import re
import csv
import sys
import json
import time
import signal
import logging
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any, Tuple

import ray
import torch
import pandas as pd
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    TimeElapsedColumn,
    MofNCompleteColumn,
)

# ============================================================================
# 환경 설정
# ============================================================================

ENV_NAME = os.environ.get("ENV_NAME", "rtx4090").lower()
ROOT_DIR = Path(os.environ.get("ROOT_DIR", "."))

# GPU 환경별 설정 프로파일
# vram_gb: 단일 GPU VRAM (GB)
# num_gpus: 사용 가능한 GPU 수
# workers_per_gpu: GPU당 worker 수
# gpu_fraction: worker당 GPU 할당 비율
# marker는 worker당 평균 3.5GB, 피크 5GB VRAM 사용
GPU_PROFILES = {
    "colab_a100": {"vram_gb": 40, "num_gpus": 1, "workers_per_gpu": 6, "gpu_fraction": 0.16},
    "rtx4090":    {"vram_gb": 24, "num_gpus": 1, "workers_per_gpu": 4, "gpu_fraction": 0.25},
    "rtx5090":    {"vram_gb": 32, "num_gpus": 1, "workers_per_gpu": 2, "gpu_fraction": 0.50},
    "l4x2":       {"vram_gb": 24, "num_gpus": 2, "workers_per_gpu": 1, "gpu_fraction": 1.0},
}

if ENV_NAME not in GPU_PROFILES:
    print(f"[ERROR] Unknown ENV_NAME: {ENV_NAME}. Available: {list(GPU_PROFILES.keys())}")
    sys.exit(1)

GPU_PROFILE = GPU_PROFILES[ENV_NAME]
TOTAL_WORKERS = GPU_PROFILE["num_gpus"] * GPU_PROFILE["workers_per_gpu"]
GPU_FRACTION = GPU_PROFILE["gpu_fraction"]

# 경로 설정
INPUT_CSV = ROOT_DIR / f"01_preprocess_03_{ENV_NAME}.csv"
OUTPUT_DIR = ROOT_DIR / "output"
META_CSV = ROOT_DIR / f"02_conversion_meta_{ENV_NAME}.csv"
ERROR_LOG = ROOT_DIR / f"02_conversion_errors_{ENV_NAME}.log"

# Marker 설정: 논문 본문 PDF용
MAIN_PDF_CONFIG = {
    "force_ocr": False,          # 디지털 텍스트 기반 처리로 VRAM 사용량 완화
    "strip_existing_ocr": False,  # 기존 디지털 텍스트는 유지
    "paginate_output": False,     # 페이지 구분자 불필요
    "extract_images": True,       # Figure 이미지 추출
    "output_format": "markdown",
    "languages": ["en"],          # 영문 논문
}

# Marker 설정: Supplementary PDF용 (LibreOffice 변환 PDF, 단순 레이아웃)
SUPP_PDF_CONFIG = {
    "force_ocr": False,           # docx→PDF 변환이므로 디지털 텍스트 품질 양호
    "strip_existing_ocr": False,
    "paginate_output": False,
    "extract_images": True,
    "output_format": "markdown",
    "languages": ["en"],
}

# OOM 재시도 설정
MAX_OOM_RETRIES = 2

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(ERROR_LOG, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================================
# 데이터 구조
# ============================================================================

@dataclass
class ConversionTask:
    """변환 작업 단위"""
    pii: str
    pdf_path: str
    output_md_path: str
    output_img_dir: str
    file_type: str           # "main" 또는 "supp"
    supp_index: Optional[str] = None  # mmc 번호 (supp인 경우)

    @property
    def config(self) -> dict:
        return MAIN_PDF_CONFIG if self.file_type == "main" else SUPP_PDF_CONFIG


@dataclass
class ConversionResult:
    """변환 결과"""
    pii: str
    file_type: str
    pdf_filename: str
    success: bool
    page_count: int = 0
    processing_time: float = 0.0
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    output_md_path: Optional[str] = None
    metadata: Optional[dict] = None


# ============================================================================
# 유틸리티 함수
# ============================================================================

def extract_pii_from_filename(filename: str) -> Optional[str]:
    """파일명에서 PII 추출"""
    # main PDF: 1-s2.0-{PII}-main.pdf
    m = re.match(r"1-s2\.0-(.+?)-main\.pdf", filename)
    if m:
        return m.group(1)
    # supp PDF: {PII}_1-s2.0-{PII}-mmc{N}.pdf
    m = re.match(r"(.+?)_1-s2\.0-.+?-mmc(\d+)\.pdf", filename)
    if m:
        return m.group(1)
    return None


def extract_mmc_index(filename: str) -> Optional[str]:
    """Supplementary 파일에서 mmc 번호 추출"""
    m = re.search(r"-mmc(\d+)\.pdf", filename)
    return m.group(1) if m else None


def classify_error(exc: Exception) -> str:
    """예외 유형 분류"""
    exc_type = type(exc).__name__
    exc_str = str(exc).lower()

    if isinstance(exc, torch.cuda.OutOfMemoryError) or "out of memory" in exc_str:
        return "OOM"
    elif "pdfium" in exc_type.lower() or "pdfium" in exc_str:
        return "PDF_CORRUPT"
    elif "timeout" in exc_str or "timed out" in exc_str:
        return "TIMEOUT"
    elif "cuda" in exc_str or "gpu" in exc_str:
        return "GPU_ERROR"
    elif "permission" in exc_str or "access" in exc_str:
        return "FILE_ACCESS"
    elif "memory" in exc_str:
        return "MEMORY"
    else:
        return "UNKNOWN"


def check_already_done(output_md_path: str) -> bool:
    """이미 변환 완료된 파일인지 확인"""
    p = Path(output_md_path)
    return p.exists() and p.stat().st_size > 0


def build_task_list(csv_path: Path, output_dir: Path) -> List[ConversionTask]:
    """
    CSV를 읽어 ConversionTask 리스트를 생성.
    CSV 컬럼: pii, pdf_folder, pdf_filename, supp_folder, supp_filename
    supp_filename은 세미콜론(;)으로 다중 파일 구분 가능.
    """
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    tasks = []

    for _, row in df.iterrows():
        pii = row["pii"].strip()
        if not pii:
            continue

        pii_output_dir = output_dir / pii

        # 논문 본문 PDF
        pdf_folder = row.get("pdf_folder", "").strip()
        pdf_filename = row.get("pdf_filename", "").strip()
        if pdf_filename:
            pdf_path = str(ROOT_DIR / pdf_folder / pdf_filename) if pdf_folder else str(ROOT_DIR / pdf_filename)
            md_path = str(pii_output_dir / "main.md")
            img_dir = str(pii_output_dir / "images" / "main")

            if not check_already_done(md_path):
                tasks.append(ConversionTask(
                    pii=pii,
                    pdf_path=pdf_path,
                    output_md_path=md_path,
                    output_img_dir=img_dir,
                    file_type="main",
                ))

        # Supplementary PDF(s)
        supp_folder = row.get("supp_folder", "").strip()
        supp_filename = row.get("supp_filename", "").strip()
        if supp_filename:
            # 세미콜론 구분 다중 파일 처리
            supp_files = [f.strip() for f in supp_filename.split(";") if f.strip()]
            for sf in supp_files:
                supp_path = str(ROOT_DIR / "supplementary_files" / supp_folder / sf) if supp_folder else str(ROOT_DIR / sf)
                mmc_idx = extract_mmc_index(sf) or "1"
                md_path = str(pii_output_dir / f"mmc{mmc_idx}.md")
                img_dir = str(pii_output_dir / "images" / f"mmc{mmc_idx}")

                if not check_already_done(md_path):
                    tasks.append(ConversionTask(
                        pii=pii,
                        pdf_path=supp_path,
                        output_md_path=md_path,
                        output_img_dir=img_dir,
                        file_type="supp",
                        supp_index=mmc_idx,
                    ))

    return tasks


def save_result_to_disk(result: ConversionResult, markdown: Optional[str], images: Optional[dict]):
    """변환 결과를 디스크에 저장"""
    if not result.success or markdown is None:
        return

    md_path = Path(result.output_md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    # Markdown 저장
    md_path.write_text(markdown, encoding="utf-8")

    # 이미지 저장
    if images:
        img_dir = md_path.parent / "images" / (
            "main" if result.file_type == "main"
            else f"mmc{result.pii}"  # fallback
        )
        # ConversionResult에는 img_dir 정보가 없으므로 md_path 기반으로 추론
        # 실제로는 task에서 전달받은 img_dir 사용
        pass  # 이미지 저장은 worker 내부에서 수행


def append_meta_csv(meta_csv_path: Path, results: List[ConversionResult]):
    """메타데이터 CSV에 결과 추가"""
    file_exists = meta_csv_path.exists()
    fieldnames = [
        "pii", "file_type", "pdf_filename", "success",
        "page_count", "processing_time", "error_type",
        "error_message", "output_md_path", "timestamp",
    ]

    with open(meta_csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for r in results:
            writer.writerow({
                "pii": r.pii,
                "file_type": r.file_type,
                "pdf_filename": r.pdf_filename,
                "success": r.success,
                "page_count": r.page_count,
                "processing_time": round(r.processing_time, 2),
                "error_type": r.error_type or "",
                "error_message": (r.error_message or "")[:500],
                "output_md_path": r.output_md_path or "",
                "timestamp": datetime.now().isoformat(),
            })


# ============================================================================
# Ray Actor: GPU 모델을 1회만 로드하고 재사용하는 구조
# ============================================================================
# Ray remote 함수 대신 Actor를 사용하여:
#   1) create_model_dict()를 Actor 생성 시 1회만 호출 → VRAM 절약
#   2) 같은 Actor 내에서 여러 PDF를 순차 처리 → 모델 재로딩 없음
#   3) num_gpus=GPU_FRACTION으로 GPU 분할 할당

@ray.remote
class MarkerWorker:
    """
    Marker PDF 변환 워커 액터.
    생성 시 모델을 1회 로드하고, convert() 호출 시마다 재사용.
    """

    def __init__(self, worker_id: int, gpu_fraction: float):
        import torch
        self.worker_id = worker_id
        self.converter_main = None
        self.converter_supp = None
        self._init_converters()

    def _init_converters(self):
        """Marker 모델 및 Converter 초기화 (1회)"""
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.config.parser import ConfigParser

        # 모델 딕셔너리 1회 생성 — 모든 converter가 공유
        self.artifact_dict = create_model_dict()

        # 논문 본문용 Converter
        main_config_parser = ConfigParser(MAIN_PDF_CONFIG)
        self.converter_main = PdfConverter(
            config=main_config_parser.generate_config_dict(),
            artifact_dict=self.artifact_dict,
            processor_list=main_config_parser.get_processors(),
            renderer=main_config_parser.get_renderer(),
        )

        # Supplementary용 Converter
        supp_config_parser = ConfigParser(SUPP_PDF_CONFIG)
        self.converter_supp = PdfConverter(
            config=supp_config_parser.generate_config_dict(),
            artifact_dict=self.artifact_dict,
            processor_list=supp_config_parser.get_processors(),
            renderer=supp_config_parser.get_renderer(),
        )

    def ping(self) -> bool:
        """Worker 생존 확인용"""
        return True

    def convert(
        self,
        pii: str,
        pdf_path: str,
        output_md_path: str,
        output_img_dir: str,
        file_type: str,
        supp_index: Optional[str] = None,
    ) -> dict:
        """
        단일 PDF 변환 수행. OOM 시 CUDA 캐시 정리 후 재시도.
        반환: ConversionResult를 dict로 직렬화한 값 + markdown 텍스트
        """
        import torch
        from pathlib import Path

        pdf_filename = Path(pdf_path).name
        start_time = time.time()

        converter = self.converter_main if file_type == "main" else self.converter_supp
        last_error = None
        last_error_type = None

        for attempt in range(1 + MAX_OOM_RETRIES):
            try:
                rendered = converter(pdf_path)
                elapsed = time.time() - start_time

                # 결과 추출
                markdown_text = rendered.markdown
                images = rendered.images if hasattr(rendered, "images") else {}
                metadata = rendered.metadata if hasattr(rendered, "metadata") else {}
                page_count = converter.page_count if hasattr(converter, "page_count") and converter.page_count else 0

                # 디스크 저장
                md_p = Path(output_md_path)
                md_p.parent.mkdir(parents=True, exist_ok=True)
                md_p.write_text(markdown_text, encoding="utf-8")

                # 이미지 저장
                if images:
                    img_dir = Path(output_img_dir)
                    img_dir.mkdir(parents=True, exist_ok=True)
                    for img_name, img_obj in images.items():
                        try:
                            if hasattr(img_obj, "save"):
                                # PIL Image
                                if img_obj.mode != "RGB":
                                    img_obj = img_obj.convert("RGB")
                                img_obj.save(str(img_dir / img_name), "JPEG")
                        except Exception:
                            pass  # 이미지 저장 실패는 무시

                # 메타데이터 JSON 저장
                meta_path = md_p.parent / f"{md_p.stem}_meta.json"
                try:
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)
                except Exception:
                    pass

                return {
                    "pii": pii,
                    "file_type": file_type,
                    "pdf_filename": pdf_filename,
                    "success": True,
                    "page_count": page_count,
                    "processing_time": elapsed,
                    "error_type": None,
                    "error_message": None,
                    "output_md_path": output_md_path,
                }

            except torch.cuda.OutOfMemoryError as e:
                torch.cuda.empty_cache()
                last_error = e
                last_error_type = "OOM"
                if attempt < MAX_OOM_RETRIES:
                    time.sleep(2 * (attempt + 1))  # 점진적 대기
                    continue
                break

            except Exception as e:
                last_error = e
                last_error_type = classify_error(e)
                break

        # 실패 결과
        elapsed = time.time() - start_time
        error_msg = f"[{type(last_error).__name__}] {str(last_error)[:400]}"
        return {
            "pii": pii,
            "file_type": file_type,
            "pdf_filename": pdf_filename,
            "success": False,
            "page_count": 0,
            "processing_time": elapsed,
            "error_type": last_error_type,
            "error_message": error_msg,
            "output_md_path": None,
        }


# ============================================================================
# Rich 모니터링 대시보드
# ============================================================================

class Dashboard:
    """Rich 기반 실시간 변환 현황 대시보드"""

    def __init__(self, total_tasks: int, total_skipped: int):
        self.total_tasks = total_tasks
        self.total_skipped = total_skipped
        self.completed = 0
        self.success_count = 0
        self.fail_count = 0
        self.oom_count = 0
        self.corrupt_count = 0
        self.current_files: Dict[int, str] = {}
        self.start_time = time.time()
        self.recent_results: List[dict] = []

        self.console = Console()
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=50),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=self.console,
        )
        self.task_id = self.progress.add_task(
            "Converting PDFs", total=total_tasks
        )

    def update(self, result: dict):
        self.completed += 1
        if result["success"]:
            self.success_count += 1
        else:
            self.fail_count += 1
            if result.get("error_type") == "OOM":
                self.oom_count += 1
            elif result.get("error_type") == "PDF_CORRUPT":
                self.corrupt_count += 1

        self.recent_results.append(result)
        if len(self.recent_results) > 8:
            self.recent_results = self.recent_results[-8:]

        self.progress.update(self.task_id, completed=self.completed)

    def render(self) -> Table:
        elapsed = time.time() - self.start_time
        rate = self.completed / max(elapsed, 1)
        remaining = (self.total_tasks - self.completed) / max(rate, 0.001)

        # 상태 테이블
        status_table = Table(title=f"Marker PDF Pipeline — {ENV_NAME.upper()}", show_header=False, expand=True)
        status_table.add_column("Key", style="bold cyan", width=24)
        status_table.add_column("Value", style="white")

        status_table.add_row("Environment", ENV_NAME.upper())
        status_table.add_row("GPU Profile", f"{GPU_PROFILE['vram_gb']}GB × {GPU_PROFILE['num_gpus']} GPU, {TOTAL_WORKERS} workers")
        status_table.add_row("Total Tasks", str(self.total_tasks))
        status_table.add_row("Skipped (Resume)", str(self.total_skipped))
        status_table.add_row("Completed", f"{self.completed} / {self.total_tasks}")
        status_table.add_row("Success", f"[green]{self.success_count}[/green]")
        status_table.add_row("Failed", f"[red]{self.fail_count}[/red] (OOM: {self.oom_count}, Corrupt: {self.corrupt_count})")
        status_table.add_row("Speed", f"{rate:.2f} files/sec")
        status_table.add_row("Elapsed", str(timedelta(seconds=int(elapsed))))
        status_table.add_row("ETA", str(timedelta(seconds=int(remaining))))

        # 최근 결과 테이블
        recent_table = Table(title="Recent Conversions", expand=True)
        recent_table.add_column("PII", style="dim", max_width=24)
        recent_table.add_column("Type", width=6)
        recent_table.add_column("Status", width=10)
        recent_table.add_column("Pages", width=6, justify="right")
        recent_table.add_column("Time(s)", width=8, justify="right")
        recent_table.add_column("Error", max_width=40)

        for r in reversed(self.recent_results):
            status_str = "[green]OK[/green]" if r["success"] else f"[red]{r.get('error_type', 'FAIL')}[/red]"
            recent_table.add_row(
                r["pii"][:24],
                r["file_type"],
                status_str,
                str(r.get("page_count", 0)),
                f"{r.get('processing_time', 0):.1f}",
                (r.get("error_message") or "")[:40],
            )

        return status_table, recent_table


# ============================================================================
# 메인 실행 로직
# ============================================================================

def main():
    console = Console()

    # ── 입력 CSV 확인 ──
    if not INPUT_CSV.exists():
        console.print(f"[red]ERROR: Input CSV not found: {INPUT_CSV}[/red]")
        sys.exit(1)

    console.print(f"\n[bold cyan]{'='*60}[/bold cyan]")
    console.print(f"[bold]Marker PDF → Markdown Conversion Pipeline[/bold]")
    console.print(f"[bold cyan]{'='*60}[/bold cyan]")
    console.print(f"  Environment : {ENV_NAME.upper()}")
    console.print(f"  GPU Profile : {GPU_PROFILE['vram_gb']}GB × {GPU_PROFILE['num_gpus']} GPU")
    console.print(f"  Workers     : {TOTAL_WORKERS}")
    console.print(f"  Input CSV   : {INPUT_CSV}")
    console.print(f"  Output Dir  : {OUTPUT_DIR}")
    console.print()

    # ── 태스크 목록 생성 (Resume: 이미 완료된 건 스킵) ──
    console.print("[yellow]Building task list (checking resume state)...[/yellow]")

    df_input = pd.read_csv(INPUT_CSV, dtype=str).fillna("")
    total_possible = len(df_input)
    tasks = build_task_list(INPUT_CSV, OUTPUT_DIR)
    skipped = total_possible - len(tasks)  # 대략적 스킵 수 (supp 다중 파일로 정확하지 않을 수 있음)

    # 정확한 스킵 수 계산을 위해 전체 태스크 수도 구함
    all_tasks_no_resume = build_task_list_no_resume(INPUT_CSV, OUTPUT_DIR)
    actual_skipped = len(all_tasks_no_resume) - len(tasks)

    console.print(f"  Total files to convert : [bold]{len(tasks)}[/bold]")
    console.print(f"  Already done (skipped) : [bold green]{actual_skipped}[/bold green]")

    if len(tasks) == 0:
        console.print("[green]All files already converted. Nothing to do.[/green]")
        return

    # ── Ray 초기화 ──
    console.print("\n[yellow]Initializing Ray...[/yellow]")
    if ray.is_initialized():
        ray.shutdown()

    ray.init(
        num_gpus=GPU_PROFILE["num_gpus"],
        ignore_reinit_error=True,
        log_to_driver=False,
    )
    console.print(f"[green]Ray initialized: {ray.cluster_resources()}[/green]")

    # ── Worker Actor Pool 생성 ──
    console.print(f"[yellow]Creating {TOTAL_WORKERS} MarkerWorker actors (loading models)...[/yellow]")
    workers = []
    for i in range(TOTAL_WORKERS):
        # 각 actor에 GPU fraction 할당
        worker = MarkerWorker.options(
            num_gpus=GPU_FRACTION,
            max_concurrency=1,  # actor 내부 직렬 실행
        ).remote(worker_id=i, gpu_fraction=GPU_FRACTION)
        workers.append(worker)

    # ── 워밍업: ping 메서드로 actor 생존 확인 (파일 변환 없음) ──
    console.print("[yellow]Waiting for model loading (this may take 1-3 min)...[/yellow]")
    time.sleep(10)  # 모델 로딩 대기

    try:
        ray.get([w.ping.remote() for w in workers], timeout=600)
        console.print("[green]All workers ready.[/green]\n")
    except Exception as e:
        console.print(f"[red]Worker init failed: {e}[/red]")
        sys.exit(1)

    # ── 작업 분배 및 실행 ──
    dashboard = Dashboard(total_tasks=len(tasks), total_skipped=actual_skipped)
    results_buffer: List[dict] = []
    FLUSH_INTERVAL = 50  # 매 50건마다 메타데이터 CSV flush

    # 라운드로빈으로 작업을 worker에 할당
    pending: Dict[ray.ObjectRef, Tuple[int, ConversionTask]] = {}
    task_iter = iter(tasks)
    finished = False

    def submit_next(worker_idx: int):
        """다음 태스크를 worker에 제출"""
        nonlocal finished
        try:
            task = next(task_iter)
            ref = workers[worker_idx].convert.remote(
                pii=task.pii,
                pdf_path=task.pdf_path,
                output_md_path=task.output_md_path,
                output_img_dir=task.output_img_dir,
                file_type=task.file_type,
                supp_index=task.supp_index,
            )
            pending[ref] = (worker_idx, task)
        except StopIteration:
            finished = True

    # 초기 작업 제출: 각 worker에 1개씩
    for i in range(min(TOTAL_WORKERS, len(tasks))):
        submit_next(i)

    # ── Rich Live 대시보드로 진행률 표시 ──
    with Live(console=console, refresh_per_second=2) as live:
        while pending:
            # 완료된 작업 수거
            ready, _ = ray.wait(list(pending.keys()), num_returns=1, timeout=5.0)

            for ref in ready:
                worker_idx, task = pending.pop(ref)
                try:
                    result = ray.get(ref)
                except Exception as e:
                    result = {
                        "pii": task.pii,
                        "file_type": task.file_type,
                        "pdf_filename": Path(task.pdf_path).name,
                        "success": False,
                        "page_count": 0,
                        "processing_time": 0,
                        "error_type": "RAY_ERROR",
                        "error_message": str(e)[:400],
                        "output_md_path": None,
                    }

                # 대시보드 업데이트
                dashboard.update(result)
                results_buffer.append(result)

                # 실패 시 로깅
                if not result["success"]:
                    logger.error(
                        f"FAIL [{result.get('error_type')}] "
                        f"pii={result['pii']} file={result['pdf_filename']} "
                        f"err={result.get('error_message', '')[:200]}"
                    )

                # 주기적 CSV flush
                if len(results_buffer) >= FLUSH_INTERVAL:
                    flush_results = [
                        ConversionResult(**{k: v for k, v in r.items() if k != "metadata"})
                        for r in results_buffer
                    ]
                    append_meta_csv(META_CSV, flush_results)
                    results_buffer.clear()

                # 해당 worker에 새 작업 할당
                if not finished:
                    submit_next(worker_idx)

            # 대시보드 렌더링
            status_tbl, recent_tbl = dashboard.render()
            layout = Table.grid(expand=True)
            layout.add_row(status_tbl)
            layout.add_row(recent_tbl)
            live.update(Panel(layout, title="[bold]PDF Conversion Dashboard[/bold]"))

    # ── 잔여 결과 flush ──
    if results_buffer:
        flush_results = [
            ConversionResult(**{k: v for k, v in r.items() if k != "metadata"})
            for r in results_buffer
        ]
        append_meta_csv(META_CSV, flush_results)
        results_buffer.clear()

    # ── 최종 요약 ──
    console.print(f"\n[bold cyan]{'='*60}[/bold cyan]")
    console.print("[bold]Conversion Complete![/bold]")
    console.print(f"  Total Processed : {dashboard.completed}")
    console.print(f"  Success         : [green]{dashboard.success_count}[/green]")
    console.print(f"  Failed          : [red]{dashboard.fail_count}[/red]")
    console.print(f"    - OOM         : {dashboard.oom_count}")
    console.print(f"    - Corrupt PDF : {dashboard.corrupt_count}")
    console.print(f"  Metadata CSV    : {META_CSV}")
    console.print(f"  Error Log       : {ERROR_LOG}")
    console.print(f"[bold cyan]{'='*60}[/bold cyan]\n")

    # ── Ray 종료 ──
    ray.shutdown()


def build_task_list_no_resume(csv_path: Path, output_dir: Path) -> List[ConversionTask]:
    """Resume 체크 없이 전체 태스크 목록 생성 (스킵 수 계산용)"""
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    tasks = []

    for _, row in df.iterrows():
        pii = row["pii"].strip()
        if not pii:
            continue

        pii_output_dir = output_dir / pii

        pdf_folder = row.get("pdf_folder", "").strip()
        pdf_filename = row.get("pdf_filename", "").strip()
        if pdf_filename:
            pdf_path = str(ROOT_DIR / pdf_folder / pdf_filename) if pdf_folder else str(ROOT_DIR / pdf_filename)
            md_path = str(pii_output_dir / "main.md")
            img_dir = str(pii_output_dir / "images" / "main")
            tasks.append(ConversionTask(
                pii=pii, pdf_path=pdf_path, output_md_path=md_path,
                output_img_dir=img_dir, file_type="main",
            ))

        supp_folder = row.get("supp_folder", "").strip()
        supp_filename = row.get("supp_filename", "").strip()
        if supp_filename:
            supp_files = [f.strip() for f in supp_filename.split(";") if f.strip()]
            for sf in supp_files:
                supp_path = str(ROOT_DIR / "supplementary_files" / supp_folder / sf) if supp_folder else str(ROOT_DIR / sf)
                mmc_idx = extract_mmc_index(sf) or "1"
                md_path = str(pii_output_dir / f"mmc{mmc_idx}.md")
                img_dir = str(pii_output_dir / "images" / f"mmc{mmc_idx}")
                tasks.append(ConversionTask(
                    pii=pii, pdf_path=supp_path, output_md_path=md_path,
                    output_img_dir=img_dir, file_type="supp", supp_index=mmc_idx,
                ))

    return tasks


# ============================================================================
# Entrypoint
# ============================================================================

if __name__ == "__main__":
    main()
