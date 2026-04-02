# SciDir Crawler — 설치 및 실행 가이드

## 1. Conda 환경 생성

```bash
conda create -n scidir python=3.10 -y
conda activate scidir
```

## 2. 핵심 의존성 설치

```bash
pip install playwright pandas rich requests
```

## 3. Playwright 브라우저 설치

```bash
playwright install chromium
```

> Playwright는 자체 Chromium 바이너리를 사용합니다.
> CDP 모드에서는 시스템 Chrome에 연결하므로, 이 단계는 fallback용입니다.

## 4. 설정 초기화

```bash
python scidir_crawler.py init
```

이 명령이 수행하는 것:

- `scidir_config.json` 기본 설정 파일 생성
- 시스템 Chrome 경로 자동 탐지
- CDP 포트(9222) 상태 확인
- `./pdfs`, `./supplementary_files` 디렉토리 생성
- 설치된 패키지 점검 결과 출력

## 5. Chrome 디버그 모드 실행

```bash
python scidir_crawler.py chrome --start
```

Chrome 창이 열리면 **ScienceDirect에 로그인**(기관 프록시 또는 개인 계정)한 뒤 Chrome 창을 **닫지 말고** 유지합니다.

로그인 확인 후 다운로드를 시작합니다:

```bash
# PDF만
python scidir_crawler.py pdf --csv 검수파일77.csv --col prism_url

# Supplementary만
python scidir_crawler.py supp --csv 검수파일77.csv --col prism_url

# 둘 다
python scidir_crawler.py all --csv 검수파일77.csv --col prism_url
```

## 6. 실패 재처리

```bash
python scidir_crawler.py pdf --csv fail_pdf.csv --retry-failed
python scidir_crawler.py supp --csv fail_supp.csv --retry-failed
```

## 7. 전체 명령어 요약

| 명령              | 설명                            |
| ----------------- | ------------------------------- |
| `init`            | 환경 초기화 + 설정 파일 생성    |
| `chrome --start`  | Chrome 디버그 모드 시작         |
| `chrome --stop`   | Chrome 종료                     |
| `chrome --status` | Chrome 상태 확인                |
| `pdf --csv FILE`  | 논문 PDF 다운로드               |
| `supp --csv FILE` | Supplementary 다운로드          |
| `all --csv FILE`  | PDF + Supplementary 동시        |
| `--force`         | 기존 파일 무시, 강제 재다운로드 |
| `--retry-failed`  | 실패 CSV로 재처리               |
