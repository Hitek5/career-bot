# CareerBot 🤖

AI-powered Telegram career assistant: vacancy analysis, resume generation, digital profile.

## Features

- **Digital Profile** — upload documents (PDF/DOCX), answer questions → AI creates a structured career profile
- **Vacancy Analysis** — send hh.ru link or text → company research, match %, recommendations
- **Resume Generation** — 1-page PDF (designer layout) + DOCX (HH format), adapted per vacancy
- **Multi-user** — whitelist-based access, each user has isolated profile

## Stack

- Python 3.11+, python-telegram-bot v21
- Claude API (Anthropic) — analysis & generation
- SQLite + SQLAlchemy — data storage
- WeasyPrint — PDF generation
- python-docx — DOCX generation
- HH.ru API — vacancy parsing (no auth needed)

## Setup

```bash
# Clone
git clone https://github.com/Hitek5/career-bot.git
cd career-bot

# Install dependencies
pip install -r requirements.txt

# System deps for WeasyPrint
apt install -y libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0

# Configure
export BOT_TOKEN="your-telegram-bot-token"
export ANTHROPIC_API_KEY="your-anthropic-key"

# Add users to whitelist
cat allowed_users.json
# [{"tg_id": 123456789, "name": "User", "role": "admin"}]

# Run
python bot.py
```

## Systemd Service

```bash
cat > /etc/systemd/system/career-bot.service << 'EOF'
[Unit]
Description=CareerBot Telegram
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/career-bot
Environment=BOT_TOKEN=your-token
Environment=ANTHROPIC_API_KEY=your-key
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable career-bot
systemctl start career-bot
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Start / main menu |
| `/profile` | Show digital profile |
| `/history` | Vacancy analysis history |
| `/update` | Update profile with new documents |
| `/help` | Help |

## Flow

1. `/start` → upload documents → answer questions → AI profile created
2. Send hh.ru link → analysis with match %, gaps, recommendations
3. Click "Generate resume" → PDF + DOCX adapted for the vacancy

## License

MIT
