#!/bin/bash
# push_to_github.sh
# Projeyi GitHub'a push eder.
# Kullanım: bash push_to_github.sh
# NOT: Token'ı burada bırakma — push sonrası GitHub'dan revoke et ve yenisini oluştur.

set -e

REPO_URL="https://github.com/erenkumcuoglu/ollyfocusgroup.git"
BRANCH="main"

# Proje dizinine git (bu scriptin bulunduğu yer)
cd "$(dirname "$0")"

# Git yoksa init et
if [ ! -d ".git" ]; then
  git init
  git branch -M main
fi

# Remote yoksa ekle
if ! git remote get-url origin &>/dev/null; then
  git remote add origin "$REPO_URL"
else
  git remote set-url origin "$REPO_URL"
fi

# .gitignore oluştur (yoksa)
if [ ! -f ".gitignore" ]; then
cat > .gitignore << 'EOF'
.env
__pycache__/
*.pyc
*.pyo
reports/
.DS_Store
*.egg-info/
dist/
build/
.venv/
venv/
EOF
echo ".gitignore oluşturuldu"
fi

# Stage & commit
git add -A
git commit -m "feat: focus group test runner + dashboard" --allow-empty

# Push — token'ı komut satırından al
echo ""
echo "GitHub Personal Access Token gir (görünmez):"
read -s GIT_TOKEN

# Remote URL'ye token göm (geçici)
AUTHED_URL="https://erenkumcuoglu:${GIT_TOKEN}@github.com/erenkumcuoglu/ollyfocusgroup.git"
git remote set-url origin "$AUTHED_URL"

git push -u origin "$BRANCH" --force

# Token'ı URL'den temizle
git remote set-url origin "$REPO_URL"

echo ""
echo "✅ Push tamamlandı: $REPO_URL"
echo "⚠️  Kullandığın token'ı GitHub → Settings → Developer settings → Personal access tokens'tan REVOKE et."
echo "   Sonra yeni bir token oluştur."
