#!/usr/bin/env bash
set -e

echo "======================================="
echo " LibreOffice installation for WSL"
echo "======================================="

# 1. System update
echo "[1/4] Updating package index..."
apt update

# 2. Install LibreOffice (full suite, headless usable)
echo "[2/4] Installing LibreOffice..."
apt install -y libreoffice

# 3. Verify soffice command
echo "[3/4] Verifying installation..."
if command -v soffice &> /dev/null; then
    echo "✅ LibreOffice installed successfully."
    echo "soffice path: $(which soffice)"
    echo "Version:"
    soffice --version
else
    echo "❌ soffice not found. Installation failed."
    exit 1
fi

# 4. Headless conversion test (optional)
echo "[4/4] Testing headless DOC → DOCX conversion (optional)..."
cat <<EOF > /tmp/lo_test.doc
This is a LibreOffice headless test.
EOF

# Convert test file
soffice --headless --convert-to docx --outdir /tmp /tmp/lo_test.doc >/dev/null 2>&1 || true

if [ -f /tmp/lo_test.docx ]; then
    echo "✅ Headless conversion test passed."
    rm -f /tmp/lo_test.doc /tmp/lo_test.docx
else
    echo "⚠️ Headless conversion test skipped or failed (this may be normal if .doc test file is minimal)."
fi

echo "======================================="
echo " LibreOffice setup completed."
echo "======================================="

