# -*- coding: utf-8 -*-
"""
Supplementary Files to Markdown Converter (통합 버전)
- DOCX/PDF/HTML → Markdown 변환
- 이미지 자동 추출
- 경로 자동 수정
- 모든 작업을 한 번에 처리
"""

import os
import argparse
import logging
from pathlib import Path
from tqdm import tqdm
import subprocess
import re
import base64

# ---------------------------------------------------------------
# 1. Pandoc 설치 확인
# ---------------------------------------------------------------
def check_pandoc():
    """Pandoc이 설치되어 있는지 확인"""
    try:
        result = subprocess.run(["pandoc", "--version"], 
                               capture_output=True, 
                               text=True, 
                               check=True)
        version = result.stdout.split('\n')[0]
        logging.info(f"Pandoc found: {version}")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        logging.warning("Pandoc not found. Install from: https://pandoc.org/installing.html")
        return False

# ---------------------------------------------------------------
# 2. 파일 변환 함수들
# ---------------------------------------------------------------
def convert_with_pandoc(input_file, output_file):
    """Pandoc을 사용하여 파일을 마크다운으로 변환"""
    try:
        media_dir = output_file.parent / f"{output_file.stem}_media"
        
        cmd = [
            "pandoc",
            str(input_file),
            "-f", "docx",
            "-t", "markdown",
            "--extract-media", str(media_dir),
            "-o", str(output_file),
            "--wrap=none"
        ]
        
        result = subprocess.run(cmd, 
                               capture_output=True, 
                               text=True, 
                               timeout=60)
        
        if result.returncode == 0:
            logging.info(f"[CONVERT OK] {output_file.name}")
            return True
        else:
            logging.error(f"[CONVERT ERROR] {input_file.name}: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        logging.error(f"[TIMEOUT] {input_file.name}")
        return False
    except Exception as e:
        logging.error(f"[ERROR] {input_file.name}: {e}")
        return False

def convert_txt_to_md(input_file, output_file):
    """텍스트 파일을 마크다운으로 복사"""
    try:
        with open(input_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        md_content = f"# {input_file.stem}\n\n```\n{content}\n```\n"
        
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(md_content)
        
        logging.info(f"[TXT OK] {output_file.name}")
        return True
    except Exception as e:
        logging.error(f"[ERROR] {input_file.name}: {e}")
        return False

# ---------------------------------------------------------------
# 3. Base64 이미지 처리
# ---------------------------------------------------------------
def process_base64_images(md_file_path):
    """마크다운 파일 내의 base64 이미지를 파일로 추출"""
    md_path = Path(md_file_path)
    
    if not md_path.exists():
        return 0
    
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        logging.debug(f"[BASE64 READ ERROR] {md_path.name}: {e}")
        return 0
    
    # base64 이미지 패턴
    pattern = r'!\[\]\(data:image/([\w+]+);base64,([A-Za-z0-9+/=]+)\)'
    matches = list(re.finditer(pattern, content))
    
    if not matches:
        return 0
    
    logging.info(f"[BASE64] Found {len(matches)} embedded images in {md_path.name}")
    
    media_dir = md_path.parent / f"{md_path.stem}_media"
    media_dir.mkdir(exist_ok=True)
    
    extracted_count = 0
    new_content = content
    
    # 역순으로 처리하여 오프셋 문제 방지
    for idx, match in enumerate(reversed(matches)):
        real_idx = len(matches) - idx - 1
        img_type = match.group(1)
        b64_data = match.group(2)
        
        try:
            img_data = base64.b64decode(b64_data)
            
            ext = img_type.lower()
            if ext == 'tiff':
                ext = 'tif'
            elif ext == 'jpeg':
                ext = 'jpg'
            
            img_filename = f"image_{real_idx + 1:03d}.{ext}"
            img_path = media_dir / img_filename
            
            with open(img_path, 'wb') as img_file:
                img_file.write(img_data)
            
            rel_path = f"{md_path.stem}_media/{img_filename}"
            old_text = match.group(0)
            new_text = f"![]({rel_path})"
            
            start = match.start()
            end = match.end()
            new_content = new_content[:start] + new_text + new_content[end:]
            
            extracted_count += 1
            
        except Exception as e:
            logging.warning(f"[BASE64 DECODE ERROR] {e}")
            continue
    
    if extracted_count > 0:
        try:
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            logging.info(f"[BASE64 EXTRACTED] {extracted_count} images from {md_path.name}")
        except Exception as e:
            logging.error(f"[BASE64 WRITE ERROR] {md_path.name}: {e}")
    
    return extracted_count

# ---------------------------------------------------------------
# 4. 이미지 경로 수정
# ---------------------------------------------------------------
def fix_image_paths(md_file_path):
    """마크다운 파일의 잘못된 이미지 경로를 수정"""
    md_path = Path(md_file_path)
    
    if not md_path.exists():
        return False
    
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        logging.debug(f"[PATH FIX READ ERROR] {md_path.name}: {e}")
        return False
    
    original_content = content
    
    # supplementary_markdown\...\... 경로를 상대 경로로 변경
    pattern = r'!\[(.*?)\]\(supplementary_markdown\\[^\\]+\\([^)]+)\)'
    content = re.sub(pattern, r'![\1](\2)', content)
    
    # 역슬래시를 슬래시로 변경
    content = content.replace('\\', '/')
    
    if content != original_content:
        try:
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logging.info(f"[PATH FIXED] {md_path.name}")
            return True
        except Exception as e:
            logging.error(f"[PATH FIX WRITE ERROR] {md_path.name}: {e}")
            return False
    
    return False

# ---------------------------------------------------------------
# 5. 파일 변환 메인 함수
# ---------------------------------------------------------------
def convert_file(input_file, output_dir, use_pandoc=True):
    """파일을 마크다운으로 변환"""
    input_path = Path(input_file)
    output_path = output_dir / f"{input_path.stem}.md"
    
    # 이미 변환된 파일이 있으면 스킵
    if output_path.exists():
        logging.debug(f"[SKIP] Already exists: {output_path.name}")
        return True
    
    ext = input_path.suffix.lower()
    result = False
    
    if ext in ['.docx', '.doc']:
        if use_pandoc:
            result = convert_with_pandoc(input_path, output_path)
        else:
            logging.warning(f"[SKIP] Pandoc not available: {input_path.name}")
            result = False
    
    elif ext == '.txt':
        result = convert_txt_to_md(input_path, output_path)
    
    elif ext == '.pdf':
        if use_pandoc:
            result = convert_with_pandoc(input_path, output_path)
        else:
            logging.warning(f"[SKIP] PDF requires Pandoc: {input_path.name}")
            result = False
    
    elif ext in ['.html', '.htm']:
        if use_pandoc:
            result = convert_with_pandoc(input_path, output_path)
        else:
            logging.warning(f"[SKIP] HTML requires Pandoc: {input_path.name}")
            result = False
    
    else:
        logging.warning(f"[UNSUPPORTED] {ext}: {input_path.name}")
        result = False
    
    return result

# ---------------------------------------------------------------
# 6. 폴더 처리 메인 함수
# ---------------------------------------------------------------
def process_all(input_dir, output_dir, use_pandoc=True):
    """
    전체 프로세스 실행:
    1. DOCX → MD 변환
    2. Base64 이미지 추출
    3. 이미지 경로 수정
    """
    input_path = Path(input_dir)
    output_base = Path(output_dir)
    
    if not input_path.exists():
        raise FileNotFoundError(f"Directory not found: {input_dir}")
    
    # PII 폴더 수집
    pii_folders = [d for d in input_path.iterdir() if d.is_dir()]
    
    if not pii_folders:
        logging.warning(f"No subdirectories found in {input_dir}")
        return
    
    logging.info(f"Found {len(pii_folders)} PII folders")
    
    stats = {
        'converted': 0,
        'failed': 0,
        'skipped': 0,
        'base64_extracted': 0,
        'paths_fixed': 0
    }
    
    # 각 PII 폴더 처리
    for pii_folder in tqdm(pii_folders, desc="Processing folders"):
        pii_name = pii_folder.name
        output_folder = output_base / pii_name
        output_folder.mkdir(parents=True, exist_ok=True)
        
        files = list(pii_folder.iterdir())
        
        for file in files:
            if file.is_file():
                # 1. 파일 변환
                result = convert_file(file, output_folder, use_pandoc)
                if result:
                    stats['converted'] += 1
                else:
                    stats['failed'] += 1
            else:
                stats['skipped'] += 1
    
    # 2. 생성된 모든 마크다운 파일에 대해 후처리
    logging.info("Post-processing markdown files...")
    md_files = list(output_base.rglob("*.md"))
    
    for md_file in tqdm(md_files, desc="Post-processing"):
        # Base64 이미지 추출
        extracted = process_base64_images(md_file)
        if extracted > 0:
            stats['base64_extracted'] += extracted
        
        # 이미지 경로 수정
        if fix_image_paths(md_file):
            stats['paths_fixed'] += 1
    
    # 결과 요약
    logging.info("=" * 60)
    logging.info(f"✅ Conversion Complete!")
    logging.info(f"   Converted:        {stats['converted']} files")
    logging.info(f"   Failed:           {stats['failed']} files")
    logging.info(f"   Skipped:          {stats['skipped']} items")
    logging.info(f"   Base64 extracted: {stats['base64_extracted']} images")
    logging.info(f"   Paths fixed:      {stats['paths_fixed']} files")
    logging.info("=" * 60)

# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Convert supplementary files to Markdown (All-in-One)"
    )
    
    parser.add_argument(
        "--input_dir",
        default="./supplementary_files",
        help="Input directory containing PII subfolders"
    )
    
    parser.add_argument(
        "--output_dir",
        default="./supplementary_markdown",
        help="Output directory for converted markdown files"
    )
    
    parser.add_argument(
        "--use_pandoc",
        action="store_true",
        default=True,
        help="Use Pandoc for conversion (recommended)"
    )
    
    parser.add_argument(
        "--no_pandoc",
        action="store_true",
        help="Do not use Pandoc"
    )
    
    args = parser.parse_args()
    
    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )
    
    # Pandoc 확인
    use_pandoc = args.use_pandoc and not args.no_pandoc
    
    if use_pandoc:
        pandoc_available = check_pandoc()
        if not pandoc_available:
            logging.warning("Falling back to Python libraries...")
            use_pandoc = False
    
    # 전체 프로세스 실행
    try:
        process_all(args.input_dir, args.output_dir, use_pandoc=use_pandoc)
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        raise

if __name__ == "__main__":
    main()
