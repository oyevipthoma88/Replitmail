#!/data/data/com.termux/files/usr/bin/bash
# ── Replitmail bot: environment self-check ──────────────────────────────────
cd "$(dirname "$0")"
ok=0

say() { printf '%-34s %s\n' "$1" "$2"; }
fail() { ok=1; }

[ -d .venv ] && say "virtualenv (.venv)" "OK" || { say "virtualenv (.venv)" "MISSING - run termux-setup.sh"; fail; }
[ -d .venv ] && . .venv/bin/activate

say "python" "$(python -V 2>&1)"

for m in telegram aiohttp httpx dotenv; do
  if python -c "import $m" 2>/dev/null; then say "module $m" "OK"; else say "module $m" "MISSING"; fail; fi
done
python -c "import brotli" 2>/dev/null && say "module brotli (optional)" "OK" || say "module brotli (optional)" "not installed (fine)"

if [ -f .env ] && grep -q '^BOT_TOKEN=.\+' .env && ! grep -q 'your_telegram_bot_token_here' .env; then
  say ".env BOT_TOKEN" "OK"
else
  say ".env BOT_TOKEN" "NOT SET - edit .env"; fail
fi

if python -m py_compile *.py 2>/dev/null; then say "python syntax" "OK"; else say "python syntax" "FAILED"; fail; fi

if [ -f .env ]; then
  cat > /tmp/_rm_import_check.py <<'PY'
import os, sys
sys.path.insert(0, os.getcwd())
from dotenv import load_dotenv
load_dotenv(".env")
os.environ.setdefault("BOT_TOKEN", "0:test")
import main  # noqa: F401
print("import OK")
PY
  if python /tmp/_rm_import_check.py >/dev/null 2>/tmp/import_err.txt; then say "bot imports" "OK"; else say "bot imports" "FAILED (see /tmp/import_err.txt)"; fail; fi
fi

if python -c "import socket; socket.create_connection(('api.telegram.org',443),8)" 2>/dev/null; then
  say "network -> api.telegram.org" "OK"
else
  say "network -> api.telegram.org" "UNREACHABLE"; fail
fi

echo
[ $ok -eq 0 ] && echo "All checks passed." || echo "Some checks failed - fix the lines above."
exit $ok
