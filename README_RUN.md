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
NAZ_TELEGRAM_AUTO_TIMES=10:00,14:00,18:00,22:00
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

The default image chain uses the OpenAI-compatible Images API through
OpenRouter, then BFL, Hugging Face, and finally the local Naz-branded fallback:

```text
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_IMAGE_MODEL=openai/gpt-image-2
OPENAI_IMAGE_SIZE=1024x1024
OPENAI_IMAGE_QUALITY=medium
IMAGE_PROVIDER=openai
BFL_API_KEY=...
BFL_MODEL=flux-2-pro
BFL_IMAGE_WIDTH=1024
BFL_IMAGE_HEIGHT=1024
FALLBACK_IMAGE_DIR=assets/fallback_images
HF_TOKEN=...
ALLOW_IMAGE_FALLBACK=true
```

The Images API uses the existing `OPENAI_API_KEY`, the configured
`OPENAI_BASE_URL`, and accepts either base64 image data or a provider URL. If it
fails, the asynchronous BFL API is tried, followed by Hugging Face. Only after
all remote providers fail does Pillow render a square branded fallback; no
random stock-photo service is used. The card reuses the current Naz bot and
`@PromptOrDie` Telegram avatars when they are available.

The canonical GPT Image 2 model ID is `openai/gpt-image-2`, verified through
OpenRouter's authenticated `GET /api/v1/images/models`. Naz never substitutes a
different OpenAI image model silently: an unavailable/rejected model is logged
and the existing BFL → Hugging Face → local fallback chain is used.

Files placed in `FALLBACK_IMAGE_DIR` take priority over the generated card.
Naz randomly selects a JPG, PNG, or WebP from that directory and center-crops
it to 1024x1024. The avatar card is used only when the directory is empty.

Image-first publishing is a separate, review-gated path:

```text
VISUAL_ARCHIVE_ENABLED=false
VISUAL_ARCHIVE_ROOT=images_curated
VISUAL_ARCHIVE_MANIFEST=images_curated/catalog/publication_candidates.json
VISUAL_ARCHIVE_STATE_FILE=.visual_archive_seen.json
VISUAL_ARCHIVE_REQUIRE_APPROVED=true
VISUAL_ARCHIVE_EVERY_N_POSTS=3
```

When enabled, Naz selects an approved unused visual first, writes a post around
its OCR meaning and rubric, preserves the original aspect ratio, then records
the image ID in a separate seen-state file. It falls back to the normal
topic-first autopost loop when no eligible visual is available. The persistent
cadence counter uses one visual on every third scheduled post by default, so
visual posts never run consecutively and rotate through the daily time slots.

## Naz VK

VK is configured as a separate Naz-owned producer contour. It can target the
shared VK public, but Naz owns only content generation and queue scheduling:

```text
NAZ_VK_ENABLED=false
NAZ_VK_PUBLIC_ID=
NAZ_VK_AUTO_ON=false
NAZ_VK_DAILY_TIME=10:30
NAZ_VK_GAMING_TIME=16:30
NAZ_VK_TIMEZONE=Europe/Moscow
NAZ_VK_SCHEDULER=systemd
NAZ_VK_QUEUE_DIR=/var/lib/void-vk-publisher/queue
NAZ_VK_TRACK_STATE_FILE=/var/lib/naz-ai-bot/vk_track_rotation.json
NAZ_VK_IMAGE_POLICY=required
NAZ_VK_IMAGE_ATTEMPTS=2
```

Production uses the tracked `naz-vk-producer.service` and `.timer`. The timer
creates one queue job per invocation without starting Telegram polling. Set
`NAZ_VK_SCHEDULER=telegram` only for local compatibility with the in-process
JobQueue schedule, or `off` to disable both scheduler registrations. The
standalone command itself remains available when the scheduler mode is `off`.

Every VK job receives a track from the approved code-owned catalog. Daily and
gaming producers also read the publisher-owned global last-eight history, so
Naz and VOID cannot reuse each other's recent tracks. If no eligible approved
track is available, no job is enqueued. Images are required by default and
generation is bounded by `NAZ_VK_IMAGE_ATTEMPTS`; `text_music` is the only
explicit policy that permits a job without media.

Before enabling a production timer, run the read-only preflight as root:

```bash
/opt/naz-ai-bot/.venv/bin/python -B -m naz_vk_producer --check-config
```

The check validates the canonical environment, publisher allowlist, queue write
scope, API configuration, catalog/history readability, and the tracked
Europe/Moscow schedule. It never generates content or writes the DB or queue.

Naz creates only canonical filesystem jobs in the deployment-owned `pending`
inbox. Browser state, VK credentials, consumption, and publication remain
outside this process.

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
