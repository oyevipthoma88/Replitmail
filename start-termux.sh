#!/data/data/com.termux/files/usr/bin/bash
# ── Replitmail bot: Termux launcher (keeps running + auto-restarts) ──────────
cd "$(dirname "$0")"

# Stop Android from sleeping the CPU (needs Termux:API app, optional).
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true
trap 'command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock; exit 0' INT TERM

. .venv/bin/activate

mkdir -p logs
while true; do
  echo "[$(date '+%F %T')] starting bot" | tee -a logs/bot.log
  python main.py 2>&1 | tee -a logs/bot.log
  echo "[$(date '+%F %T')] bot exited, restarting in 10s" | tee -a logs/bot.log
  sleep 10
done
