# 01_preprocess_02_convert_supp_to_pdf_parallel.py
"""
병렬 LibreOffice PDF 변환 (CPU 80% 제한)
- 이미 변환된 파일 자동 스킵
- Rich 실시간 모니터링
- Ctrl+C graceful 종료
"""

import csv
import subprocess
import shutil
import time
import os
import signal
import sys
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    Progress, SpinnerColumn, BarColumn,
    TextColumn, TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn
)
from rich.layout import Layout
from rich.text import Text
import threading

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
SUPP_DIR    = Path("/mnt/d/20251111_SEI_CRAWLING_WITH_HYU/20260105_develope/supplementary_files")
IN_CSV      = Path("01_preprocess_01.csv")
REPORT_CSV  = Path("01_preprocess_02_convert_report.csv")
TARGET_EXTS = {".docx", ".doc", ".rtf"}
SOFFICE     = shutil.which("libreoffice") or shutil.which("soffice")

# CPU 80% 사용
N_WORKERS = max(1, int(cpu_count() * 0.8))

# ── 전역 상태 (스레드 공유) ───────────────────────────────────────────────────
console     = Console()
results     = []          # 완료된 결과 누적
results_lock = threading.Lock()
shutdown_flag = threading.Event()
def convert_one(args):
    pii, src_str, dst_str, soffice = args
    src = Path(src_str)
    dst = Path(dst_str)

    if dst.exists():
        return {"pii": pii, "src": src.name, "status": "SKIP", "detail": "already exists", "elapsed": 0}

    import tempfile, hashlib
    uid         = hashlib.md5(src_str.encode()).hexdigest()[:8]
    profile_dir = Path(tempfile.gettempdir()) / f"lo_profile_{uid}"
    profile_dir.mkdir(exist_ok=True)
    profile_url = f"file://{profile_dir}"

    def run_convert(input_path, out_dir):
        return subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile_url}",
                "--headless",
                "--norestore",
                "--nofirststartwizard",
                "--convert-to", "pdf",
                "--outdir", str(out_dir),
                str(input_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

    t = time.time()
    try:
        # ── 1차 시도: 원래 경로 ───────────────────────────────────────────────
        proc = run_convert(src, src.parent)
        elapsed = round(time.time() - t, 2)

        if proc.returncode == 0 and dst.exists():
            return {"pii": pii, "src": src.name, "status": "OK",
                    "detail": f"{round(dst.stat().st_size/1024,1)}KB", "elapsed": elapsed}

        # ── 2차 시도: /tmp 경유 (경로 길이 문제 fallback) ────────────────────
        tmp_dir  = Path(tempfile.gettempdir())
        tmp_src  = tmp_dir / f"{uid}_{src.name}"
        tmp_dst  = tmp_src.with_suffix(".pdf")

        shutil.copy2(src, tmp_src)
        proc2 = run_convert(tmp_src, tmp_dir)
        elapsed = round(time.time() - t, 2)

        if proc2.returncode == 0 and tmp_dst.exists():
            shutil.move(str(tmp_dst), str(dst))   # /tmp → 원래 위치로 이동
            return {"pii": pii, "src": src.name, "status": "OK(fallback)",
                    "detail": f"{round(dst.stat().st_size/1024,1)}KB", "elapsed": elapsed}

        detail = proc2.stderr.strip()[:200] if proc2.stderr else "dst not created"
        return {"pii": pii, "src": src.name, "status": "FAIL", "detail": detail, "elapsed": elapsed}

    except subprocess.TimeoutExpired:
        return {"pii": pii, "src": src.name, "status": "TIMEOUT", "detail": "exceeded 120s", "elapsed": 120}
    except Exception as e:
        return {"pii": pii, "src": src.name, "status": "ERROR", "detail": str(e)[:200],
                "elapsed": round(time.time() - t, 2)}
    finally:
        # 임시파일 정리
        shutil.rmtree(profile_dir, ignore_errors=True)
        tmp_src_maybe = Path(tempfile.gettempdir()) / f"{uid}_{src.name}"
        tmp_src_maybe.unlink(missing_ok=True)
        tmp_dst_maybe = tmp_src_maybe.with_suffix(".pdf")
        tmp_dst_maybe.unlink(missing_ok=True)


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    if not SOFFICE:
        console.print("[red]❌ LibreOffice를 찾을 수 없습니다.[/red]")
        sys.exit(1)

    # ── 대상 파일 수집 ────────────────────────────────────────────────────────
    console.print(f"[cyan]전수조사 중...[/cyan]")

    with open(IN_CSV, encoding="utf-8-sig") as f:
        target_piis = set(row["pii"] for row in csv.DictReader(f))

    tasks        = []   # 변환할 것
    already_done = []   # 이미 완료된 것
    skip_ext     = []   # 대상 아닌 확장자

    for pii in sorted(target_piis):
        folder = SUPP_DIR / pii
        if not folder.exists():
            continue
        for src in sorted(folder.iterdir()):
            if not src.is_file():
                continue
            ext = src.suffix.lower()
            dst = src.with_suffix(".pdf")
            if ext in TARGET_EXTS:
                if dst.exists():
                    already_done.append({"pii": pii, "src": src.name,
                                         "status": "SKIP", "detail": "already exists", "elapsed": 0})
                else:
                    tasks.append((pii, str(src), str(dst), SOFFICE))
            else:
                skip_ext.append({"pii": pii, "src": src.name,
                                  "status": "SKIP_EXT", "detail": ext, "elapsed": 0})

    total      = len(tasks)
    pre_done   = len(already_done)

    console.print(f"[green]변환 대상  : {total}[/green]")
    console.print(f"[yellow]이미 완료  : {pre_done}[/yellow]")
    console.print(f"[blue]워커 수    : {N_WORKERS} (CPU {cpu_count()}코어 × 80%)[/blue]")
    console.print()

    if total == 0:
        console.print("[green]✅ 변환할 파일이 없습니다.[/green]")
        save_report(already_done + skip_ext)
        return

    # ── Rich Progress 구성 ────────────────────────────────────────────────────
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TextColumn("[yellow]{task.percentage:>5.1f}%"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=4,
    )

    task_id = progress.add_task("PDF 변환", total=total)

    # 최근 결과 로그 (최대 12줄)
    recent_log = []
    log_lock   = threading.Lock()

    ok_count   = pre_done
    fail_count = 0
    counter_lock = threading.Lock()

    STATUS_COLOR = {"OK": "green", "FAIL": "red", "TIMEOUT": "yellow",
                    "ERROR": "red", "SKIP": "dim"}

    def make_panel():
        # 통계 테이블
        stat = Table.grid(padding=(0, 2))
        stat.add_column(style="bold")
        stat.add_column()
        stat.add_row("✅ 완료",   f"[green]{ok_count}[/green]")
        stat.add_row("❌ 실패",   f"[red]{fail_count}[/red]")
        stat.add_row("⏭  스킵",  f"[dim]{pre_done}[/dim]")
        stat.add_row("🔧 워커",  f"[blue]{N_WORKERS}[/blue]")

        # 최근 로그 테이블
        log_table = Table(show_header=True, header_style="bold magenta",
                          box=None, padding=(0,1))
        log_table.add_column("PII",      style="dim",   width=20)
        log_table.add_column("파일",                    width=36)
        log_table.add_column("상태",                    width=8)
        log_table.add_column("크기/에러",               width=20)
        log_table.add_column("초",                      width=6)

        with log_lock:
            for r in recent_log[-12:]:
                c = STATUS_COLOR.get(r["status"], "white")
                log_table.add_row(
                    r["pii"],
                    r["src"],
                    f"[{c}]{r['status']}[/{c}]",
                    r["detail"][:20],
                    str(r["elapsed"]),
                )

        layout = Layout()
        layout.split_row(
            Layout(Panel(stat,       title="통계",   border_style="cyan"),  ratio=1),
            Layout(Panel(log_table,  title="최근 변환", border_style="magenta"), ratio=3),
        )
        return Panel(layout, title=f"[bold]SEI PDF 변환 | {datetime.now().strftime('%H:%M:%S')}[/bold]",
                     border_style="green")

    # ── 병렬 실행 ─────────────────────────────────────────────────────────────
    all_results = list(already_done + skip_ext)

    try:
        with Live(console=console, refresh_per_second=4) as live:
            with ProcessPoolExecutor(max_workers=N_WORKERS) as executor:
                futures = {executor.submit(convert_one, t): t for t in tasks}

                for future in as_completed(futures):
                    if shutdown_flag.is_set():
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

                    r = future.result()
                    all_results.append(r)

                    with counter_lock:
                        if r["status"] == "OK":
                            ok_count += 1
                        elif r["status"] in ("FAIL", "TIMEOUT", "ERROR"):
                            fail_count += 1

                    with log_lock:
                        recent_log.append(r)

                    progress.advance(task_id)
                    live.update(Layout(
                        make_panel(),
                        name="main"
                    ))

    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️  Ctrl+C 감지 — 현재까지 결과 저장 중...[/yellow]")
        shutdown_flag.set()

    # ── 리포트 저장 ───────────────────────────────────────────────────────────
    save_report(all_results)

    console.print(f"\n[bold green]✅ 완료![/bold green]")
    console.print(f"  변환 성공 : [green]{ok_count}[/green]")
    console.print(f"  변환 실패 : [red]{fail_count}[/red]")
    console.print(f"  리포트    → [cyan]{REPORT_CSV}[/cyan]")


def save_report(rows):
    fieldnames = ["pii", "src", "status", "detail", "elapsed"]
    with open(REPORT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()

