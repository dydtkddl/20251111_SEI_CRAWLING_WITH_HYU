#!/bin/bash
# WSL에서 실행하세요
# 사용법: bash clean_git.sh

cd /mnt/d/20251111_SEI_CRAWLING_WITH_HYU

# 1. 기존 .git 삭제 & 새 저장소
rm -rf .git
git init

# 2. .gitignore 생성
cat > .gitignore << 'EOF'
*
!*/
!*.py
!*.ipynb
!*.sh
!*.bat
!*.yaml
!*.toml
!*.gitignore
!README*
pw_profile/
__pycache__/
.ipynb_checkpoints/
EOF

# 3. 코드 파일만 add
git add .gitignore
find . -name "*.py"     -not -path "*/pw_profile/*" -not -path "*/__pycache__/*" | xargs -r git add -f
find . -name "*.ipynb"  -not -path "*/pw_profile/*" -not -path "*/.ipynb_checkpoints/*" | xargs -r git add -f
find . -name "*.sh"     -not -path "*/pw_profile/*" | xargs -r git add -f
find . -name "*.bat"    -not -path "*/pw_profile/*" | xargs -r git add -f
find . -name "*.yaml"   -not -path "*/pw_profile/*" | xargs -r git add -f
find . -name "*.toml"   -not -path "*/pw_profile/*" | xargs -r git add -f

# 4. 확인
echo ""
echo "===== 스테이징된 파일 수 ====="
git diff --cached --name-only | wc -l

echo ""
echo "===== 확장자별 현황 ====="
git diff --cached --name-only | grep -oE '\.[^./]+$' | sort | uniq -c | sort -rn

echo ""
echo "===== 파일 목록 ====="
git diff --cached --name-only

echo ""
echo "========================================="
echo "위 목록을 확인하세요."
echo "문제 없으면 아래 명령을 순서대로 실행:"
echo "========================================="
echo ""
echo "  git commit -m 'initial commit (code only)'"
echo "  git remote add origin https://github.com/dydtkddl/20251111_SEI_CRAWLING_WITH_HYU.git"
echo "  git push -u origin main --force"
echo ""
echo "(GitHub에서 저장소를 삭제 후 빈 저장소로 재생성했다면 --force 없이 push 가능)"

