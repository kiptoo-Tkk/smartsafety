#!/bin/bash
# Connects the Pi's code folder to YOUR private GitHub repository (one time).
# Usage:  bash setup_github.sh git@github.com:YOUR_USER/YOUR_REPO.git ["Your Name"] [you@example.com]
set -e
URL="$1"; NAME="${2:-SmartSafety Pi}"; MAIL="${3:-smartsafety@users.noreply.github.com}"
if [ -z "$URL" ]; then echo 'Usage: bash setup_github.sh git@github.com:USER/REPO.git ["Name"] [email]'; exit 1; fi
APP="$HOME/motor-safety/pi"; KEY="$HOME/.ssh/smartsafety_github"
command -v git >/dev/null || sudo apt-get install -y git
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "smartsafety-pi" -f "$KEY"
ssh-keyscan -t ed25519 github.com 2>/dev/null >> "$HOME/.ssh/known_hosts" || true
cd "$APP"
cat > .gitignore <<'EOF'
.venv/
__pycache__/
*.pyc
app.log
app.py.*
*.bak
backup_*/
apply_*.py
install_smartsafety*.sh
data/auth.json
data/secret.key
EOF
[ -d .git ] || git init -q -b main
git config user.name "$NAME"
git config user.email "$MAIL"
git config core.sshCommand "ssh -i $KEY -o IdentitiesOnly=yes"
git remote remove origin 2>/dev/null || true
git remote add origin "$URL"
echo
echo "=== ONE MORE STEP, in your browser ==="
echo "GitHub -> your repository -> Settings -> Deploy keys -> Add deploy key"
echo "Title: smartsafety-pi | paste the key below | TICK 'Allow write access' | Add key"
echo
cat "$KEY.pub"
echo
echo "Then upload from the website: Settings -> GitHub Backup -> enter the password -> Upload."
