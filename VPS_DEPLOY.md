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

Production keeps Naz settings only in the canonical application environment
file `/opt/naz-ai-bot/.env`:

```text
NAZ_VK_ENABLED=true
NAZ_VK_TIMEZONE=Europe/Moscow
NAZ_VK_SCHEDULER=systemd
NAZ_VK_QUEUE_DIR=/var/lib/void-vk-publisher/queue
NAZ_VK_TRACK_STATE_FILE=/var/lib/naz-ai-bot/vk_track_rotation.json
NAZ_VK_IMAGE_POLICY=required
NAZ_VK_IMAGE_ATTEMPTS=2
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
/opt/naz-ai-bot/.venv/bin/python -B -m naz_vk_producer --check-config
```

The timer has an explicit Europe/Moscow daily slot at `10:30` and a gaming slot
at `16:30` on Tuesday, Thursday, and Sunday. systemd does not expand environment
variables inside `OnCalendar`; keep `NAZ_VK_DAILY_TIME` and
`NAZ_VK_GAMING_TIME` synchronized with the tracked timer when using the optional
local `NAZ_VK_SCHEDULER=telegram` mode.

Every job receives a query selected from the code-owned approved music catalog.
Naz checks both its own rotation state and the consumer-owned global last-eight
history, preventing reuse across Naz and VOID. If no eligible approved track
remains, the producer fails closed and does not enqueue a post. Images are
required by default and image attempts are bounded.

The oneshot service runs only `python -B -m naz_vk_producer`. Its writable
paths are limited to Naz data/cache and the shared `pending` inbox; publisher
profiles and private `processing`, `done`, and `failed` directories are not
available to it.
