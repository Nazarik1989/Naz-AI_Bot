# Narrative Normalizer

Narrative Normalizer превращает подтверждённые материалы из Content Inbox в
понятные черновики историй. Он работает отдельно от Telegram-бота и ничего не
публикует.

Open-domain разделы ниже задают обязательный contract этого checkpoint. Они не
являются заявлением, что ещё не выполненные test gates уже прошли.

## Что означает `needs_narrative`

Это материал, для которого ещё нет проверенной человеческой истории. Исходные
файлы остаются на месте. Normalizer читает их полностью, вычисляет цифровой
отпечаток и выделяет только подтверждённые факты в исходном порядке.

Normalizer обязан классифицировать каждый элемент очереди, но не обязан делать
из каждого элемента публикуемую историю. Четырнадцать существующих точных
грамматик остаются неизменяемыми детерминированными fast path. Незнакомая
грамматика сама по себе больше не означает недостаточность: сначала применяется
общая сегментация и проверяемое извлечение evidence. Если подтверждённого
материала действительно не хватает, результатом становится
`source_insufficient`; если материал богатый, но допускает несколько честных
толкований, результатом становится `manual_attention`. Normalizer не заполняет
пробелы вымышленными пользователями, сбоями, деньгами, эмоциями или
последствиями.

## Что делает Naz

Для exact fast path сохраняется короткий bounded flow. Для неизвестной
грамматики сначала один запрос извлекает структурированное evidence, а отдельный
запрос независимо adjudicates каждый evidence item. Только после code-owned
проверки evidence один запрос создаёт ограниченный набор вариантов истории, а
ещё один независимо adjudicates public claims. При исправимой структурной
ошибке истории разрешён максимум один repair-запрос. Naz и VOID выбираются по
подтверждённым фактам и переданному канону: ни один из них не обязан быть героем,
критиком или участником каждой истории.

Модели не зашиты в код. Клиент и контекст передаются через явный adapter. Для
будущей настройки generic path рекомендуется:

- evidence extraction: Terra, reasoning medium;
- evidence adjudication: SOL, reasoning high;
- story generation: Terra, reasoning medium;
- story adjudication: SOL, reasoning high;
- единственное исправление истории: Terra, reasoning high.

Happy path использует ровно четыре model calls; repair увеличивает максимум до
пяти. Скрытые retry, второй repair, отдельный запрос на каждую claim и вызовы
Director/StoryPack запрещены. Эти имена являются рекомендацией, а не
выдуманным provider mapping.

## Open-domain source и evidence

Общий путь не добавляет новую Python-грамматику для каждого формата. Он строит
immutable `SourceDocument`, связанный с `source_ref`, `source_digest`, версией
source contract и media type. Документ сохраняет точный исходный текст и
упорядоченные `SourceSegment` со следующими координатами:

- byte и character start/end;
- line start/end;
- exact text;
- закрытый `segment_kind`;
- container path для JSON-like источника.

Сегментация должна поддерживать plain text, multiline logs, Markdown,
структурированный JSON, key/value reports, chat/email-like записи и смешанный
русско-английский технический текст. Она не изменяет исходные evidence-строки и
не нормализует числовые lexemes.

Generic extractor возвращает закрытый набор `SourceEvidence`: proposition,
evidence kind, ordered segment refs, exact quotes, entities, numbers, dates,
polarity, temporal/causal relation, uncertainty и public-safety status. Каждый
обычный evidence item обязан иметь хотя бы один точный span; исключение — явный
`insufficient_or_ambiguous` outcome. Независимый adjudicator возвращает решение
для каждого предложенного evidence ID. Множества proposed и adjudicated IDs
должны совпадать точно; missing, extra, duplicate и stale решения fail closed.

После model adjudication application code независимо проверяет refs, offsets,
quotes, числа, даты, entities, relation operands, causal language, polarity,
metaphor/literal distinction, source identity и source freshness. Только
принятое evidence может быть проецировано в CP1/CP2 `SourceFact`; приложение,
а не модель, определяет итоговый порядок и cardinality фактов.

Current Normalizer policy requires at least **two** independently adjudicated
verified fact bindings before CP1/CP2 projection (`MIN_SOURCE_FACTS = 2`). CP1
and CP2 themselves accept a non-empty fact tuple, but CP6B deliberately requires
two propositions as its minimum narrative-sufficiency gate. One valid evidence
item is classified as `source_insufficient`; no synthetic second, fourth or
fifth fact is invented. Five public fields may reuse those verified facts only
through the existing bounded statement adjudication.

## Что сохраняется

Для каждой составной identity источника создаётся внешний каталог:

```text
<outbox>/<source-identity>/
    story.md
    story.json
    draft-manifest.json
    review.json
```

`source-identity` is byte-exact:
`sha256(source_ref + NUL + source_digest + NUL + source_contract_version)`.
Equal bytes under different `source_ref` values therefore have independent
drafts, locks, claims, reviews, and approvals. The old digest-only directory is
accepted only as an unambiguous legacy location; legacy plus identity manifests
fail closed.

`story.json` contains five closed public claims plus code-owned factuality and
meaning-preservation receipts. Public fields and `story.md` are reconstructed
only from those ordered claims. Approval rereads the raw source and recomputes
claims, factuality, meaning, plain-language status, identities, and digests; a
stored `passed` or zero counter is never trusted.

The accepted CP2 `HumanStoryPackage` is stored together with the final typed
generation/adjudication evidence that CP2 actually parsed (after its bounded
repair, if any). The evidence binds the authority context, selected draft and
candidate, exact five ordered statement decisions, source refs, statement and
claim digests, package digest, and CP1 validation context. Raw prompts and raw
provider replies are never persisted. Its evidence digest, package digest,
selected candidate, inference kinds, and ordered claim digests are sealed into
the terminal completed claim. Approval reconstructs the typed evidence, reruns
CP1 validation, and requires every sealed value to match, with no model call.

На generic path CP2 получает только факты, построенные из принятого evidence.
Каждый public statement обязан одновременно ссылаться на accepted evidence ID,
verified `SourceFact` ID и точную adjudication identity. `observed` statement без
собственного решения, uncovered bridge, unbound interpretation, invented
significance или неизвестное число/entity/date отклоняются. Existing CP1/CP2
cardinality не ослабляется: если честного материала для неё недостаточно,
Normalizer возвращает honest outcome вместо выдуманного факта.

Meaning preservation is a separate code-owned gate. На fast path anchors
остаются fact-qualified и выводятся из четырнадцати точных правил. На generic
path anchors выводятся только из verified evidence: core entities/objects,
actions, state changes, explicit sequence/relation/cause, numbers, dates,
checks/results, uncertainty и source-supported significance. Generic фраза не
может покрыть два разных facts, а фраза вроде «всё аккуратно проверили» не
заменяет исходные objects/actions. Local negation, relation operand direction и
coordinated extra events invalidируют claim. Public ending хранит либо
`source_supported_significance` с полным evidence/fact binding, либо честный
`significance_not_supported`; непустой текст сам по себе ничего не доказывает.

Четырнадцать closed rules остаются быстрыми и более дешёвыми, но перестают быть
единственным admission vocabulary. Flow задаётся так:

```text
source
  -> exact deterministic grammar, если она подходит
  -> иначе generic segmentation
  -> evidence extraction
  -> independent evidence adjudication
  -> code-owned evidence validation
  -> verified facts
  -> bounded story generation/adjudication
  -> draft или honest non-draft outcome
```

Unknown predicate/object нельзя принять по нескольким совпавшим словам, но его
нельзя автоматически объявить insufficient. Если extractor/adjudicator не
создают полный проверяемый bundle, путь завершается `source_insufficient`,
`manual_attention`, `sensitive_rejected`, `failed` или `uncertain` согласно
наблюдаемому состоянию.

### Generic semantic boundary

The generic path is deliberately extractive at this checkpoint. Every accepted
evidence proposition must equal one exact source quote, and every generic public
claim must equal one complete accepted proposition. A token subset, reordered
free paraphrase, cross-language paraphrase, merged multi-fact sentence, or text
that merely carries stored anchor identifiers fails closed. This is what lets
application code prove predicate, object, qualifier, polarity, number, date and
relation preservation without pretending that arbitrary semantic equivalence is
decidable from token overlap.

Consequently, open-domain means that previously unseen textual containers and
source predicates can be segmented, extracted, adjudicated and classified
without adding a Python grammar. It does not mean unrestricted generative
paraphrase. A useful source whose proposed story cannot stay inside the exact
extractive boundary is reported as `manual_attention`; it is never silently
promoted to `draft_ready_for_review`. Supporting freer paraphrase later requires
a stronger versioned semantic proof contract, not a relaxation of this gate.

`story.md` содержит только читаемую историю. В нём нет путей, prompts, ответов
модели, кодов внутренних причин и технического отчёта.

`story.json` связывает историю с полным упорядоченным набором фактов и содержит
детерминированную проверку простого языка. `review.json` хранит только безопасный
результат проверки. `draft-manifest.json` связывает версии правил, запрос модели
и identity повторного запуска.

Черновик не является готовым материалом для Reels. До явного approval файл
`narrative_ready.json` отсутствует.

## Honest outcomes и batch accounting

Каждый queue item должен завершаться ровно одним закрытым outcome:

- `draft_ready_for_review` — evidence complete, fully adjudicated и прошло все
  code-owned factuality, meaning и plain-language gates;
- `source_insufficient` — подтверждённого материала недостаточно для честного
  CP1/CP2 input;
- `manual_attention` — полезного evidence достаточно, но relation или несколько
  допустимых interpretations требуют человека; этот же fail-closed operational
  state используется до начала model calls, если отсутствует generic evidence
  adapter, trust key или source содержит unreadable/unsupported companion data;
- `sensitive_rejected` — public-safe evidence невозможно построить без раскрытия
  защищённых данных;
- `existing_draft` — exact valid draft уже существует;
- `processing` — identity уже принадлежит активной bounded attempt;
- `failed` — завершённая безопасно классифицированная ошибка;
- `uncertain` — процесс мог завершиться после внешнего side effect, поэтому
  автоматический retry запрещён.

Batch report содержит total и счётчик каждого outcome. Их сумма обязана точно
равняться total; unclassified и silently skipped элементы запрещены. Ошибка
одного item не останавливает остальные. Такой accounting не означает, что все
элементы должны стать публикуемыми историями.

## Простой язык

Проверка рассчитана на умного читателя 12–14 лет. Она отклоняет необъяснённые
технические слова, внутренние имена функций, абсолютные пути, коды причин,
слишком много сокращений, длинные предложения и текст, похожий на отчёт тестов.

История должна ответить: что произошло, почему это важно и что изменилось. Она
может быть спокойным наблюдением, рассказом о предмете, объяснением продукта,
признанием ошибки или открытым вопросом. Конфликт, мораль и одинаковая
драматургия не обязательны.

## Keyed trust seal

Draft trust boundary использует HMAC-SHA256, а не unkeyed writer-owned digest.
Secret передаётся dependency injection через
`NARRATIVE_NORMALIZER_TRUST_KEY` либо явный CLI key-file. Key никогда не
сохраняется в draft/outbox, не попадает в diagnostics и не передаётся модели.

Seal покрывает source identity, source/evidence contract versions, полный
evidence bundle и adjudication identity, CP1/CP2 package, ordered public claims,
story files, code-owned receipts и draft manifest. Координированное изменение
нескольких файлов без key поэтому не создаёт доверенный draft. Approval без key
или с несовпадающим seal fail closed.

Production deploy обязан настроить key отдельно; этот checkpoint не изменяет
`.env` и не задаёт production secret. Tests должны получать ephemeral external
key, не зафиксированный в repository fixtures или logs.

### Authoritative monotonic review state

The old replaceable
`.normalizer-state/review-ledger/<source-identity>.json` file is not an approval
authority. Review authority is configured explicitly with
`NARRATIVE_NORMALIZER_REVIEW_AUTHORITY_ROOT` or `--review-authority-root` and
must be outside the outbox, raw inbox, quarantine registry root, and Git
checkout. Its authoritative layout is:

```text
<review-authority-root>/
  .locks/<source-identity>.lock
  <source-identity>/
    events/
      00000001-<event-digest>.json
      00000002-<event-digest>.json
      00000003-<event-digest>.json
    head.json
```

Every event object is canonical UTF-8 JSON, HMAC-SHA256 authenticated, written
through an exclusive sibling staging object, flushed/fsynced, strictly reread,
and promoted with no-clobber semantics under the per-source OS lock. Event file
names bind revision and event digest. There is no public update, replace,
truncate, or delete operation for an event. `head.json` is only a mutable
diagnostic cache: authoritative reads ignore it and scan every event object.

The scan requires exactly one valid event for every contiguous revision
starting at 1. It verifies source and draft identity, HMAC, filename digest,
previous revision/digest, and legal state transition. A gap, fork, duplicate
revision, copied event, unknown object, invalid signature, or broken previous
digest fails closed. A cached revision 2 never hides a valid revision 3.

Draft creation appends `drafted` revision 1 and its deterministic initial
`passed`/`rejected` decision at revision 2. Every operator transition binds the
exact previous revision/event digest, source identity, draft identity, operator
request ID, safe reasons, timestamp, policy version and action digest. Two
events prepared from one revision cannot both win. An exact operator action is
idempotent; reuse of its request ID with different semantics is a conflict.
`approved` is terminal in this checkpoint.

`review.json` is immutable creation-time review evidence, never a second mutable
owner. `reject`, `supersede` and `approve` consult and advance only the external
append-only chain. Restoring every historically valid signed draft file, an old
`head.json`, or old mutable-ledger bytes cannot restore approval rights while a
newer `rejected` or `superseded` event remains in the authority chain.

Successful approval adds `approval-attestation.json` beside the unchanged
production `narrative_ready.json`. The HMAC attestation binds source/draft/package,
the exact ready-manifest bytes, story/manifest/review/completed-claim digests,
artifact binding, the current approved ledger revision/event/request, contract
versions and key ID. Quarantine requires both files for the identity layout and
independently scans the authority chain, verifies the HMAC, and requires the
attestation revision/event digest to equal its latest `approved` event. A
missing/wrong key or authority root,
ready without attestation, attestation without ready, stale event, or any
coherently recomputed open SHA digest remains `needs_narrative`. The quarantine
consumer classifies the code-owned discovery location before checking any package
digest. Identity-layout V2 uses SHA-256 of the exact persisted `story.json` bytes;
legacy source-side and unambiguous digest-only manifests use the frozen historical
tree-envelope digest. There is no cross-layout retry or fallback. Coexisting
legacy/V2 artifacts and a manifest placed in the wrong layout fail closed.

Threat model: this local design blocks rollback by replacing the draft bundle,
legacy ledger, or mutable head cache while newer immutable events remain. It
does not detect a malicious same-privilege process that can delete event files,
delete/truncate the authority root, rewrite every authoritative object, or use
the HMAC key. HMAC proves authenticity, not the continued existence of a deleted
highest revision. No root-compromise or same-privilege deletion resistance is
claimed.

A future production deployment should use a separately permissioned root such
as `/var/lib/naz-ai-bot/narrative-review-authority`: outside the outbox, Git
checkout, raw inbox and quarantine registry; no symlinks; restrictive owner and
mode; no delete/replace right over historical events for the draft/outbox
writer; read-only access for the quarantine consumer; append capability only
for the review/approval component. This checkpoint neither creates that path
nor changes production `.env`.

### Guarded CP2 seam

Generic evidence extraction/adjudication is entirely Normalizer-owned and does
not call a private CP2 API. The deterministic story path still needs the exact
final typed adjudication object that CP2 currently does not expose in its public
result. That single seam is isolated in versioned adapter
`normalizer-cp2-final-parse-capture-v1`. Construction requires the exact stock
CP2 service, exact `narrative-generation-contract-v1`, callable client and exact
private `_call_parse(self, request, parser, calls, repair_used)` signature; any
drift fails closed before generation. It records only CP2's final already-parsed
typed generation/adjudication objects in a context-local session—never prompts
or raw responses. This remains documented maintainability debt, not an API
claimed to be public, and CP1/CP2 bytes are not modified to hide it.

## Approval

Approval model-free и требует участия человека. Команда `approve`:

1. повторно читает source unit;
2. заново вычисляет source digest и source identity;
3. повторяет segmentation и проверяет каждый evidence ref, offset и exact quote;
4. проверяет полную evidence adjudication cardinality и HMAC trust seal;
5. реконструирует verified facts и без model calls повторяет CP1, factuality,
   meaning-preservation и plain-language gates;
6. строго проверяет draft files, receipts, review и package digests;
7. проверяет latest authoritative event `passed` и готовит approved CAS event;
8. собирает canonical attestation и ready payload в памяти;
9. отдельно stage/fsync/strict-validate оба payload;
10. no-clobber продвигает attestation первым, ready manifest последним;
11. удаляет staging names, fsync и проверяет staged pair через production consumer
    относительно prospective approved event;
12. append-only no-clobber добавляет approved event как linearization point;
13. повторно сканирует authority chain и проверяет pair, claim и draft. До event
    CAS обычный consumer не считает пару ready.

Raw inbox при этом не меняется. Current quarantine locator поддерживает старый
manifest внутри source unit и новый immutable-outbox layout, но два manifest для
одного источника считаются неоднозначностью и отклоняются.

Повторный identical approval идемпотентен. Divergent approval останавливается.
`reject` и `supersede` не удаляют черновик.

## Очередь, resume и параллельность

Поддерживаются `normalize --all`, `--limit`, `--source-ref`, `--dry-run` и
ограниченное число workers. По умолчанию используется один worker.

На каждую `source-identity` действует отдельный межпроцессный lock и durable claim.
Поэтому два процесса не создают две bounded model attempts и два черновика. После
неожиданной смерти свежий claim не повторяется автоматически. Просроченный claim
становится `uncertain`; повтор возможен только с явным `--retry-uncertain`.

Готовый валидный draft пропускается без нового model call. Изменившийся source
получает новый digest и новый каталог. Ошибка одного материала не останавливает
остальную очередь.

Architecture не содержит лимита на пять материалов. Первый production rollout
всё равно должен быть staged: сначала пять representative live drafts,
обязательная human quality review, затем отдельное решение о продолжении
остальной очереди. Это quality gate, а не автоматическое approval и не право на
publication, Director, Renderer или Reels.

## Sealed local transactions

- `draft-manifest.json` and `review.json` are immutable after draft creation;
  the external append-only signed event chain is the only current state owner.
- Reject requires an operator request ID and expected draft identity. An exact
  duplicate is byte-idempotent; reuse with a different payload is a conflict.
- Supersede binds both old and new refs, digests, source identities, and draft
  identities in one monotonic CAS event.
- Approval stages and validates the signed attestation and ready manifest,
  promotes attestation first and ready last, validates the pair, and commits the
  approved append-only event last. Before that CAS, the pair is consumer-ineligible.
- `normalize --all --dry-run` does not load an adapter, create outbox/state
  directories, acquire locks, write claims/registry, or call a model.

## Dry-run: structural ledger, не final outcomes

Adapter-free dry-run выполняет только read-only discovery и structural
classification. Его ledger может показать deterministic fast-path candidates,
generic-fallback candidates, structurally insufficient, sensitive,
unsupported/container и parse-error counts. Без evidence extraction и
adjudication он не имеет права заявлять итоговые `draft_ready_for_review`,
`source_insufficient` или `manual_attention`: это provisional structural
распределение, а не model-backed final outcome.

Dry-run обязан иметь точный total и не пропускать найденные identities. При
этом model calls, outbox/state directories, locks, claims, registry writes и
source mutations равны нулю. Privacy-safe report содержит только агрегаты и
opaque identities; raw bodies, filenames, paths, exact private quotes, prompts,
credentials и model payloads запрещены.

### `coverage-snapshot` и ограничение очереди из 99 элементов

Production baseline описывает 99 `needs_narrative` records, но в локальном
checkpoint нет registry snapshot, который выбирает эти exact identities.
Read-only local discovery обнаружил 158 source units. Поэтому этот результат не
является аудитом exact production-очереди из 99 элементов; брать произвольные
«первые 99» запрещено. Для точного all-99 structural audit нужен privacy-safe
read-only registry snapshot или эквивалентный explicit selector. До его
появления документация и CLI не должны утверждать `total outcomes=99`.

`coverage-snapshot` требует явные `--registry`, `--inbox-root` и
`--narrative-outbox`. Команда не загружает adapters/keys, не вызывает models и
ничего не создаёт. Она выводит только total, structural categories, fast/generic
counts, insufficient/manual/sensitive counts, file/segment ranges, contract
versions и один aggregate snapshot digest. Source refs, имена файлов, paths,
quotes, bodies и credentials в output отсутствуют. До separately authorized
read-only запуска against the real 99-record production registry итоговый gate:

`PRODUCTION_COVERAGE_REQUIRED`

## CLI

CLI всегда требует явные пути к temp/local inbox, registry и outbox:

```powershell
python tools/run_narrative_normalizer.py `
  --inbox-root <local-inbox> `
  --registry-path <local-registry.json> `
  --outbox-root <local-outbox> `
  scan
```

Executable `normalize`/`resume`, `pass`, `approve`, `reject`, `supersede`, and
`verify` require an explicit `--review-authority-socket` with exact socket owner
UID/GID, mode, and timeout configuration. The local review-authority root adapter
remains available only through an internal unit-test seam and is not selected by
the production CLI. Read-only
`scan`, `coverage-snapshot`, `list`, `show`, `status`, and every dry-run neither
read nor create the authority root. A missing authority on a mutating command
fails before adapter/model loading, locks, or filesystem writes.

Команды: `scan`, `coverage-snapshot`, `normalize`, `resume`, `list`, `show`,
`pass`, `approve`, `reject`, `supersede`, `status`, `verify`.

In Broker mode, normalization registers the fully persisted and revalidated
draft with `register_draft`. Human review state and approval are owned only by
the Broker; transport, protocol, role, stale-state, or request-conflict failures
never fall back to the local ledger. Approval binds the logical
`draft_package_digest` independently from the SHA-256 of the exact persisted
`story.json` bytes (including its final LF). The attestation and
`narrative_ready.json` are promoted as a no-clobber pair before Broker commit,
and quarantine subsequently requires both latest `approved` and
`verify_ready=true`.

Privacy-safe batch output различает `evidence_fast_path`,
`evidence_generic_path`, `source_insufficient`, `manual_attention` и
`sensitive_rejected`, а terminal report использует полный закрытый outcome set.
CLI не выводит source body, evidence quotes, absolute paths, prompts/responses
или trust key.

`show` intentionally returns only the public story fields plus opaque identities
and review counters. On the safe generic extractive path those public sentences
may equal selected public-safe source quotes; non-public segments, evidence span
coordinates, source paths, raw model payloads and trust material are never shown.

Исполняющие `normalize` и `resume` дополнительно требуют `--enable-local-execution` и явный
dependency injection adapter в форме `module:factory`. Adapter-free исключение — только
`normalize --all --dry-run` (или такой же dry-run с ограничением/одним source): он читает
source и registry, но не загружает adapter, не создаёт outbox и ничего не записывает.
Import модуля сам ничего не запускает.
`resume` повторяет только durable `failed`/`uncertain` attempts и сохраняет identity
неопределённой попытки; обычный `normalize` не делает скрытых retry.
Scheduler, Telegram command, auto-approval и auto-Reels в CP6B отсутствуют.

## Versioned outbox permissions

The versioned `private-v1` policy remains the default and preserves the existing
`0700` directory / `0600` file behavior. A shared Normalizer/Reviewer/Broker
deployment must opt in explicitly:

```text
--outbox-permission-policy shared-review-v1 \
--outbox-shared-group naz-narrative-content
```

`shared-review-v1` is POSIX-only and resolves the named group before any live
write. The outbox root is `02750`; every promoted draft directory is `03770`;
`story.md`, `story.json`, `draft-manifest.json`, and `review.json` are `0640`
and remain owned by the Normalizer UID. Reviewer-created
`approval-attestation.json` and `narrative_ready.json` are `0640`, owned by the
Reviewer UID, and use the same configured GID. Internal claims are group
readable and coordination locks are group writable, without granting the
Reviewer access to the trust key or Review Authority storage. World
permission bits are always zero.

The sticky bit on a final draft directory allows a group Reviewer to create the
approval pair but prevents it from deleting or replacing Normalizer-owned draft
files. Staging directories/files remain private (`0700`/`0600`) until the exact
validated promotion boundary. Every owner, group, and mode is applied and read
back before Broker registration or approval commit. A missing group, a
non-POSIX host, a symlink, or any ownership/mode mismatch fails closed; there is
no fallback to private or ambient ACL behavior. Help, coverage, and dry-run
commands never create outbox state.

## Test isolation

Каждая test group запускается отдельным процессом с новым внешним `DB_PATH`, внешним
`PYTHONPYCACHEPREFIX`, запретом записи bytecode и отключённым pytest cache provider.
Generic-path tests дополнительно используют fake provider, network tripwire и
новый ephemeral HMAC key во внешнем temp root:

```powershell
$env:DB_PATH = '<external-temp>\runtime.sqlite3'
$env:PYTHONPYCACHEPREFIX = '<external-temp>\pycache'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pytest -p no:cacheprovider tests/test_narrative_normalizer.py -q
```

До и после полного gate сравниваются SHA-256, размер и mtime всех scoped source/test
файлов, рабочей SQLite и её WAL/SHM, а также содержимое и metadata уже существующих
`.pytest_cache` и `__pycache__`. Временная DB и pycache удаляются только из заранее
проверенного внешнего temp-каталога; существующие ignored-файлы worktree не удаляются.
Отдельно проверяются source tree, registry, outbox/state/lock/claim roots и
staging leftovers. Cleanup разрешён только внутри exact validated external temp
root. Этот раздел задаёт обязательный gate и не утверждает, что ещё не
запущенные suites уже прошли.

## Права и приватность

В production outbox должен находиться вне Git checkout и raw inbox, например
`/var/lib/naz-ai-bot/narrative-outbox`, с root-only доступом. Записи выполняются
через sibling staging, flush/fsync, строгую проверку и атомарное продвижение.

Диагностика содержит только стабильные reason codes, opaque source ID, digest и
счётчики. Она не содержит raw prompts, ответов модели, credentials, абсолютных
путей или exception messages.

Evidence model получает только bounded source segments и необходимый immutable
context. Registry history, filesystem layout, unrelated state, trust key и
другие sources ему не передаются. Evidence adjudicator и story adjudicator
являются отдельными bounded calls; output одного источника нельзя использовать
как evidence другого source identity.

Normalizer не гарантирует, что любая техническая запись является хорошей
публичной историей. При недостаточных фактах или непонятном тексте он отказывает,
а не выдумывает.
