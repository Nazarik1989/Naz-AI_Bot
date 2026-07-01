# Naz_AI_Bot v2.4

Telegram bot for AI content generation, OpenRouter chat completions, SQLite memory,
Hugging Face image generation, channel publishing, and scheduled autoposting.

## Project Files

Required runtime files:

```text
main.py
controller.py
memory.py
prompts.py
requirements.txt
.env.example
```

Local-only files are ignored by git: `.env`, `.venv/`, SQLite databases,
archives, backups, patch folders, and `__pycache__/`.

## Local Start

```bash
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python.exe main.py
```

Fill `.env` before starting:

```text
BOT_TOKEN=Telegram bot token
ADMIN_ID=your Telegram user id
CHANNEL_ID=@channel_username or -100xxxxxxxxxx
OPENAI_API_KEY=OpenRouter API key
HF_TOKEN=Hugging Face token
```

## Health Check

```bash
.\.venv\Scripts\python.exe -m py_compile main.py controller.py memory.py prompts.py
.\.venv\Scripts\python.exe -c "import main; app = main.build_application(); print('build ok')"
```

## Commands

```text
/start - main menu
/menu - open menu
/help - help
/state - current mode
/roles - list roles
/role marketer - select expert mode
/voice tech_hooligan - select voice profile
/goal engagement - select content goal
/memory - memory summary
/clear - clear your memory

/post topic - regular post
/viral topic - viral post
/script topic - Reels script
/plan topic - content plan
/hooks topic - hooks
/imagepost topic - post with two images
/image topic - one image
/publish topic - generate and publish to channel

/stats - admin stats
```

## Autoposting

Enabled by default:

```text
AUTOPOST_ENABLED=true
BOT_TIMEZONE=Europe/Moscow
AUTOPOST_TIMES=10:00,20:00
AUTOPOST_TASKS=post,viral
```

The bot schedules posts at the comma-separated `AUTOPOST_TIMES` values in
`BOT_TIMEZONE`. `AUTOPOST_TASKS` can contain `post` and `viral`.

## VPS

See `VPS_DEPLOY.md`.
