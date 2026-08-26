# Running the bot on Termux (Android)

Complete, copy-paste command list. Works on Termux from **F-Droid** or GitHub
(the Play Store build is outdated and will fail).

## 1. One-time Termux prep

```bash
pkg update -y && pkg upgrade -y
pkg install -y git
termux-setup-storage      # allow the storage permission popup
```

## 2. Get the code

```bash
cd ~
git clone https://github.com/oyevipthoma88/Replitmail.git
cd Replitmail
```

Private repo? Use a token:

```bash
git clone https://<YOUR_GITHUB_TOKEN>@github.com/oyevipthoma88/Replitmail.git
```

## 3. Install everything (one command)

```bash
bash termux-setup.sh
```

This installs `python`, `clang`, `libffi`, `openssl`, `rust`, `brotli`,
`termux-api`, creates `.venv`, installs the Python packages from
`requirements-termux.txt`, asks for your `BOT_TOKEN`, and runs the verifier.

## 4. Configure (if you skipped the prompt)

```bash
nano .env
```

```env
BOT_TOKEN=123456:ABC-your-token-from-@BotFather
OWNER_ID=8098146730
OWNER_USERNAME=@TheY_CaIl_mE_OG
SUPPORT_CHANNEL=https://t.me/ApexAssociation
```

Save with `CTRL+O`, `Enter`, exit with `CTRL+X`.

## 5. Verify

```bash
bash verify-termux.sh
```

Expect `All checks passed.` It checks the venv, Python version, every module,
`BOT_TOKEN`, code syntax, a real import of `main.py`, and Telegram reachability.

## 6. Run

```bash
bash start-termux.sh
```

Keeps a wake-lock, logs to `logs/bot.log`, and auto-restarts on a crash.
Stop it with `CTRL+C`.

Run in the background instead:

```bash
nohup bash start-termux.sh > /dev/null 2>&1 &
tail -f logs/bot.log        # watch logs
pkill -f main.py            # stop the bot
```

## 7. Auto-start on phone boot (optional)

```bash
pkg install -y termux-services
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/replitmail <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/Replitmail && bash start-termux.sh
SH
chmod +x ~/.termux/boot/replitmail
```

Install the **Termux:Boot** app (F-Droid) and open it once.

## 8. Update to the latest code

```bash
cd ~/Replitmail
git pull
. .venv/bin/activate && pip install -r requirements-termux.txt
bash verify-termux.sh
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `pip` build fails on `aiohttp` / `Brotli` | `pkg install -y clang make binutils python-dev` then rerun step 3. Brotli is optional — the bot works without it. |
| `KeyError: 'BOT_TOKEN'` | `.env` missing or empty → `nano .env` |
| `Conflict: terminated by other getUpdates` | The same token is running elsewhere (Replit/Heroku). Stop the other instance. |
| Bot dies when the screen locks | Install Termux:API, run `termux-wake-lock`, and disable battery optimisation for Termux. |
| `SSL: CERTIFICATE_VERIFY_FAILED` | `pkg install -y ca-certificates openssl && pkg upgrade -y` |
| `command not found: python` | `pkg install -y python` |
| Slow/failed downloads | `termux-change-repo` and pick a nearby mirror. |

Note: `requirements.txt`, `Procfile`, `runtime.txt` and `app.json` stay for
Replit/Heroku deploys — Termux uses `requirements-termux.txt` instead.
