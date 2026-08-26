#!/data/data/com.termux/files/usr/bin/bash
# ── Replitmail bot: one-shot Termux installer ────────────────────────────────
# Usage:  bash termux-setup.sh
set -e

echo "==> Updating Termux packages"
yes | pkg update -y || true
yes | pkg upgrade -y || true

echo "==> Installing build + runtime packages"
pkg install -y python python-pip git clang make binutils libffi openssl \
  libjpeg-turbo zlib brotli rust termux-api

echo "==> Creating virtual environment (.venv)"
cd "$(dirname "$0")"
python -m venv .venv
. .venv/bin/activate

echo "==> Upgrading pip toolchain"
pip install --upgrade pip setuptools wheel

echo "==> Installing Python dependencies"
if ! pip install -r requirements-termux.txt; then
  echo "!! Full install failed, retrying without Brotli (optional)"
  grep -v -i '^brotli' requirements-termux.txt > /tmp/req-nobrotli.txt
  pip install -r /tmp/req-nobrotli.txt
fi

echo "==> Preparing .env"
if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Enter your Telegram BOT_TOKEN (from @BotFather), or press Enter to edit later:"
  read -r TOKEN
  if [ -n "$TOKEN" ]; then
    printf 'BOT_TOKEN=%s\n' "$TOKEN" > .env
    echo "Optional: your Telegram numeric user id (OWNER_ID), press Enter to skip:"
    read -r OID
    [ -n "$OID" ] && printf 'OWNER_ID=%s\n' "$OID" >> .env
  else
    echo "Edit it later with:  nano .env"
  fi
fi

echo "==> Verifying installation"
bash verify-termux.sh

echo
echo "Setup complete. Start the bot with:   bash start-termux.sh"
