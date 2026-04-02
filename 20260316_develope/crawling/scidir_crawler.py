#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scidir_crawler.py — ScienceDirect PDF & Supplementary Crawler (Enterprise Edition)
===================================================================================

Elsevier ScienceDirect에서 논문 PDF 및 Supplementary 파일을 자동 수집하는
엔터프라이즈급 단일 CLI 크롤러.

Usage
-----
    # 환경 초기화 (설정 파일 생성 + Chrome 감지 + 의존성 점검)
    python scidir_crawler.py init

    # Chrome 디버그 모드 관리
    python scidir_crawler.py chrome --start
    python scidir_crawler.py chrome --stop
    python scidir_crawler.py chrome --status

    # 논문 PDF 다운로드
    python scidir_crawler.py pdf --csv input.csv --col prism_url --save_dir ./pdfs

    # Supplementary 파일 다운로드
    python scidir_crawler.py supp --csv input.csv --col prism_url --save_dir ./supp

    # PDF + Supplementary 동시 다운로드
    python scidir_crawler.py all --csv input.csv

    # 실패 목록 재처리
    python scidir_crawler.py pdf --csv fail_pdf.csv --retry-failed
    python scidir_crawler.py supp --csv fail_supp.csv --retry-failed

    # 이미 받은 파일 무시하고 강제 재다운로드
    python scidir_crawler.py pdf --csv input.csv --force

Requirements
------------
    pip install playwright pandas rich requests
    playwright install chromium

Configuration
-------------
    실행 디렉토리에 ``scidir_config.json`` 이 없으면 ``init`` 명령으로 자동 생성.
    CLI 인자는 JSON 설정을 오버라이드함.

Architecture
------------
    - Config:        JSON 파일 + CLI 오버라이드, dataclass 기반
    - ChromeManager: CDP 포트 점검, subprocess 시작/종료, 주기적 재시작
    - SessionKeeper: storage_state(cookies.json) 저장/복원
    - PDFDownloader: 논문 본문 PDF 다운로드 (Playwright 네이티브 + fallback)
    - SuppDownloader: Supplementary 파일 다운로드
    - Dashboard:     Rich 기반 실시간 모니터링
    - RetryHandler:  에러 분류 + 재시도 데코레이터

Platform Notes
--------------
    Windows/Linux/macOS 모두 지원. Windows 전용 API(ctypes.windll)는 사용하지 않음.
    Chrome 경로 자동 탐지는 OS별 표준 설치 경로를 순회.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import random
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

# ============================================================================
# Constants
# ============================================================================

CONFIG_FILE = "scidir_config.json"
COOKIES_FILE = "cookies.json"
FAIL_PDF_FILE = "fail_pdf.csv"
FAIL_SUPP_FILE = "fail_supp.csv"
SD_BASE_URL = "https://www.sciencedirect.com"
SD_ARTICLE_URL = f"{SD_BASE_URL}/science/article/pii"

DEFAULT_CONFIG: Dict[str, Any] = {
    "chrome": {
        "path": "auto",
        "debug_port": 9222,
        "profile_dir": "./chrome_debug_profile",
        "restart_every_n": 100,
    },
    "download": {
        "save_dir_pdf": "./pdfs",
        "save_dir_supp": "./supplementary_files",
        "timeout_page": 90000,
        "timeout_download": 30000,
        "retry_count": 3,
        "delay_min": 1.5,
        "delay_max": 3.5,
        "post_download_wait": 1.0,
    },
    "log": {
        "level": "INFO",
        "file": "scidir_crawler.log",
    },
}


# ============================================================================
# Error Classification
# ============================================================================

class ErrorType(str, Enum):
    """다운로드 에러 유형 분류."""
    TIMEOUT = "TIMEOUT"
    NOT_FOUND = "NOT_FOUND"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    DOWNLOAD_FAIL = "DOWNLOAD_FAIL"
    CHROME_DEAD = "CHROME_DEAD"
    UNKNOWN = "UNKNOWN"


class LoginRequiredError(Exception):
    """ScienceDirect 로그인이 필요할 때 발생하는 예외."""
    pass


class ChromeDeadError(Exception):
    """Chrome 프로세스가 응답하지 않을 때 발생하는 예외."""
    pass


def classify_error(exc: Exception, url: str = "") -> ErrorType:
    """예외 및 URL 기반 에러 유형 분류."""
    msg = str(exc).lower()
    url_lower = url.lower()

    if "login" in url_lower or "idp" in url_lower or "auth" in url_lower:
        return ErrorType.LOGIN_REQUIRED
    if "timeout" in msg or "timed out" in msg:
        return ErrorType.TIMEOUT
    if "not found" in msg or "404" in msg or "no pdf" in msg.lower():
        return ErrorType.NOT_FOUND
    if "chrome" in msg and ("dead" in msg or "disconnect" in msg or "closed" in msg):
        return ErrorType.CHROME_DEAD
    if "download" in msg:
        return ErrorType.DOWNLOAD_FAIL
    return ErrorType.UNKNOWN


# ============================================================================
# Configuration (dataclass)
# ============================================================================

@dataclass
class ChromeConfig:
    """Chrome 브라우저 관련 설정."""
    path: str = "auto"
    debug_port: int = 9222
    profile_dir: str = "./chrome_debug_profile"
    restart_every_n: int = 100


@dataclass
class DownloadConfig:
    """다운로드 관련 설정."""
    save_dir_pdf: str = "./pdfs"
    save_dir_supp: str = "./supplementary_files"
    timeout_page: int = 90000
    timeout_download: int = 30000
    retry_count: int = 3
    delay_min: float = 1.5
    delay_max: float = 3.5
    post_download_wait: float = 1.0


@dataclass
class LogConfig:
    """로깅 관련 설정."""
    level: str = "INFO"
    file: str = "scidir_crawler.log"


@dataclass
class AppConfig:
    """
    애플리케이션 전체 설정.
    JSON 파일에서 로드 후 CLI 인자로 오버라이드 가능.
    """
    chrome: ChromeConfig = field(default_factory=ChromeConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    log: LogConfig = field(default_factory=LogConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        """딕셔너리에서 AppConfig 생성."""
        chrome_data = data.get("chrome", {})
        download_data = data.get("download", {})
        log_data = data.get("log", {})
        return cls(
            chrome=ChromeConfig(**{k: v for k, v in chrome_data.items()
                                   if k in ChromeConfig.__dataclass_fields__}),
            download=DownloadConfig(**{k: v for k, v in download_data.items()
                                       if k in DownloadConfig.__dataclass_fields__}),
            log=LogConfig(**{k: v for k, v in log_data.items()
                             if k in LogConfig.__dataclass_fields__}),
        )

    def to_dict(self) -> Dict[str, Any]:
        """설정을 딕셔너리로 변환."""
        return {
            "chrome": asdict(self.chrome),
            "download": asdict(self.download),
            "log": asdict(self.log),
        }

    @classmethod
    def load(cls, config_path: Path) -> "AppConfig":
        """JSON 파일에서 설정 로드. 파일이 없으면 기본값 사용."""
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 기본값과 병합 (파일에 누락된 키 보완)
            merged = DEFAULT_CONFIG.copy()
            for section in merged:
                if section in data and isinstance(data[section], dict):
                    merged[section] = {**merged[section], **data[section]}
            return cls.from_dict(merged)
        return cls.from_dict(DEFAULT_CONFIG)

    def save(self, config_path: Path) -> None:
        """설정을 JSON 파일로 저장."""
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    def apply_cli_overrides(self, args: argparse.Namespace) -> None:
        """CLI 인자로 설정 오버라이드."""
        if hasattr(args, "save_dir") and args.save_dir:
            if hasattr(args, "command"):
                if args.command == "pdf":
                    self.download.save_dir_pdf = args.save_dir
                elif args.command == "supp":
                    self.download.save_dir_supp = args.save_dir
                elif args.command == "all":
                    # all 모드에서 save_dir는 pdf 기본으로
                    self.download.save_dir_pdf = args.save_dir


# ============================================================================
# Logging Setup
# ============================================================================

def setup_logging(config: LogConfig) -> logging.Logger:
    """Rich + 파일 핸들러 기반 로거 구성."""
    logger = logging.getLogger("scidir")
    logger.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    logger.handlers.clear()

    # 파일 핸들러
    fh = logging.FileHandler(config.file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    # Rich 콘솔 핸들러 (Dashboard 사용 시 비활성화될 수 있음)
    rh = RichHandler(
        console=Console(stderr=True),
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
    )
    rh.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    logger.addHandler(rh)

    return logger


# ============================================================================
# Chrome Manager
# ============================================================================

class ChromeManager:
    """
    Chrome 브라우저 프로세스 관리.
    - 경로 자동 탐지 (Windows/Linux/macOS)
    - CDP 포트 점검
    - subprocess 시작/종료
    - 주기적 재시작
    """

    # OS별 Chrome 표준 설치 경로
    CHROME_PATHS: Dict[str, List[str]] = {
        "Windows": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        ],
        "Linux": [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ],
        "Darwin": [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ],
    }

    def __init__(self, config: ChromeConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self._process: Optional[subprocess.Popen] = None
        self._chrome_path: Optional[str] = None

    def find_chrome(self) -> str:
        """Chrome 실행 파일 경로를 탐지하여 반환."""
        if self.config.path != "auto":
            p = Path(self.config.path)
            if p.exists():
                return str(p)
            raise FileNotFoundError(f"Specified Chrome path not found: {self.config.path}")

        system = platform.system()
        candidates = self.CHROME_PATHS.get(system, [])

        for path_str in candidates:
            p = Path(path_str)
            if p.exists():
                self.logger.info(f"Chrome found: {p}")
                return str(p)

        raise FileNotFoundError(
            f"Chrome not found on {system}. "
            f"Searched: {candidates}. "
            f"Set 'chrome.path' in {CONFIG_FILE} manually."
        )

    def is_port_open(self) -> bool:
        """CDP 포트가 열려 있는지 소켓으로 확인."""
        try:
            with socket.create_connection(
                ("127.0.0.1", self.config.debug_port), timeout=2
            ):
                return True
        except (ConnectionRefusedError, OSError, socket.timeout):
            return False

    def start(self) -> None:
        """Chrome을 디버그 모드로 시작."""
        if self.is_port_open():
            self.logger.info(
                f"CDP port {self.config.debug_port} already open — reusing existing Chrome."
            )
            return

        chrome_path = self.find_chrome()
        self._chrome_path = chrome_path
        profile_dir = Path(self.config.profile_dir).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            chrome_path,
            f"--remote-debugging-port={self.config.debug_port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        self.logger.info(f"Starting Chrome: {' '.join(cmd)}")

        # stdout/stderr를 DEVNULL로 보내서 프로세스가 터미널에 쓰레기를 출력하지 않게 함
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 포트 열릴 때까지 대기 (최대 15초)
        for _ in range(30):
            if self.is_port_open():
                self.logger.info(
                    f"Chrome started (PID={self._process.pid}, "
                    f"port={self.config.debug_port})."
                )
                return
            time.sleep(0.5)

        raise RuntimeError(
            f"Chrome started but CDP port {self.config.debug_port} "
            f"did not open within 15 seconds."
        )

    def stop(self) -> None:
        """Chrome 프로세스를 종료."""
        if self._process and self._process.poll() is None:
            self.logger.info(f"Stopping Chrome (PID={self._process.pid})...")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self.logger.info("Chrome stopped.")
            self._process = None
        else:
            # 외부에서 시작된 Chrome은 직접 kill하지 않음
            self.logger.info("No managed Chrome process to stop.")

    def restart(self) -> None:
        """Chrome 재시작 (메모리 누수 방지용)."""
        self.logger.warning("[CHROME RESTART] Restarting Chrome for memory cleanup...")
        self.stop()
        time.sleep(2)
        self.start()
        time.sleep(2)  # 새 프로세스 안정화 대기

    def status(self) -> Dict[str, Any]:
        """현재 Chrome 상태 정보 반환."""
        port_open = self.is_port_open()
        managed_alive = self._process is not None and self._process.poll() is None

        try:
            chrome_path = self.find_chrome()
        except FileNotFoundError:
            chrome_path = "NOT FOUND"

        return {
            "chrome_path": chrome_path,
            "cdp_port": self.config.debug_port,
            "port_open": port_open,
            "managed_process": managed_alive,
            "pid": self._process.pid if managed_alive else None,
            "profile_dir": str(Path(self.config.profile_dir).resolve()),
        }


# ============================================================================
# Session Keeper (cookies / storage_state)
# ============================================================================

class SessionKeeper:
    """
    Playwright storage_state(쿠키 + localStorage) 저장/복원.
    CDP 연결 시 context 생성 때 주입하여 로그인 세션 유지.
    """

    def __init__(self, cookies_path: Path, logger: logging.Logger):
        self.cookies_path = cookies_path
        self.logger = logger

    def save(self, context: Any) -> None:
        """현재 context의 storage_state를 파일로 저장."""
        try:
            state = context.storage_state()
            with open(self.cookies_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            self.logger.debug(f"Session saved → {self.cookies_path}")
        except Exception as e:
            self.logger.warning(f"Failed to save session: {e}")

    def get_storage_state(self) -> Optional[str]:
        """저장된 storage_state 파일 경로 반환 (없으면 None)."""
        if self.cookies_path.exists() and self.cookies_path.stat().st_size > 10:
            return str(self.cookies_path)
        return None


# ============================================================================
# URL / PII Utilities
# ============================================================================

def normalize_url(raw: Any) -> Optional[str]:
    """
    다양한 형태의 입력(prism_url, link_self, pii)을
    ScienceDirect PII URL로 정규화.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    if not s:
        return None
    # 이미 완전한 URL이면 PII만 추출 후 재조합
    pii = s.rstrip("/").split("/")[-1]
    # PII 정제: 하이픈/괄호 제거 가능하나 ScienceDirect는 하이픈 포함 PII도 수용
    return f"{SD_ARTICLE_URL}/{pii}"


def extract_pii(url: str) -> str:
    """URL에서 PII 추출."""
    return url.rstrip("/").split("/")[-1]


def normalize_pii_for_filename(pii: str) -> str:
    """PII를 파일명 안전 형태로 변환 (하이픈 제거, 괄호 제거)."""
    return re.sub(r"[^a-zA-Z0-9]", "", pii)


def make_pdf_filename(pii: str) -> str:
    """논문 본문 PDF 파일명 생성."""
    clean = normalize_pii_for_filename(pii)
    return f"1-s2.0-{clean}-main.pdf"


# ============================================================================
# Resume Check
# ============================================================================

def get_existing_pdfs(save_dir: Path) -> set:
    """저장 디렉토리에서 이미 다운로드된 PII 집합을 반환."""
    existing = set()
    if not save_dir.exists():
        return existing
    for f in save_dir.iterdir():
        if f.suffix.lower() == ".pdf" and f.name.startswith("1-s2.0-"):
            # 1-s2.0-{PII}-main.pdf 에서 PII 추출
            m = re.match(r"1-s2\.0-(.+?)-main\.pdf", f.name, re.IGNORECASE)
            if m:
                existing.add(m.group(1))
    return existing


def get_existing_supp_piis(save_dir: Path) -> set:
    """Supplementary 저장 디렉토리에서 이미 다운로드된 PII 집합 반환."""
    existing = set()
    if not save_dir.exists():
        return existing
    for d in save_dir.iterdir():
        if d.is_dir() and any(d.iterdir()):
            existing.add(d.name)
    return existing


# ============================================================================
# Retry Decorator
# ============================================================================

def with_retry(
    max_retries: int = 3,
    delay_base: float = 2.0,
    logger: Optional[logging.Logger] = None,
) -> Callable:
    """
    재시도 데코레이터.
    LoginRequiredError는 재시도하지 않고 즉시 전파.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except LoginRequiredError:
                    raise  # 로그인 필요 → 즉시 전파
                except ChromeDeadError:
                    raise  # Chrome 죽음 → 상위에서 재시작 처리
                except Exception as e:
                    last_exc = e
                    if logger:
                        logger.warning(
                            f"[RETRY {attempt}/{max_retries}] "
                            f"{func.__name__}: {type(e).__name__}: {str(e)[:150]}"
                        )
                    if attempt < max_retries:
                        time.sleep(delay_base * attempt + random.uniform(0, 1))
            raise last_exc  # type: ignore
        return wrapper
    return decorator


# ============================================================================
# Dashboard (Rich)
# ============================================================================

class LogBuffer:
    """최근 로그 라인을 고정 크기 버퍼로 관리."""

    def __init__(self, max_lines: int = 12):
        self.max_lines = max_lines
        self._lines: List[Tuple[str, str]] = []  # (timestamp, message)

    def add(self, msg: str, style: str = "white") -> None:
        """로그 라인 추가."""
        ts = datetime.now().strftime("%H:%M:%S")
        self._lines.append((f"[dim]{ts}[/dim]", f"[{style}]{msg}[/{style}]"))
        if len(self._lines) > self.max_lines:
            self._lines = self._lines[-self.max_lines:]

    def render(self) -> str:
        """Rich 마크업 문자열로 반환."""
        return "\n".join(f"  {ts}  {msg}" for ts, msg in self._lines)


class Dashboard:
    """Rich 기반 실시간 다운로드 현황 대시보드."""

    def __init__(
        self,
        total: int,
        skipped: int,
        mode: str,
        config: AppConfig,
        chrome_status: Dict[str, Any],
    ):
        self.total = total
        self.skipped = skipped
        self.mode = mode
        self.config = config
        self.chrome_status = chrome_status

        self.completed = 0
        self.success = 0
        self.failed = 0
        self.skip_during = 0  # 다운로드 중 스킵 (이미 존재)
        self.current_url = ""
        self.start_time = time.time()
        self.log_buf = LogBuffer(max_lines=12)

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
        )
        self.task_id = self.progress.add_task("Downloading", total=total)

    def update_success(self, pii: str, filename: str) -> None:
        """성공 건 업데이트."""
        self.completed += 1
        self.success += 1
        self.progress.update(self.task_id, completed=self.completed)
        self.log_buf.add(f"[OK] {pii} → {filename}", "green")

    def update_fail(self, pii: str, error_type: ErrorType, msg: str) -> None:
        """실패 건 업데이트."""
        self.completed += 1
        self.failed += 1
        self.progress.update(self.task_id, completed=self.completed)
        self.log_buf.add(f"[FAIL:{error_type.value}] {pii}: {msg[:80]}", "red")

    def update_skip(self, pii: str) -> None:
        """스킵 건 업데이트."""
        self.completed += 1
        self.skip_during += 1
        self.progress.update(self.task_id, completed=self.completed)
        self.log_buf.add(f"[SKIP] {pii} (already exists)", "dim")

    def update_chrome_restart(self) -> None:
        """Chrome 재시작 이벤트."""
        self.log_buf.add(
            "★ Chrome restarted for memory cleanup ★", "bold yellow"
        )

    def set_current(self, url: str) -> None:
        """현재 처리 중인 URL 설정."""
        self.current_url = url

    def render(self) -> Panel:
        """대시보드 전체를 Panel로 렌더링."""
        elapsed = time.time() - self.start_time
        rate = self.completed / max(elapsed, 0.1)
        remaining = (self.total - self.completed) / max(rate, 0.001)

        # ── 상단: 설정 정보 ──
        chrome_icon = "[green]●[/green]" if self.chrome_status.get("port_open") else "[red]●[/red]"
        config_lines = (
            f"  Mode: [bold]{self.mode.upper()}[/bold]   "
            f"Chrome: {chrome_icon} port {self.config.chrome.debug_port}   "
            f"PDF: {self.config.download.save_dir_pdf}   "
            f"Supp: {self.config.download.save_dir_supp}"
        )
        header = Panel(config_lines, title="[bold cyan]Configuration[/bold cyan]", border_style="cyan")

        # ── 중단: 진행률 ──
        pii_display = extract_pii(self.current_url) if self.current_url else "—"
        stats_line = (
            f"  [green]Success: {self.success}[/green]  "
            f"[red]Failed: {self.failed}[/red]  "
            f"[dim]Skipped: {self.skipped + self.skip_during}[/dim]  "
            f"Speed: {rate:.1f}/s  "
            f"ETA: {timedelta(seconds=int(remaining))}  "
            f"Current: [bold]{pii_display}[/bold]"
        )

        progress_panel = Panel(
            f"{self.progress.__rich_console__(self.console, self.console.options)}\n"  # type: ignore
            if False else stats_line,
            title="[bold cyan]Progress[/bold cyan]",
            border_style="blue",
        )

        # 실제로는 Table grid 구성
        grid = Table.grid(expand=True)

        # 상단
        grid.add_row(header)

        # 중단 - progress bar + stats
        mid = Table.grid(expand=True)
        mid.add_row(Panel(stats_line, title="[bold]Stats[/bold]", border_style="blue"))
        grid.add_row(mid)

        # 하단: 로그
        log_text = self.log_buf.render() or "  (waiting...)"
        log_panel = Panel(
            log_text,
            title="[bold cyan]Recent Logs[/bold cyan]",
            border_style="dim",
        )
        grid.add_row(log_panel)

        return Panel(grid, title=f"[bold]SciDir Crawler — {self.mode.upper()}[/bold]")


# ============================================================================
# Core Downloaders
# ============================================================================

class PDFDownloader:
    """
    ScienceDirect 논문 본문 PDF 다운로더.

    다운로드 전략 (우선순위):
    1. 기사 페이지에서 PDF 링크 href 추출 → requests로 직접 다운로드
    2. View PDF 클릭 → 팝업 → expect_download
    3. 팝업 URL이 PDF이면 requests fallback으로 다운로드
    4. page.pdf()로 인쇄 형태 저장 (최후 수단)
    """

    # 기사 페이지에서 PDF 버튼 탐지 셀렉터 우선순위
    PDF_BUTTON_SELECTORS = [
        'li.ViewPDF a[href*="pdfft"]',
        'a.accessbar-utility-link[href*="pdfft"]',
        'a[href*="/pii/"][href$="pdf"]',
        'a[href*="pdf"]',
        '#pdfLink',
        'button[data-aa-name="viewPdf"]',
    ]

    # PDF 뷰어 팝업 내 다운로드 버튼 셀렉터
    PDF_VIEWER_DOWNLOAD_SELECTORS = [
        'button[aria-label*="ownload"]',
        'a[aria-label*="ownload"]',
        'a[href*="pdfft"][download]',
        'a[download]',
        '#download',
        'cr-icon-button#download',
    ]

    def __init__(
        self,
        config: DownloadConfig,
        session_keeper: SessionKeeper,
        logger: logging.Logger,
    ):
        self.config = config
        self.session_keeper = session_keeper
        self.logger = logger
        self.save_dir = Path(config.save_dir_pdf)
        self.save_dir.mkdir(parents=True, exist_ok=True)

    def _random_delay(self) -> None:
        """안티봇 탐지를 위한 랜덤 대기."""
        d = random.uniform(self.config.delay_min, self.config.delay_max)
        time.sleep(d)

    def _extract_pdf_href(self, page: Any) -> Optional[str]:
        """기사 페이지에서 PDF 직접 다운로드 URL을 추출."""
        for sel in self.PDF_BUTTON_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    href = el.get_attribute("href")
                    if href and ("pdfft" in href or href.endswith(".pdf")):
                        if href.startswith("/"):
                            href = f"{SD_BASE_URL}{href}"
                        self.logger.debug(f"PDF href found: {href}")
                        return href
            except Exception:
                continue
        return None

    def _get_cookies_for_requests(self, context: Any) -> Dict[str, str]:
        """Playwright context에서 쿠키를 requests용 dict로 변환."""
        cookies = {}
        try:
            for c in context.cookies():
                cookies[c["name"]] = c["value"]
        except Exception:
            pass
        return cookies

    def _download_via_requests(
        self, url: str, dest: Path, cookies: Dict[str, str]
    ) -> bool:
        """requests 라이브러리로 PDF를 직접 다운로드."""
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": SD_BASE_URL,
            }
            resp = requests.get(
                url, cookies=cookies, headers=headers,
                timeout=60, stream=True, allow_redirects=True,
            )

            # 로그인 리다이렉트 감지
            final_url = resp.url.lower()
            if "login" in final_url or "idp" in final_url or "auth" in final_url:
                raise LoginRequiredError(f"Redirected to login: {resp.url}")

            if resp.status_code != 200:
                self.logger.warning(f"HTTP {resp.status_code} for {url}")
                return False

            content_type = resp.headers.get("Content-Type", "").lower()
            if "pdf" not in content_type and "octet" not in content_type:
                # 응답이 PDF가 아닐 수 있음
                self.logger.warning(f"Non-PDF Content-Type: {content_type}")
                # 그래도 저장 시도 (일부 서버는 content-type을 잘못 보냄)

            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)

            # 최소 크기 검증 (1KB 미만이면 실패 간주)
            if dest.stat().st_size < 1024:
                dest.unlink(missing_ok=True)
                self.logger.warning("Downloaded file too small (<1KB), discarded.")
                return False

            return True

        except LoginRequiredError:
            raise
        except Exception as e:
            self.logger.warning(f"requests download failed: {e}")
            return False

    def download_one(self, context: Any, url: str, pii: str) -> Tuple[bool, str]:
        """
        단일 논문 PDF 다운로드.

        Returns:
            (success, filename_or_error_msg)
        """
        filename = make_pdf_filename(pii)
        dest = self.save_dir / filename

        page = context.new_page()
        try:
            self.logger.info(f"[OPEN] {url}")
            page.goto(url, timeout=self.config.timeout_page, wait_until="domcontentloaded")

            # 로그인 리다이렉트 감지
            current_url = page.url.lower()
            if "login" in current_url or "idp" in current_url or "auth" in current_url:
                raise LoginRequiredError(f"Redirected to login page: {page.url}")

            time.sleep(2)
            # 스크롤하여 동적 로딩 유도
            page.mouse.wheel(0, 1500)
            time.sleep(1)

            # ── 전략 1: href 추출 → requests 직접 다운로드 ──
            pdf_href = self._extract_pdf_href(page)
            if pdf_href:
                cookies = self._get_cookies_for_requests(context)
                if self._download_via_requests(pdf_href, dest, cookies):
                    self.logger.info(f"[SUCCESS:requests] {filename}")
                    return True, filename

            # ── 전략 2: View PDF 클릭 → 팝업 ──
            target_sel = None
            for sel in self.PDF_BUTTON_SELECTORS:
                try:
                    if page.locator(sel).count() > 0:
                        target_sel = sel
                        break
                except Exception:
                    continue

            if not target_sel:
                raise RuntimeError("No PDF button found on article page.")

            self.logger.info(f"[CLICK] {target_sel}")

            pdf_page = None
            try:
                with page.expect_popup(timeout=15000) as popup_ev:
                    page.locator(target_sel).first.click()
                pdf_page = popup_ev.value
                self.logger.info(f"[POPUP] {pdf_page.url}")
            except Exception as e:
                self.logger.warning(f"Popup failed: {e}, trying navigation...")
                # 팝업 대신 같은 탭에서 이동한 경우
                time.sleep(3)
                current = page.url
                if "pdfft" in current or current.endswith(".pdf"):
                    # 현재 페이지가 PDF URL
                    cookies = self._get_cookies_for_requests(context)
                    if self._download_via_requests(current, dest, cookies):
                        self.logger.info(f"[SUCCESS:nav-requests] {filename}")
                        return True, filename

            if pdf_page:
                try:
                    pdf_page.wait_for_load_state("networkidle", timeout=30000)
                except Exception:
                    pass
                time.sleep(2)

                pdf_viewer_url = pdf_page.url

                # ── 전략 2a: 팝업 URL이 PDF이면 requests로 다운로드 ──
                if "pdfft" in pdf_viewer_url or pdf_viewer_url.endswith(".pdf"):
                    cookies = self._get_cookies_for_requests(context)
                    if self._download_via_requests(pdf_viewer_url, dest, cookies):
                        self.logger.info(f"[SUCCESS:popup-requests] {filename}")
                        try:
                            pdf_page.close()
                        except Exception:
                            pass
                        return True, filename

                # ── 전략 2b: 팝업 내 다운로드 버튼 클릭 → expect_download ──
                for dl_sel in self.PDF_VIEWER_DOWNLOAD_SELECTORS:
                    try:
                        if pdf_page.locator(dl_sel).count() > 0:
                            try:
                                with pdf_page.expect_download(
                                    timeout=self.config.timeout_download
                                ) as dl_ev:
                                    pdf_page.locator(dl_sel).first.click()
                                download = dl_ev.value
                                download.save_as(str(dest))
                                self.logger.info(f"[SUCCESS:expect_download] {filename}")
                                try:
                                    pdf_page.close()
                                except Exception:
                                    pass
                                return True, filename
                            except Exception as e:
                                self.logger.debug(f"expect_download failed with {dl_sel}: {e}")
                                continue
                    except Exception:
                        continue

                # ── 전략 3: page.pdf() fallback ──
                try:
                    pdf_page.pdf(path=str(dest))
                    if dest.stat().st_size > 1024:
                        self.logger.info(f"[SUCCESS:page.pdf] {filename}")
                        try:
                            pdf_page.close()
                        except Exception:
                            pass
                        return True, filename
                except Exception as e:
                    self.logger.debug(f"page.pdf() failed: {e}")

                try:
                    pdf_page.close()
                except Exception:
                    pass

            raise RuntimeError("All download strategies exhausted.")

        except (LoginRequiredError, ChromeDeadError):
            raise
        except Exception as e:
            return False, str(e)[:300]
        finally:
            try:
                page.close()
            except Exception:
                pass


class SuppDownloader:
    """
    ScienceDirect Supplementary 파일 다운로더.

    기사 페이지에서 Supplementary data 섹션의 파일 링크를 탐지하여 다운로드.
    """

    SUPP_LINK_SELECTORS = [
        "div.Appendices a.download-link",
        'a[href*="mmc"]',
    ]

    SUPP_XPATH_SELECTORS = [
        '//section[.//h2[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
        ' "abcdefghijklmnopqrstuvwxyz"), "supplementary")]]'
        '//a[contains(@class, "download-link")]',
    ]

    def __init__(
        self,
        config: DownloadConfig,
        session_keeper: SessionKeeper,
        logger: logging.Logger,
    ):
        self.config = config
        self.session_keeper = session_keeper
        self.logger = logger
        self.save_dir = Path(config.save_dir_supp)
        self.save_dir.mkdir(parents=True, exist_ok=True)

    def _random_delay(self) -> None:
        """안티봇 탐지를 위한 랜덤 대기."""
        d = random.uniform(self.config.delay_min, self.config.delay_max)
        time.sleep(d)

    def _get_cookies_for_requests(self, context: Any) -> Dict[str, str]:
        """Playwright context에서 쿠키를 requests용 dict로 변환."""
        cookies = {}
        try:
            for c in context.cookies():
                cookies[c["name"]] = c["value"]
        except Exception:
            pass
        return cookies

    def download_one(self, context: Any, url: str, pii: str) -> Tuple[bool, int, str]:
        """
        단일 논문의 Supplementary 파일 전체 다운로드.

        Returns:
            (success, downloaded_count, error_msg_or_empty)
        """
        article_dir = self.save_dir / pii
        page = context.new_page()
        downloaded_count = 0

        try:
            self.logger.info(f"[OPEN] {url}")
            page.goto(url, timeout=self.config.timeout_page, wait_until="domcontentloaded")

            # 로그인 감지
            current_url = page.url.lower()
            if "login" in current_url or "idp" in current_url:
                raise LoginRequiredError(f"Redirected to login: {page.url}")

            time.sleep(3)
            page.mouse.wheel(0, 15000)
            time.sleep(2)

            # ── 링크 탐지 ──
            target_links = None
            used_selector = ""

            # CSS 셀렉터 시도
            for sel in self.SUPP_LINK_SELECTORS:
                try:
                    page.wait_for_selector(sel, timeout=2000)
                    if page.locator(sel).count() > 0:
                        target_links = page.locator(sel)
                        used_selector = sel
                        break
                except Exception:
                    continue

            # XPath 시도
            if not target_links:
                for xpath in self.SUPP_XPATH_SELECTORS:
                    try:
                        if page.locator(xpath).count() > 0:
                            target_links = page.locator(xpath)
                            used_selector = xpath
                            break
                    except Exception:
                        continue

            if not target_links or target_links.count() == 0:
                self.logger.info(f"[INFO] No supplementary links found for {pii}")
                return True, 0, ""  # 보충 자료가 없는 건 성공 처리

            count = target_links.count()
            self.logger.info(f"[FOUND] {count} supplementary file(s) via {used_selector}")

            # ── 쿠키 기반 직접 다운로드 시도 ──
            cookies = self._get_cookies_for_requests(context)

            for i in range(count):
                link = target_links.nth(i)
                try:
                    href = link.get_attribute("href") or ""
                    if href.startswith("/"):
                        href = f"{SD_BASE_URL}{href}"

                    if not href:
                        continue

                    # href에서 파일명 추출 시도
                    link_text = link.text_content() or ""
                    original_name = href.split("/")[-1].split("?")[0]
                    if not original_name or "." not in original_name:
                        original_name = f"mmc{i+1}"

                    safe_filename = f"{pii}_{original_name}"
                    article_dir.mkdir(parents=True, exist_ok=True)
                    dest = article_dir / safe_filename

                    # 이미 존재하면 스킵
                    if dest.exists() and dest.stat().st_size > 100:
                        self.logger.debug(f"[SKIP] Already exists: {dest}")
                        downloaded_count += 1
                        continue

                    # 전략 1: requests로 직접 다운로드
                    if href and ("mmc" in href or "download" in href.lower()):
                        try:
                            headers = {
                                "User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36"
                                ),
                                "Referer": url,
                            }
                            resp = requests.get(
                                href, cookies=cookies, headers=headers,
                                timeout=60, stream=True, allow_redirects=True,
                            )
                            if resp.status_code == 200 and len(resp.content) > 100:
                                # Content-Disposition에서 파일명 추출
                                cd = resp.headers.get("Content-Disposition", "")
                                if "filename=" in cd:
                                    fn_match = re.search(r'filename[*]?="?([^";]+)', cd)
                                    if fn_match:
                                        original_name = fn_match.group(1).strip()
                                        safe_filename = f"{pii}_{original_name}"
                                        dest = article_dir / safe_filename

                                with open(dest, "wb") as f:
                                    for chunk in resp.iter_content(65536):
                                        f.write(chunk)
                                self.logger.info(f"[SUCCESS:requests] {safe_filename}")
                                downloaded_count += 1
                                continue
                        except Exception as e:
                            self.logger.debug(f"requests failed for supp {i}: {e}")

                    # 전략 2: Playwright expect_download
                    try:
                        link.scroll_into_view_if_needed()
                        with page.expect_download(
                            timeout=self.config.timeout_download
                        ) as dl_ev:
                            link.click()
                        download = dl_ev.value
                        suggested = download.suggested_filename
                        safe_filename = f"{pii}_{suggested}"
                        dest = article_dir / safe_filename
                        download.save_as(str(dest))
                        self.logger.info(f"[SUCCESS:playwright] {safe_filename}")
                        downloaded_count += 1
                    except Exception as e:
                        self.logger.warning(
                            f"[DOWNLOAD FAIL] Supp index {i} for {pii}: {e}"
                        )
                        continue

                except Exception as e:
                    self.logger.warning(f"[SUPP FAIL] Index {i} for {pii}: {e}")
                    continue

            return downloaded_count > 0 or count == 0, downloaded_count, ""

        except (LoginRequiredError, ChromeDeadError):
            raise
        except Exception as e:
            return False, downloaded_count, str(e)[:300]
        finally:
            try:
                page.close()
            except Exception:
                pass


# ============================================================================
# Orchestrator (main download loop)
# ============================================================================

class CrawlOrchestrator:
    """
    크롤링 오케스트레이터.
    Chrome 관리, 세션 유지, 주기적 재시작, 대시보드를 통합 관리.
    """

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        chrome_mgr: ChromeManager,
        session_keeper: SessionKeeper,
    ):
        self.config = config
        self.logger = logger
        self.chrome_mgr = chrome_mgr
        self.session_keeper = session_keeper

    def _connect_browser(self, pw: Any) -> Any:
        """Playwright로 Chrome CDP에 연결."""
        port = self.config.chrome.debug_port
        cdp_url = f"http://127.0.0.1:{port}"
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            return browser
        except Exception as e:
            raise ChromeDeadError(f"Cannot connect to Chrome CDP at {cdp_url}: {e}")

    def _create_context(self, browser: Any) -> Any:
        """storage_state 주입하여 새 context 생성."""
        ss = self.session_keeper.get_storage_state()
        kwargs = {"accept_downloads": True}
        if ss:
            kwargs["storage_state"] = ss
        return browser.new_context(**kwargs)

    def run_pdf(
        self,
        urls: List[Tuple[str, str]],
        force: bool = False,
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        PDF 다운로드 메인 루프.

        Args:
            urls: [(url, pii), ...] 리스트
            force: True이면 이미 존재해도 재다운로드

        Returns:
            (success_list, fail_list)
        """
        from playwright.sync_api import sync_playwright

        # Resume 체크
        save_dir = Path(self.config.download.save_dir_pdf)
        existing = get_existing_pdfs(save_dir) if not force else set()
        pending = [(u, p) for u, p in urls if normalize_pii_for_filename(p) not in existing]
        skipped = len(urls) - len(pending)

        self.logger.info(f"PDF download: {len(pending)} pending, {skipped} skipped (resume)")

        if not pending:
            Console().print("[green]All PDFs already downloaded. Nothing to do.[/green]")
            return [], []

        downloader = PDFDownloader(self.config.download, self.session_keeper, self.logger)
        dashboard = Dashboard(
            total=len(pending),
            skipped=skipped,
            mode="pdf",
            config=self.config,
            chrome_status=self.chrome_mgr.status(),
        )

        success_list: List[Dict] = []
        fail_list: List[Dict] = []
        processed_count = 0
        restart_interval = self.config.chrome.restart_every_n

        console = Console()

        with sync_playwright() as pw:
            browser = self._connect_browser(pw)

            with Live(console=console, refresh_per_second=2) as live:
                for url, pii in pending:
                    context = None
                    try:
                        dashboard.set_current(url)

                        # 주기적 Chrome 재시작
                        if (
                            restart_interval > 0
                            and processed_count > 0
                            and processed_count % restart_interval == 0
                        ):
                            # 세션 저장
                            if context:
                                self.session_keeper.save(context)
                                context.close()
                                context = None

                            try:
                                browser.close()
                            except Exception:
                                pass

                            self.chrome_mgr.restart()
                            browser = self._connect_browser(pw)
                            dashboard.update_chrome_restart()

                        context = self._create_context(browser)

                        # 재시도 래핑
                        retry_fn = with_retry(
                            max_retries=self.config.download.retry_count,
                            logger=self.logger,
                        )(downloader.download_one)

                        success, msg = retry_fn(context, url, pii)

                        if success:
                            dashboard.update_success(pii, msg)
                            success_list.append({"pii": pii, "url": url, "file": msg})
                        else:
                            err_type = classify_error(Exception(msg), url)
                            dashboard.update_fail(pii, err_type, msg)
                            fail_list.append({
                                "pii": pii, "url": url,
                                "error_type": err_type.value, "error": msg,
                            })

                        # 세션 주기적 저장
                        if processed_count % 10 == 0:
                            self.session_keeper.save(context)

                    except LoginRequiredError as e:
                        console.print(
                            f"\n[bold red]{'='*60}[/bold red]\n"
                            f"[bold red]LOGIN REQUIRED[/bold red]\n"
                            f"ScienceDirect requires login.\n"
                            f"Please log in via the Chrome window and restart the crawler.\n"
                            f"Redirect URL: {e}\n"
                            f"[bold red]{'='*60}[/bold red]\n"
                        )
                        # 지금까지 실패 저장
                        fail_list.append({
                            "pii": pii, "url": url,
                            "error_type": ErrorType.LOGIN_REQUIRED.value,
                            "error": str(e),
                        })
                        break

                    except ChromeDeadError:
                        dashboard.log_buf.add("Chrome connection lost, reconnecting...", "yellow")
                        try:
                            self.chrome_mgr.restart()
                            browser = self._connect_browser(pw)
                            dashboard.update_chrome_restart()
                        except Exception as re_e:
                            self.logger.error(f"Chrome reconnect failed: {re_e}")
                            break

                    except Exception as e:
                        err_type = classify_error(e, url)
                        dashboard.update_fail(pii, err_type, str(e)[:200])
                        fail_list.append({
                            "pii": pii, "url": url,
                            "error_type": err_type.value, "error": str(e)[:300],
                        })

                    finally:
                        if context:
                            try:
                                context.close()
                            except Exception:
                                pass
                        processed_count += 1

                        # 대시보드 업데이트
                        live.update(dashboard.render())

                        # 랜덤 대기
                        downloader._random_delay()

        return success_list, fail_list

    def run_supp(
        self,
        urls: List[Tuple[str, str]],
        force: bool = False,
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Supplementary 다운로드 메인 루프.
        """
        from playwright.sync_api import sync_playwright

        save_dir = Path(self.config.download.save_dir_supp)
        existing = get_existing_supp_piis(save_dir) if not force else set()
        pending = [(u, p) for u, p in urls if p not in existing]
        skipped = len(urls) - len(pending)

        self.logger.info(f"Supp download: {len(pending)} pending, {skipped} skipped")

        if not pending:
            Console().print("[green]All supplementary files already downloaded.[/green]")
            return [], []

        downloader = SuppDownloader(self.config.download, self.session_keeper, self.logger)
        dashboard = Dashboard(
            total=len(pending),
            skipped=skipped,
            mode="supp",
            config=self.config,
            chrome_status=self.chrome_mgr.status(),
        )

        success_list: List[Dict] = []
        fail_list: List[Dict] = []
        processed_count = 0
        restart_interval = self.config.chrome.restart_every_n

        console = Console()

        with sync_playwright() as pw:
            browser = self._connect_browser(pw)

            with Live(console=console, refresh_per_second=2) as live:
                for url, pii in pending:
                    context = None
                    try:
                        dashboard.set_current(url)

                        if (
                            restart_interval > 0
                            and processed_count > 0
                            and processed_count % restart_interval == 0
                        ):
                            if context:
                                self.session_keeper.save(context)
                                context.close()
                                context = None
                            try:
                                browser.close()
                            except Exception:
                                pass
                            self.chrome_mgr.restart()
                            browser = self._connect_browser(pw)
                            dashboard.update_chrome_restart()

                        context = self._create_context(browser)

                        retry_fn = with_retry(
                            max_retries=self.config.download.retry_count,
                            logger=self.logger,
                        )(downloader.download_one)

                        success, dl_count, msg = retry_fn(context, url, pii)

                        if success:
                            dashboard.update_success(pii, f"{dl_count} files")
                            success_list.append({
                                "pii": pii, "url": url, "file_count": dl_count,
                            })
                        else:
                            err_type = classify_error(Exception(msg), url)
                            dashboard.update_fail(pii, err_type, msg)
                            fail_list.append({
                                "pii": pii, "url": url,
                                "error_type": err_type.value, "error": msg,
                            })

                        if processed_count % 10 == 0:
                            self.session_keeper.save(context)

                    except LoginRequiredError as e:
                        console.print(
                            f"\n[bold red]LOGIN REQUIRED — "
                            f"Please log in and restart.[/bold red]\n"
                            f"Redirect: {e}\n"
                        )
                        fail_list.append({
                            "pii": pii, "url": url,
                            "error_type": ErrorType.LOGIN_REQUIRED.value,
                            "error": str(e),
                        })
                        break

                    except ChromeDeadError:
                        dashboard.log_buf.add("Chrome lost, reconnecting...", "yellow")
                        try:
                            self.chrome_mgr.restart()
                            browser = self._connect_browser(pw)
                            dashboard.update_chrome_restart()
                        except Exception:
                            break

                    except Exception as e:
                        err_type = classify_error(e, url)
                        dashboard.update_fail(pii, err_type, str(e)[:200])
                        fail_list.append({
                            "pii": pii, "url": url,
                            "error_type": err_type.value, "error": str(e)[:300],
                        })

                    finally:
                        if context:
                            try:
                                context.close()
                            except Exception:
                                pass
                        processed_count += 1
                        live.update(dashboard.render())
                        downloader._random_delay()

        return success_list, fail_list


# ============================================================================
# CSV I/O Helpers
# ============================================================================

def load_urls_from_csv(
    csv_path: Path,
    col: str,
    retry_failed: bool = False,
) -> List[Tuple[str, str]]:
    """
    CSV에서 URL + PII 목록을 로드.

    Args:
        csv_path: CSV 파일 경로
        col: URL/PII 컬럼명
        retry_failed: True이면 fail CSV 형식(pii, url 컬럼) 기대

    Returns:
        [(url, pii), ...] 리스트
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    suffix = csv_path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        df = pd.read_excel(csv_path)
    else:
        df = pd.read_csv(csv_path)

    if retry_failed and "url" in df.columns and "pii" in df.columns:
        pairs = []
        for _, row in df.iterrows():
            url_val = row.get("url")
            pii_val = row.get("pii", "")
            if url_val and not pd.isna(url_val):
                url_norm = normalize_url(url_val)
                if url_norm:
                    pairs.append((url_norm, str(pii_val)))
        return pairs

    if col not in df.columns:
        # pii 컬럼 시도
        if "pii" in df.columns:
            col = "pii"
        else:
            available = ", ".join(df.columns.tolist()[:10])
            raise ValueError(
                f"Column '{col}' not found in CSV. Available: {available}"
            )

    pairs = []
    for _, row in df.iterrows():
        raw = row[col]
        url_norm = normalize_url(raw)
        if url_norm:
            pii = extract_pii(url_norm)
            pairs.append((url_norm, pii))

    return pairs


def save_fail_list(fail_list: List[Dict], path: Path) -> None:
    """실패 목록을 CSV로 저장."""
    if not fail_list:
        return
    df = pd.DataFrame(fail_list)
    df.to_csv(path, index=False, encoding="utf-8")


# ============================================================================
# CLI Commands
# ============================================================================

def cmd_init(config: AppConfig, logger: logging.Logger) -> None:
    """환경 초기화: 설정 파일 생성 + Chrome 감지 + 의존성 점검."""
    console = Console()
    console.print("\n[bold cyan]═══ SciDir Crawler — Environment Init ═══[/bold cyan]\n")

    # 1. 설정 파일 생성
    config_path = Path(CONFIG_FILE)
    if config_path.exists():
        console.print(f"  [dim]Config file exists:[/dim] {config_path}")
    else:
        config.save(config_path)
        console.print(f"  [green]Config file created:[/green] {config_path}")

    # 2. Chrome 탐지
    chrome_mgr = ChromeManager(config.chrome, logger)
    try:
        chrome_path = chrome_mgr.find_chrome()
        console.print(f"  [green]Chrome found:[/green] {chrome_path}")
    except FileNotFoundError as e:
        console.print(f"  [red]Chrome NOT found:[/red] {e}")

    # 3. CDP 포트 확인
    port_open = chrome_mgr.is_port_open()
    if port_open:
        console.print(f"  [green]CDP port {config.chrome.debug_port}: OPEN[/green]")
    else:
        console.print(f"  [yellow]CDP port {config.chrome.debug_port}: CLOSED[/yellow]")
        console.print(f"    → Run: python scidir_crawler.py chrome --start")

    # 4. 디렉토리 생성
    for d in [config.download.save_dir_pdf, config.download.save_dir_supp]:
        dp = Path(d)
        dp.mkdir(parents=True, exist_ok=True)
        console.print(f"  [green]Directory ready:[/green] {dp}")

    # 5. Playwright 점검
    try:
        from playwright.sync_api import sync_playwright
        console.print("  [green]Playwright: installed[/green]")
    except ImportError:
        console.print("  [red]Playwright: NOT installed[/red]")
        console.print("    → pip install playwright && playwright install chromium")

    # 6. 기타 의존성
    for pkg_name in ["pandas", "requests", "rich"]:
        try:
            __import__(pkg_name)
            console.print(f"  [green]{pkg_name}: installed[/green]")
        except ImportError:
            console.print(f"  [red]{pkg_name}: NOT installed[/red]")

    console.print("\n[bold green]Init complete.[/bold green]\n")


def cmd_chrome(
    config: AppConfig,
    logger: logging.Logger,
    action: str,
) -> None:
    """Chrome 관리 명령."""
    chrome_mgr = ChromeManager(config.chrome, logger)
    console = Console()

    if action == "start":
        chrome_mgr.start()
        console.print("[green]Chrome started in debug mode.[/green]")
        console.print(
            f"[bold]Please log in to ScienceDirect in the Chrome window,[/bold]\n"
            f"then keep it open while running downloads."
        )

    elif action == "stop":
        chrome_mgr.stop()
        console.print("[yellow]Chrome stopped.[/yellow]")

    elif action == "status":
        status = chrome_mgr.status()
        table = Table(title="Chrome Status", show_header=False)
        table.add_column("Key", style="bold")
        table.add_column("Value")
        for k, v in status.items():
            style = "green" if v and k == "port_open" else "white"
            table.add_row(k, str(v))
        console.print(table)


def cmd_pdf(
    config: AppConfig,
    logger: logging.Logger,
    args: argparse.Namespace,
) -> None:
    """PDF 다운로드 명령."""
    csv_path = Path(args.csv)
    col = args.col
    force = getattr(args, "force", False)
    retry_failed = getattr(args, "retry_failed", False)

    urls = load_urls_from_csv(csv_path, col, retry_failed=retry_failed)
    logger.info(f"Loaded {len(urls)} URLs from {csv_path}")

    if not urls:
        Console().print("[yellow]No URLs to process.[/yellow]")
        return

    chrome_mgr = ChromeManager(config.chrome, logger)
    if not chrome_mgr.is_port_open():
        Console().print("[yellow]Chrome not running. Starting automatically...[/yellow]")
        chrome_mgr.start()

    session_keeper = SessionKeeper(Path(COOKIES_FILE), logger)
    orchestrator = CrawlOrchestrator(config, logger, chrome_mgr, session_keeper)

    success_list, fail_list = orchestrator.run_pdf(urls, force=force)

    # 실패 목록 저장
    save_fail_list(fail_list, Path(FAIL_PDF_FILE))

    # 최종 요약
    console = Console()
    console.print(f"\n[bold cyan]{'═'*50}[/bold cyan]")
    console.print(f"  [bold]PDF Download Complete[/bold]")
    console.print(f"  Success : [green]{len(success_list)}[/green]")
    console.print(f"  Failed  : [red]{len(fail_list)}[/red]")
    if fail_list:
        console.print(f"  Fail CSV: {FAIL_PDF_FILE}")
    console.print(f"[bold cyan]{'═'*50}[/bold cyan]\n")


def cmd_supp(
    config: AppConfig,
    logger: logging.Logger,
    args: argparse.Namespace,
) -> None:
    """Supplementary 다운로드 명령."""
    csv_path = Path(args.csv)
    col = args.col
    force = getattr(args, "force", False)
    retry_failed = getattr(args, "retry_failed", False)

    urls = load_urls_from_csv(csv_path, col, retry_failed=retry_failed)
    logger.info(f"Loaded {len(urls)} URLs from {csv_path}")

    if not urls:
        Console().print("[yellow]No URLs to process.[/yellow]")
        return

    chrome_mgr = ChromeManager(config.chrome, logger)
    if not chrome_mgr.is_port_open():
        Console().print("[yellow]Chrome not running. Starting automatically...[/yellow]")
        chrome_mgr.start()

    session_keeper = SessionKeeper(Path(COOKIES_FILE), logger)
    orchestrator = CrawlOrchestrator(config, logger, chrome_mgr, session_keeper)

    success_list, fail_list = orchestrator.run_supp(urls, force=force)

    save_fail_list(fail_list, Path(FAIL_SUPP_FILE))

    console = Console()
    console.print(f"\n[bold cyan]{'═'*50}[/bold cyan]")
    console.print(f"  [bold]Supplementary Download Complete[/bold]")
    console.print(f"  Success : [green]{len(success_list)}[/green]")
    console.print(f"  Failed  : [red]{len(fail_list)}[/red]")
    if fail_list:
        console.print(f"  Fail CSV: {FAIL_SUPP_FILE}")
    console.print(f"[bold cyan]{'═'*50}[/bold cyan]\n")


def cmd_all(
    config: AppConfig,
    logger: logging.Logger,
    args: argparse.Namespace,
) -> None:
    """PDF + Supplementary 동시 다운로드."""
    Console().print("[bold]Phase 1/2: Downloading PDFs...[/bold]\n")
    cmd_pdf(config, logger, args)

    Console().print("\n[bold]Phase 2/2: Downloading Supplementary files...[/bold]\n")
    cmd_supp(config, logger, args)


# ============================================================================
# Argument Parser
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서 구성."""
    parser = argparse.ArgumentParser(
        prog="scidir_crawler",
        description="ScienceDirect PDF & Supplementary Crawler (Enterprise Edition)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scidir_crawler.py init\n"
            "  python scidir_crawler.py chrome --start\n"
            "  python scidir_crawler.py pdf --csv input.csv --col prism_url\n"
            "  python scidir_crawler.py supp --csv input.csv --col prism_url\n"
            "  python scidir_crawler.py all --csv input.csv\n"
            "  python scidir_crawler.py pdf --csv fail_pdf.csv --retry-failed\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── init ──
    subparsers.add_parser("init", help="Initialize environment and config")

    # ── chrome ──
    chrome_parser = subparsers.add_parser("chrome", help="Manage Chrome browser")
    chrome_group = chrome_parser.add_mutually_exclusive_group(required=True)
    chrome_group.add_argument("--start", action="store_true", help="Start Chrome in debug mode")
    chrome_group.add_argument("--stop", action="store_true", help="Stop managed Chrome")
    chrome_group.add_argument("--status", action="store_true", help="Show Chrome status")

    # ── pdf ──
    pdf_parser = subparsers.add_parser("pdf", help="Download paper PDFs")
    pdf_parser.add_argument("--csv", required=True, help="Input CSV path")
    pdf_parser.add_argument("--col", default="prism_url", help="URL/PII column name")
    pdf_parser.add_argument("--save_dir", default=None, help="Override PDF save directory")
    pdf_parser.add_argument("--force", action="store_true", help="Force re-download")
    pdf_parser.add_argument("--retry-failed", action="store_true", help="Retry from fail CSV")

    # ── supp ──
    supp_parser = subparsers.add_parser("supp", help="Download supplementary files")
    supp_parser.add_argument("--csv", required=True, help="Input CSV path")
    supp_parser.add_argument("--col", default="prism_url", help="URL/PII column name")
    supp_parser.add_argument("--save_dir", default=None, help="Override supp save directory")
    supp_parser.add_argument("--force", action="store_true", help="Force re-download")
    supp_parser.add_argument("--retry-failed", action="store_true", help="Retry from fail CSV")

    # ── all ──
    all_parser = subparsers.add_parser("all", help="Download PDFs + supplementary")
    all_parser.add_argument("--csv", required=True, help="Input CSV path")
    all_parser.add_argument("--col", default="prism_url", help="URL/PII column name")
    all_parser.add_argument("--save_dir", default=None, help="Override save directory")
    all_parser.add_argument("--force", action="store_true", help="Force re-download")
    all_parser.add_argument("--retry-failed", action="store_true", help="Retry from fail CSV")

    return parser


# ============================================================================
# Main Entry Point
# ============================================================================

def main() -> None:
    """메인 진입점."""
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # 설정 로드
    config = AppConfig.load(Path(CONFIG_FILE))

    # CLI 오버라이드 적용
    config.apply_cli_overrides(args)
    if hasattr(args, "save_dir") and args.save_dir and args.command == "supp":
        config.download.save_dir_supp = args.save_dir

    # 로거 설정
    logger = setup_logging(config.log)

    # 명령 디스패치
    if args.command == "init":
        cmd_init(config, logger)

    elif args.command == "chrome":
        action = "start" if args.start else ("stop" if args.stop else "status")
        cmd_chrome(config, logger, action)

    elif args.command == "pdf":
        cmd_pdf(config, logger, args)

    elif args.command == "supp":
        cmd_supp(config, logger, args)

    elif args.command == "all":
        cmd_all(config, logger, args)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
