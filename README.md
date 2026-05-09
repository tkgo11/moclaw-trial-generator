# moclaw-trial-generator

Automates [MoClaw](https://moclaw.ai) free-trial registration using disposable email addresses from [mail.gw](https://mail.gw). No credit card required.

## Features

- 🤖 Fully automated — temp email creation, OTP detection, login, done
- 📬 Uses [mail.gw](https://mail.gw) disposable inboxes (no signup needed)
- 🔁 **Bulk mode** — register N accounts in parallel with configurable workers
- 💾 Saves all credentials to a JSON file
- 🌐 Anti-detect browser via [camoufox-cli](https://github.com/daijro/camoufox) (bypasses bot detection)

## Requirements

```bash
pip install requests
pip install camoufox-cli   # or however it's installed in your env
camoufox-cli install       # downloads the browser binary
```

## Usage

### Single account
```bash
python3 moclaw_register.py
python3 moclaw_register.py --headed        # show the browser window
python3 moclaw_register.py --inbox         # dump inbox after login
```

### Bulk accounts
```bash
# Register 5 accounts (2 parallel workers by default)
python3 moclaw_register.py --bulk 5

# 10 accounts, 4 at a time
python3 moclaw_register.py --bulk 10 --workers 4

# Custom output file
python3 moclaw_register.py --bulk 3 --output accounts.json
```

## Output

All credentials are saved to `moclaw_credentials.json` (appended on each run):

```json
[
  {
    "email": "abc123@oakon.com",
    "mailgw_password": "xxxxxxxxxxxx",
    "mailgw_token": "eyJ...",
    "moclaw_url": "https://moclaw.ai/chat",
    "page_title": "MoClaw Chat",
    "registered_at": "2026-05-09T12:00:00+00:00",
    "success": true,
    "index": 1
  }
]
```

## How It Works

1. **Creates a disposable inbox** via `mail.gw` API
2. **Opens MoClaw `/auth`** in an isolated anti-detect browser session
3. **Submits the temp email** → MoClaw sends a 6-digit OTP
4. **Polls the inbox** every 5s (up to 90s) until the OTP arrives
5. **Enters the OTP** and waits for redirect to `/chat`
6. **Saves credentials** and closes the browser session

## Options

| Flag | Description |
|------|-------------|
| `--headed` | Show the browser window while it runs |
| `--inbox` | Print inbox contents after single registration |
| `--bulk N` | Register N accounts in bulk |
| `--workers W` | Number of parallel workers (default: 2) |
| `--output FILE` | Output JSON file (default: `moclaw_credentials.json`) |

## Notes

- Temp inboxes expire after ~10 minutes — save your credentials promptly
- Each bulk worker uses its own isolated browser profile and session
- The script cleans up browser profiles after each registration
