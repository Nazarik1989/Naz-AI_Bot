# VPS Deploy

Example path on the VPS:

```bash
/opt/naz-ai-bot
```

## Install

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
sudo mkdir -p /opt/naz-ai-bot
sudo chown "$USER":"$USER" /opt/naz-ai-bot
cd /opt/naz-ai-bot
git clone YOUR_GITHUB_REPO_URL .
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
nano .env
```

Check startup:

```bash
.venv/bin/python -m py_compile main.py controller.py memory.py prompts.py
.venv/bin/python -c "import main; app = main.build_application(); print('build ok')"
```

## systemd Service

Create `/etc/systemd/system/naz-ai-bot.service`:

```ini
[Unit]
Description=Naz AI Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/naz-ai-bot
EnvironmentFile=/opt/naz-ai-bot/.env
ExecStart=/opt/naz-ai-bot/.venv/bin/python /opt/naz-ai-bot/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and inspect:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now naz-ai-bot
sudo systemctl status naz-ai-bot
journalctl -u naz-ai-bot -f
```

Never commit `.env` or `naz_ai_bot.sqlite3` to GitHub.
