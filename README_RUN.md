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

Naz Telegram autoposting is owned by this project. VOID should not generate
Naz channel autoposts or share a scheduler with Naz.

Enabled by default:

```text
AUTOPOST_ENABLED=true
NAZ_TELEGRAM_AUTO_ON=true
BOT_TIMEZONE=Europe/Moscow
NAZ_TELEGRAM_AUTO_TIMES=09:30,13:30,17:30,21:30
NAZ_TELEGRAM_AUTO_TASKS=post,viral
```

The bot schedules posts at the comma-separated `NAZ_TELEGRAM_AUTO_TIMES`
values in `BOT_TIMEZONE`. Each slot selects a Naz rubric from the in-repo
rubric schedule, then writes only finished Naz posts to `naz_to_void` exchange
for adaptation.

Legacy `AUTOPOST_ENABLED`, `AUTOPOST_TIMES`, `AUTOPOST_TASKS`, and `CHANNEL_ID`
still work as fallbacks, but new Naz deployments should prefer the
`NAZ_TELEGRAM_*` variables.

## Images

The default image chain uses the official pinned FLUX.2 Pro endpoint first,
then Hugging Face, then a local Naz-branded card:

```text
IMAGE_PROVIDER=bfl
BFL_API_KEY=...
BFL_MODEL=flux-2-pro
BFL_IMAGE_WIDTH=1024
BFL_IMAGE_HEIGHT=1024
HF_TOKEN=...
ALLOW_IMAGE_FALLBACK=true
```

The BFL API is asynchronous. Naz submits a generation request, polls the
returned `polling_url`, and downloads the signed result immediately. If BFL is
unavailable or has no balance, Hugging Face is tried automatically. When HF
credits become available again, no code change is required. If both remote
providers fail, Pillow renders a square branded fallback locally; no random
stock-photo service is used. The card reuses the current Naz bot and
`@PromptOrDie` Telegram avatars when they are available.

## Naz VK

VK is configured as a separate Naz-owned contour. It can target the shared VK
public, but payloads, browser profile, helper mode, schedule, and rubrics belong
to this repo:

```text
NAZ_VK_ENABLED=false
NAZ_VK_PUBLIC_ID=
NAZ_VK_AUTO_ON=false
NAZ_VK_AUTO_TIMES=11:20,16:40,20:20
NAZ_VK_PAYLOAD_DIR=content_inbox/naz_vk_payloads
NAZ_VK_BROWSER_PROFILE_DIR=.browser_profiles/naz_vk
NAZ_VK_HELPER_MODE=naz
```

The current code registers the Naz VK configuration and creates the payload /
profile directories when enabled. A concrete VK browser helper can consume this
config without moving VK scheduling into VOID.

## Cross-posting

Naz and VOID exchange posts only through file queues under
`CROSSPOST_EXCHANGE_DIR`:

```text
void_to_naz/inbox
naz_to_void/inbox
```

The exchange contract is adaptation-only: one project can bring material to the
other, but neither project owns the other's scheduler.

## VPS

See `VPS_DEPLOY.md`.

## One-command Deploy

From PowerShell:

```powershell
.\deploy.ps1 -Message "Your commit message"
```

The script:

- commits and pushes code to GitHub;
- copies private runtime files to VPS: `.env`, `naz_stories.md`, `monitored_sources.json`, `content_inbox/agent_content`;
- runs `git pull --ff-only` on VPS;
- checks Python compilation;
- restarts `naz-ai-bot.service`.

Private files are ignored by git and are copied directly to VPS.
