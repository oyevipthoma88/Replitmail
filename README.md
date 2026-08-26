# Replitmail — Real-Provider Mail Bot

Telegram bot jo **sirf real email providers** ke address deta hai — Gmail /
GoogleMail / Outlook family. Saare temporary-mail services (mail.tm, mail.gw,
temp-mail.io, TempMail Plus, relay aliases) hata diye gaye hain, kyunki
Bolt.new, GitHub aur Lovable un domains ko block karte hain.

## Allowed domains

`gmail.com`, `googlemail.com`, `outlook.com`, `hotmail.com`, `live.com`, `msn.com`
[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/oyevipthoma88/Replitmail)

Har address dene se pehle live check hota hai: MX record, inbox reachability aur
target-site (Lovable / GitHub / Bolt.new) acceptance.

## Options

| Option | Kya hai |
|---|---|
| **Apna khud ka Gmail** | `/gmail you@gmail.com` set karo — bot uske naye dot-alias deta hai. Verification mail aapke **asli inbox** me aata hai, "already used" kabhi nahi aata. Bolt.new ke liye sabse pakka. |
| **Real Outlook / Hotmail** | 22.do se plus-free `@outlook.com` / `@hotmail.com` mailbox (bot inbox padh sakta hai). |
| **Real Gmail (Emailnator pool)** | Shared public Gmail pool — bot inbox padh sakta hai. |
| **Real Gmail (22.do pool)** | Doosra Gmail pool, jab pehla pool burn ho chuka ho. |

## Commands

| Kaam | Command |
|---|---|
| Menu | `/start` |
| Best available mailbox | `/new` |
| Real Outlook mailbox | `/outlook` |
| Target ke liye mailbox | `/lovable`, `/github`, `/bolt` |
| Specific option | `/bolt1`, `/bolt2`, … (`/options bolt` se list) |
| Apne Gmail ke alias | `/gmail you@gmail.com` |
| Apne Outlook ke alias | `/outlookmail you@outlook.com` |
| Address check karo | `/check someone@gmail.com` |
| Inbox | `/inbox` |
| Auto-check | `/watch` / `/stopwatch` |
| Mail delete | `/delete <id>` |
| Help | `/help` |

## Bolt.new verification mail kyun nahi aata tha?

1. Bolt har temp-mail domain reject karta hai — sirf real providers chalte hain.
2. Shared pools (Emailnator / 22.do) ke base accounts Bolt par **pehle se
   registered** hote hain, to Bolt naya verification mail bhejta hi nahi.
3. Emailnator inbox **exact address form** par keyed hai: `@gmail.com` par bheja
   mail `@googlemail.com` poll karne se dikhta hi nahi tha.

Fix: bot ab dono domain variants (`@gmail.com` + `@googlemail.com`) ek saath
poll karta hai, Bolt-acceptance address dene se pehle probe karta hai, aur
Bolt ke liye pehle aapka **khud ka Gmail alias** offer karta hai.

## Setup

```bash
pip install -r requirements.txt
export BOT_TOKEN=your_telegram_bot_token
python main.py
```

`BOT_TOKEN` ke alawa koi env var zaroori nahi.

## Termux (Android)

Run it straight from your phone — see [TERMUX.md](TERMUX.md):

```bash
pkg install -y git && git clone https://github.com/oyevipthoma88/Replitmail.git
cd Replitmail && bash termux-setup.sh && bash start-termux.sh
```
