# Naz Architecture Notes

## Ownership

Naz owns its own Telegram channel schedule, auto loop, prompts, rubrics, and
style. VOID must not generate Naz autoposts and must not call a shared scheduler
for Naz output.

Naz may post into a shared VK public, but the VK schedule still belongs to the
Naz project. VK payloads, browser profile, helper mode, and rubrics are
configured through `NAZ_VK_*` settings.

## Naz Telegram

Telegram autoposting uses:

```text
NAZ_TELEGRAM_CHANNEL_ID
NAZ_TELEGRAM_AUTO_ON
NAZ_TELEGRAM_AUTO_TIMES
NAZ_TELEGRAM_AUTO_TASKS
```

The default slots are:

```text
09:30 - Утренний дожим
13:30 - AI без магии
17:30 - Баг, который стал системой
21:30 - Naz после смены
```

Each slot chooses a Naz rubric, topic, task, and voice profile inside `main.py`.
Generated posts are saved as `naz_telegram_autopost:*` so memory, logs, and
exchange payloads can distinguish them from manual posts and VOID adaptations.

## Naz VK

VK config is deliberately separate:

```text
NAZ_VK_ENABLED
NAZ_VK_PUBLIC_ID
NAZ_VK_AUTO_ON
NAZ_VK_AUTO_TIMES
NAZ_VK_QUEUE_DIR
```

Current Naz VK rubrics:

```text
Naz Dev Log
AI без успешного успеха
Ошибка недели
```

Naz is only a producer of `vk_publish_job.v1` directories under
`NAZ_VK_QUEUE_DIR/pending`. It has no VK credentials, cookies, browser profile,
Playwright, or direct publishing code. The shared VK Publisher consumes these
jobs independently. The canonical queue root is
`/var/lib/void-vk-publisher/queue`; its directories are group-owned by
`vkqueue`. Producer and consumer services use `UMask=0027`; Naz also writes job
files as `0640`, job directories as `0750`, and keeps the setgid bit on
`pending` so new jobs inherit the queue group.

## Cross-posting

Cross-posting is adaptation-only through `CROSSPOST_EXCHANGE_DIR`:

```text
void_to_naz/inbox
naz_to_void/inbox
```

Naz -> VOID means: Naz writes a finished Naz post into `naz_to_void` for VOID to
adapt in its own voice.

VOID -> Naz means: VOID writes its own fragment into `void_to_naz`; Naz turns it
into a Naz channel draft or publication through `generate_void_crosspost`.

No shared scheduler. No direct foreign autopost generation.
