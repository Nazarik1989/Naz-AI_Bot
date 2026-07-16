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

Autoposting is configured in `.env`:

```text
AUTOPOST_ENABLED=true
BOT_TIMEZONE=Europe/Moscow
AUTOPOST_TIMES=10:00,14:00,18:00,22:00
AUTOPOST_TASKS=post,viral
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

## Standalone VK producer timer

Production must set the following in the separate Naz environment file
`/etc/naz-ai-bot/naz.env`:

```text
NAZ_VK_ENABLED=true
NAZ_VK_SCHEDULER=systemd
NAZ_VK_QUEUE_DIR=/var/lib/void-vk-publisher/queue
DB_PATH=/var/lib/naz-ai-bot/naz_ai_bot.sqlite3
```

The deployment layer creates `/var/lib/naz-ai-bot`, `/var/cache/naz-ai-bot`,
the shared queue, and membership of user `naz` in supplementary group
`vkqueue`. It also installs the tracked units:

```bash
sudo install -m 0644 deploy/systemd/naz-vk-producer.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/naz-vk-producer.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now naz-vk-producer.timer
systemctl list-timers naz-vk-producer.timer
```

The timer has explicit Europe/Moscow slots at `11:20`, `16:40`, and `20:20`.
systemd cannot expand `NAZ_VK_AUTO_TIMES` from an EnvironmentFile into
`OnCalendar`; change the three `OnCalendar` lines in the timer, run
`systemctl daemon-reload`, and restart the timer when production slots change.
Keep `NAZ_VK_AUTO_TIMES` synchronized for documentation and optional local
`NAZ_VK_SCHEDULER=telegram` mode.

The oneshot service runs only `python -B -m naz_vk_producer`. Its writable
paths are limited to Naz data/cache and the shared `pending` inbox; publisher
profiles and private `processing`, `done`, and `failed` directories are not
available to it.
