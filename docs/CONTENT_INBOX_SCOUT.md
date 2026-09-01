# Content Inbox Scout

Content Inbox Scout is a manual, admin-only archive ranking boundary. It reads only the configured project under `AGENT_CONTENT_INBOX`, performs deterministic local discovery and prefiltering without a model, then uses one closed `CONTENT_MODEL_NAME` ranking request.

## Operator interface

- `/inbox_best`
- `/inbox_best 3 reel`
- `/inbox_best_refresh`
- Content menu: `🔥 Лучшее из Inbox`

The normal command reuses the immutable ranking for the same source snapshot. Refresh is explicit and request-bound. The maximum displayed result count is five.

Each private result card provides `Подготовить материал`, `Подробнее`, and `Скрыть`. Details are served from the stored ranking with no provider call. Preparation uses one closed `content-inbox-ready-material-v1` call for only the selected candidate. `Выбрать`, `Другой из TOP`, and `Пропустить` only update private operator state; they do not publish or start media production.

## Contracts and limits

- Run: `content-inbox-scout-run-v1`
- Ranking: `content-inbox-scout-ranking-v1`
- Prepared material: `content-inbox-ready-material-v1`
- Preference: `content-inbox-scout-preference-v1`
- At most 500 locally discovered candidates and 12 candidates in the model shortlist
- One ranking call per new snapshot; no retry or repair
- One preparation call per selected candidate; no retry or repair
- Short Reel recommendations: 12–20 seconds and 4–7 scenes

The ranking is not the final ordering authority. Code applies the fixed 35/35/15/10/5 weighting and deterministic penalties and tie-breakers.

## Privacy and state

The scanner accepts only regular, non-symlink Markdown files under exact project/date directories. It enforces per-file and aggregate size limits, rejects path escape and technical-noise candidates, and receives the existing Naz risk detector and redactor as injected dependencies. Paths, filenames, raw manifests, source hashes, secrets, and redacted original values are not sent to the model or Telegram.

Private state defaults to `/var/lib/naz-ai-bot/content-inbox-scout`. Directories are mode `0700`; files are atomically created with mode `0600`; existing artifacts are immutable and conflicting reuse fails closed. The state root must remain outside the repository, Inbox, Registry, and Normalizer roots.

The module imports without scanning, provider construction, filesystem writes, or network calls. There is no schedule. It does not import or invoke Narrative Normalizer, Review Authority Broker, Renderer, or publication code.

## One-shot production invocation

After a reviewed deploy, a single private ranking can be delivered without starting a second bot process:

```text
python tools/run_content_inbox_scout.py --count 3 --format reel --request-id <unique-id>
```

This performs the local scan and at most one ranking call. It does not prepare a candidate automatically.
