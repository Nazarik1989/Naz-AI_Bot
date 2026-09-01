# Operator content package v1

`operator-content-package-v1` is a closed, admin-only boundary for material
that has already received editorial preparation. It does not use the Narrative
Normalizer, evidence extraction/adjudication, or any narrative provider.

The stored JSON has exact keys and plain JSON types. It binds source
provenance, the editorial disclaimer, F1–F7-style approved facts, prohibited
claims, public adaptations, a contiguous ordered Reel scene plan, voice-over,
caption, cover and music briefs, rights state, operator request identity and
publication restrictions. Additional fields and type coercion are rejected.

Private packages live outside Inbox, quarantine Registry, Normalizer attempts,
Broker authority history and publication queues. Creation is no-clobber and
request-id replay is byte-idempotent; divergent reuse is a conflict. Callback
records bind the operator, request, package, digest and intended action.

## Telegram flow

An admin may send a UTF-8 `.md` editorial package or a v1 `.json` document.
Naz validates and stores it before returning a private card with:

- `Собрать Reel`
- `Показать сценарий`
- `Пропустить`

Script display is local and model-free. The build action first checks the
actual worker contract. The current story worker accepts 4–7 scenes and a
12–20-second Reel, so a 9-scene/47-second package fails closed instead of being
truncated, rewritten, or submitted to a provider.

A future compatible private Reel uses a second keyboard (`Опубликовать`,
`Переделать`, `Отменить`). This boundary never auto-publishes; publication
requires another explicit admin action and a separately enabled publication
adapter.

## Rights

`UNCLEAR_DO_NOT_USE` requires `music_brief.track = null`. Import and a no-music
private preview are allowed; embedding music or claiming cleared rights is not.

## Controlled CLI import

The production operator can use the same contract without sending the source
document through Telegram:

```text
python tools/import_operator_content_package.py \
  --source-markdown PACKAGE.md \
  --operator-request-id UNIQUE_REQUEST_ID \
  --send-preview
```

The CLI verifies `ADMIN_ID`, writes only the private package root, and sends
one private preview. It does not build media or publish.
