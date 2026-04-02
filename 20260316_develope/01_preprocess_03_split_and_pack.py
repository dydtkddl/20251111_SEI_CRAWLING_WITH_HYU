# 01_preprocess_03_split_and_pack.py
"""
PDF + Supp PDF를 환경별로 가중 분할 후 tar.gz 압축
- PII 단위 분할 (같은 논문이 쪼개지지 않음)
- main PDF + supp PDF 세트를 각 환경별 tar.gz로 압축
- Rich 실시간 모니터링 + 병렬 압축

Output (스크립트와 동일 디렉토리):
  01_preprocess_03_ulsan_cluster.csv
  01_preprocess_03_my_pc_5090.csv
  01_preprocess_03_hanyang_4090.csv
  01_preprocess_03_colab_a100.csv
  01_preprocess_03_ulsan_cluster.tar.gz
  01_preprocess_03_my_pc_5090.tar.gz
  01_preprocess_03_hanyang_4090.tar.gz
  01_preprocess_03_colab_a100.tar.gz
"""

import csv
import math
import tarfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import (
    Progress, BarColumn, TextColumn,
    TimeElapsedColumn, TimeRemainingColumn,
    MofNCompleteColumn, SpinnerColumn,
)
from rich.table import Table

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE        = Path("/mnt/d/20251111_SEI_CRAWLING_WITH_HYU/20260105_develope")
PDF_DIR     = BASE / "pdfs"
SUPP_DIR    = BASE / "supplementary_files"
IN_CSV      = Path("./01_preprocess_02.5.csv")
HERE        = Path(__file__).parent          # 스크립트와 동일 디렉토리

# ── 환경별 가중치 룩업테이블 ──────────────────────────────────────────────────
#  GPU 처리속도 기준: VRAM × 처리효율 × 세션안정성
#
#  환경              GPU          VRAM    안정성   가중치
#  ulsan_cluster     L4 × 2      48GB    ★★★★★   2.5
#  my_pc_5090        RTX 5090    32GB    ★★★★★   2.0
#  hanyang_4090      RTX 4090    24GB    ★★★★★   1.8
#  colab_a100        A100        40GB    ★★★      1.5  ← 세션제한으로 감점
ENVS = [
    # (env_name,          weight)
    ("ulsan_cluster",     2.5),
    ("my_pc_5090",        2.0),
    ("hanyang_4090",      1.8),
    ("colab_a100",        1.5),
]
TOTAL_WEIGHT = sum(w for _, w in ENVS)

PREFIX      = "01_preprocess_03"
SKIP_EXTS   = {".xlsx", ".csv", ".zip", ".pptx"}

console     = Console()

# ══════════════════════════════════════════════════════════════════════════════
# 1. 데이터 수집
# ══════════════════════════════════════════════════════════════════════════════
def collect_rows():
    console.print("[cyan][1] 대상 PII 및 파일 수집 중...[/cyan]")

    with open(IN_CSV, encoding="utf-8-sig") as f:
        target_piis = [row["pii"] for row in csv.DictReader(f)]

    pii_to_rows     = defaultdict(list)
    no_pdf_piis     = []
    no_supp_piis    = []

    for pii in target_piis:
        pdf_file = PDF_DIR / f"1-s2.0-{pii}-main.pdf"
        if not pdf_file.exists():
            no_pdf_piis.append(pii)
            continue

        supp_folder = SUPP_DIR / pii
        supp_pdfs   = []
        if supp_folder.exists():
            for f in sorted(supp_folder.iterdir()):
                if f.is_file() and f.suffix.lower() == ".pdf":
                    supp_pdfs.append(f)

        if not supp_pdfs:
            no_supp_piis.append(pii)
            pii_to_rows[pii].append({
                "pii"           : pii,
                "pdf_folder"    : "pdfs",
                "pdf_filename"  : pdf_file.name,
                "supp_folder"   : pii,
                "supp_filename" : "",
                "_pdf_path"     : pdf_file,
                "_supp_path"    : None,
            })
        else:
            for sp in supp_pdfs:
                pii_to_rows[pii].append({
                    "pii"           : pii,
                    "pdf_folder"    : "pdfs",
                    "pdf_filename"  : pdf_file.name,
                    "supp_folder"   : pii,
                    "supp_filename" : sp.name,
                    "_pdf_path"     : pdf_file,
                    "_supp_path"    : sp,
                })

    total_piis = len(pii_to_rows)
    console.print(f"  유효 PII          : [green]{total_piis}[/green]")
    console.print(f"  main PDF 없음     : [yellow]{len(no_pdf_piis)}[/yellow]")
    console.print(f"  supp PDF 없음     : [yellow]{len(no_supp_piis)}[/yellow]")
    return pii_to_rows

# ══════════════════════════════════════════════════════════════════════════════
# 2. 가중 분할
# ══════════════════════════════════════════════════════════════════════════════
def split_piis(pii_to_rows):
    console.print("[cyan][2] 가중 분할 중...[/cyan]")

    unique_piis = list(pii_to_rows.keys())
    total       = len(unique_piis)

    splits      = []
    allocated   = 0
    for i, (name, weight) in enumerate(ENVS):
        if i == len(ENVS) - 1:
            count = total - allocated
        else:
            count = math.floor(total * (weight / TOTAL_WEIGHT))
            allocated += count
        splits.append((name, weight, count))

    # PII 배정
    env_piis = {}
    idx = 0
    for name, weight, count in splits:
        env_piis[name] = unique_piis[idx: idx + count]
        idx += count
        pct = weight / TOTAL_WEIGHT * 100
        console.print(
            f"  [bold]{name:<20}[/bold] : "
            f"[green]{count:>4}[/green] PII  "
            f"([yellow]{pct:.1f}%[/yellow])"
        )

    return env_piis

# ══════════════════════════════════════════════════════════════════════════════
# 3. CSV 저장
# ══════════════════════════════════════════════════════════════════════════════
def save_csvs(env_piis, pii_to_rows):
    console.print("[cyan][3] CSV 저장 중...[/cyan]")

    fieldnames  = ["pii", "pdf_folder", "pdf_filename", "supp_folder", "supp_filename"]
    env_csv     = {}

    for name, piis in env_piis.items():
        out_path    = HERE / f"{PREFIX}_{name}.csv"
        env_rows    = []
        for pii in piis:
            for r in pii_to_rows[pii]:
                env_rows.append({k: v for k, v in r.items() if not k.startswith("_")})

        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(env_rows)

        env_csv[name] = out_path
        console.print(f"  ✅ [cyan]{out_path.name}[/cyan]  ({len(env_rows)}행)")

    return env_csv

# ══════════════════════════════════════════════════════════════════════════════
# 4. 병렬 압축 (Rich 모니터링)
# ══════════════════════════════════════════════════════════════════════════════
def pack_all(env_piis, pii_to_rows):
    console.print("[cyan][4] tar.gz 압축 시작...[/cyan]\n")

    # 환경별 파일 목록 미리 계산
    env_file_lists = {}
    for name, piis in env_piis.items():
        files = []
        for pii in piis:
            for r in pii_to_rows[pii]:
                # main pdf
                p = r["_pdf_path"]
                if p and p not in files:
                    files.append(p)
                # supp pdf
                s = r["_supp_path"]
                if s:
                    files.append(s)
        env_file_lists[name] = files

    # ── Progress 구성 ─────────────────────────────────────────────────────────
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description:<22}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("[yellow]{task.percentage:>5.1f}%"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=4,
    )

    task_ids    = {}
    for name, files in env_file_lists.items():
        task_ids[name] = progress.add_task(name, total=len(files))

    # 상태 테이블용 락
    status      = {name: "대기중" for name in env_piis}
    sizes       = {name: 0       for name in env_piis}
    status_lock = threading.Lock()

    def make_status_table():
        t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        t.add_column("환경",        width=22)
        t.add_column("파일수",      width=8)
        t.add_column("상태",        width=10)
        t.add_column("압축크기",    width=12)
        for name, files in env_file_lists.items():
            with status_lock:
                st   = status[name]
                sz   = sizes[name]
            color = {"완료": "green", "압축중": "yellow", "대기중": "dim", "오류": "red"}.get(st, "white")
            sz_str = f"{sz/1024/1024:.1f}MB" if sz else "-"
            t.add_row(
                f"[bold]{name}[/bold]",
                str(len(files)),
                f"[{color}]{st}[/{color}]",
                sz_str,
            )
        return Panel(t, title="압축 현황", border_style="cyan")

    def pack_one_env(name):
        files    = env_file_lists[name]
        out_path = HERE / f"{PREFIX}_{name}.tar.gz"
        tid      = task_ids[name]

        with status_lock:
            status[name] = "압축중"

        try:
            with tarfile.open(out_path, "w:gz") as tar:
                for fpath in files:
                    if not fpath.exists():
                        progress.advance(tid)
                        continue

                    # 압축 내부 구조:
                    #   pdfs/{pdf_filename}
                    #   supplementary_files/{pii}/{supp_filename}
                    if fpath.parent == PDF_DIR:
                        arcname = f"pdfs/{fpath.name}"
                    else:
                        pii     = fpath.parent.name
                        arcname = f"supplementary_files/{pii}/{fpath.name}"

                    tar.add(fpath, arcname=arcname)
                    progress.advance(tid)

            with status_lock:
                status[name] = "완료"
                sizes[name]  = out_path.stat().st_size

        except Exception as e:
            with status_lock:
                status[name] = "오류"
            console.print(f"[red]❌ {name} 오류: {e}[/red]")

    # ── 병렬 실행 (환경 4개 동시) ─────────────────────────────────────────────
    try:
        with Live(console=console, refresh_per_second=4) as live:
            with ThreadPoolExecutor(max_workers=len(ENVS)) as executor:
                futures = {executor.submit(pack_one_env, name): name for name in env_piis}

                while not all(f.done() for f in futures):
                    layout = Layout()
                    layout.split_column(
                        Layout(make_status_table(),      size=8),
                        Layout(progress,                 size=len(ENVS) + 3),
                    )
                    live.update(layout)
                    threading.Event().wait(0.25)

                # 마지막 갱신
                layout = Layout()
                layout.split_column(
                    Layout(make_status_table(),  size=8),
                    Layout(progress,             size=len(ENVS) + 3),
                )
                live.update(layout)

    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️  Ctrl+C — 중단됨[/yellow]")

# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
def main():
    console.print(Panel(
        f"[bold]SEI 환경별 분할 & 압축[/bold]\n"
        f"시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"출력: [cyan]{HERE}[/cyan]",
        border_style="green"
    ))

    pii_to_rows = collect_rows()
    env_piis    = split_piis(pii_to_rows)
    save_csvs(env_piis, pii_to_rows)
    pack_all(env_piis, pii_to_rows)

    console.print(Panel(
        "[bold green]✅ 완료![/bold green]\n"
        + "\n".join(
            f"  [cyan]{HERE / f'{PREFIX}_{name}.tar.gz'}[/cyan]"
            for name, _ in ENVS
        ),
        border_style="green"
    ))

if __name__ == "__main__":
    main()

