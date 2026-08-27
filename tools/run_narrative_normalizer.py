"""Explicit local CLI for the review-only Narrative Normalizer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import narrative_normalizer as normalizer
import narrative_normalizer_run_profiles as run_profiles
import narrative_normalizer_trust as trust
import narrative_outbox_permissions as outbox_permissions
import narrative_review_authority_client as authority_client
import reels_failure_quarantine as quarantine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_narrative_normalizer")
    parser.add_argument("--inbox-root", required=True)
    parser.add_argument("--registry-path", "--registry", dest="registry_path", required=True)
    parser.add_argument("--outbox-root", "--narrative-outbox", dest="outbox_root", required=True)
    parser.add_argument(
        "--outbox-permission-policy",
        choices=tuple(sorted(outbox_permissions.POLICY_VERSIONS)),
        default=outbox_permissions.PRIVATE_POLICY_VERSION,
    )
    parser.add_argument("--outbox-shared-group")
    parser.add_argument("--trust-key-file")
    parser.add_argument("--review-authority-root")
    parser.add_argument("--review-authority-socket")
    parser.add_argument("--review-authority-owner-uid", type=int)
    parser.add_argument("--review-authority-owner-gid", type=int)
    parser.add_argument(
        "--review-authority-socket-mode",
        type=lambda value: int(value, 8),
        default=0o660,
    )
    parser.add_argument("--review-authority-timeout", type=float, default=10.0)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan")
    sub.add_parser("coverage-snapshot")
    for command in ("normalize", "resume"):
        execution = sub.add_parser(command)
        target = execution.add_mutually_exclusive_group(required=True)
        target.add_argument("--all", action="store_true")
        target.add_argument("--source-ref")
        target.add_argument("--source-identity", dest="source_identities", action="append")
        execution.add_argument("--limit", type=int)
        execution.add_argument("--workers", type=int, default=1)
        execution.add_argument("--dry-run", action="store_true")
        execution.add_argument("--retry-uncertain", action="store_true")
        execution.add_argument("--retry-failed", action="store_true")
        execution.add_argument("--enable-local-execution", action="store_true")
        execution.add_argument("--enable-live-provider", action="store_true")
        execution.add_argument("--adapter")
        execution.add_argument(
            "--live-run-profile",
            choices=run_profiles.LIVE_RUN_PROFILES,
        )
        execution.add_argument("--manual-retry-request-id")
        execution.add_argument("--expected-failed-attempt-id")
        execution.add_argument("--expected-failed-claim-digest")

    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("source_ref")
    show.add_argument("source_digest")
    approve = sub.add_parser("approve")
    approve.add_argument("source_ref")
    approve.add_argument("source_digest")
    approve.add_argument("expected_draft_identity")
    approve.add_argument("--operator-request-id")
    reject = sub.add_parser("reject")
    reject.add_argument("source_ref")
    reject.add_argument("source_digest")
    reject.add_argument("expected_draft_identity")
    reject.add_argument("operator_request_id")
    reject.add_argument("--reason-code", action="append", required=True)
    passed = sub.add_parser("pass")
    passed.add_argument("source_ref")
    passed.add_argument("source_digest")
    passed.add_argument("expected_draft_identity")
    passed.add_argument("operator_request_id")
    supersede = sub.add_parser("supersede")
    for name in (
        "old_source_ref", "old_source_digest", "old_source_identity", "old_draft_identity",
        "new_source_ref", "new_source_digest", "new_source_identity", "new_draft_identity",
        "operator_request_id",
    ):
        supersede.add_argument(name)
    sub.add_parser("status")
    sub.add_parser("verify")
    return parser


def _policy(args: argparse.Namespace) -> quarantine.QuarantinePathPolicy:
    authority_value = args.review_authority_root
    if authority_value is None:
        authority_value = os.environ.get("NARRATIVE_NORMALIZER_REVIEW_AUTHORITY_ROOT")
    authority_root = (
        None
        if authority_value is None or not authority_value.strip()
        else Path(authority_value)
    )
    return quarantine.QuarantinePathPolicy(
        Path(args.inbox_root),
        Path(args.registry_path),
        Path(args.outbox_root),
        narrative_review_authority_root=authority_root,
    )


def _require_review_authority(
    policy: quarantine.QuarantinePathPolicy,
    broker: normalizer.ReviewAuthorityTransport | None = None,
    *,
    allow_local_test_adapter: bool = False,
) -> None:
    if broker is None and not (
        allow_local_test_adapter
        and policy.narrative_review_authority_root is not None
    ):
        raise normalizer.NarrativeNormalizerError(
            "narrative_normalizer_review_authority_unavailable"
        )


def _load_broker_client(
    args: argparse.Namespace,
) -> normalizer.ReviewAuthorityTransport | None:
    socket_path = args.review_authority_socket
    if socket_path is None:
        return None
    try:
        if (
            args.review_authority_owner_uid is None
            or args.review_authority_owner_gid is None
            or args.review_authority_owner_uid < 0
            or args.review_authority_owner_gid < 0
        ):
            raise ValueError
        return authority_client.ReviewAuthorityClient(
            socket_path,
            owner_uid=args.review_authority_owner_uid,
            owner_gid=args.review_authority_owner_gid,
            mode=args.review_authority_socket_mode,
            timeout=args.review_authority_timeout,
        )
    except (TypeError, ValueError, authority_client.ClientError):
        raise normalizer.NarrativeNormalizerError(
            "narrative_normalizer_review_authority_unavailable"
        ) from None


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _load_trust_service(args: argparse.Namespace) -> trust.NarrativeTrustService:
    key_file = Path(args.trust_key_file) if args.trust_key_file is not None else None
    try:
        return trust.load_trust_service(os.environ, key_file)
    except trust.TrustError as error:
        reason = (
            "narrative_normalizer_trust_unavailable"
            if error.reason_code == trust.TRUST_KEY_MISSING
            else "narrative_normalizer_trust_invalid"
        )
        raise normalizer.NarrativeNormalizerError(reason) from None


def _safe_draft_rows(store: normalizer.NarrativeOutboxStore) -> tuple[dict[str, object], ...]:
    return tuple({
        "source_identity": item["source_identity"],
        "source_digest": item["source_digest"],
        "draft_identity": item["draft_identity"],
        "review_status": item["review_status"],
        "approved": item["approved"],
    } for item in store.list_drafts())


def _safe_show(value: object) -> dict[str, object]:
    if type(value) is not dict or type(value.get("story")) is not dict or type(value.get("review")) is not dict:
        raise normalizer.NarrativeNormalizerError("narrative_normalizer_draft_invalid")
    story = value["story"]
    review = value["review"]
    assert type(story) is dict and type(review) is dict
    public_fields = (
        "title", "hook", "story", "ending", "primary_character",
        "secondary_character", "presence_mode",
    )
    public_story = {name: story[name] for name in public_fields if name in story}
    return {
        "source_identity": value.get("source_identity"),
        "source_digest": value.get("source_digest"),
        "draft_identity": value.get("draft_identity"),
        "public_story": public_story,
        "review_status": value.get("authoritative_review_state", "unverified"),
        "unsupported_claim_count": review.get("unsupported_claim_count"),
        "evidence_mode": story.get("evidence_mode"),
        "approved": value.get("approved"),
    }


def _status(store: normalizer.NarrativeOutboxStore) -> dict[str, object]:
    drafts = store.list_drafts()
    review_counts: dict[str, int] = {}
    approved = 0
    for item in drafts:
        review = str(item["review_status"])
        review_counts[review] = review_counts.get(review, 0) + 1
        approved += bool(item["approved"])
    claim_counts: dict[str, int] = {}
    for path in sorted(store._claims.glob("*.json")):
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("invalid claim entry")
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw_state = payload.get("state")
            state = raw_state if raw_state in normalizer.CLAIM_STATES else "invalid"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            state = "invalid"
        claim_counts[state] = claim_counts.get(state, 0) + 1
    return {
        "draft_count": len(drafts),
        "approved_count": approved,
        "review_counts": {key: review_counts[key] for key in sorted(review_counts)},
        "claim_counts": {key: claim_counts[key] for key in sorted(claim_counts)},
    }


def _coverage_snapshot(
    policy: quarantine.QuarantinePathPolicy,
    rows: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    categories: dict[str, int] = {}
    identities: list[str] = []
    file_counts: list[int] = []
    segment_counts: list[int] = []
    fast = 0
    generic = 0
    for source_ref, source_digest in rows:
        documents = normalizer.read_source_documents(
            policy,
            source_ref,
            expected_digest=source_digest,
        )
        coverage = normalizer.evidence.classify_source_bundle(documents)
        if coverage.classification not in {
            "insufficient", "sensitive", "parse_error", "unsupported_binary_container",
        }:
            source = normalizer.read_source_unit(
                policy,
                source_ref,
                expected_digest=source_digest,
                allow_insufficient=True,
            )
            coverage = normalizer.evidence.classify_source_bundle(
                documents,
                deterministic_fast_path=(
                    len(source.facts) >= normalizer.MIN_SOURCE_FACTS
                    and normalizer._source_semantically_closed(source)
                ),
            )
        categories[coverage.classification] = categories.get(coverage.classification, 0) + 1
        fast += coverage.classification == "known_deterministic_grammar"
        generic += coverage.generic_fallback_candidate
        identities.append(normalizer.source_identity(source_ref, source_digest))
        file_counts.append(len(documents.ordered_documents))
        segment_counts.append(sum(len(item.ordered_segments) for item in documents.ordered_documents))
    aggregate = hashlib.sha256(
        json.dumps(sorted(identities), separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return {
        "schema_version": "normalizer-coverage-snapshot-v1",
        "total_record_count": len(rows),
        "aggregate_snapshot_digest": aggregate,
        "structural_categories": {key: categories[key] for key in sorted(categories)},
        "fast_candidate_count": fast,
        "generic_candidate_count": generic,
        "insufficient_count": categories.get("insufficient", 0),
        "manual_attention_count": (
            categories.get("parse_error", 0)
            + categories.get("unsupported_binary_container", 0)
        ),
        "sensitive_count": categories.get("sensitive", 0),
        "file_count_range": [min(file_counts, default=0), max(file_counts, default=0)],
        "segment_count_range": [min(segment_counts, default=0), max(segment_counts, default=0)],
        "contract_versions": {
            "coverage": "normalizer-coverage-snapshot-v1",
            "source": normalizer.SOURCE_CONTRACT_VERSION,
            "evidence": normalizer.evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION,
            "normalizer": normalizer.NORMALIZATION_POLICY_VERSION,
        },
    }


def run(
    argv: list[str] | None = None,
    *,
    _allow_local_review_authority_for_tests: bool = False,
) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = _policy(args)
        permission_policy = outbox_permissions.resolve_permission_policy(
            args.outbox_permission_policy,
            args.outbox_shared_group,
        )
        command = args.command
        if command == "scan":
            rows = normalizer.scan_needs_narrative(policy)
            _emit({
                "needs_narrative_count": len(rows),
                "items": [
                    {
                        "source_id": hashlib.sha256(ref.encode("utf-8")).hexdigest()[:12],
                        "source_digest": digest,
                    }
                    for ref, digest in rows
                ],
            })
            return 0
        if command == "coverage-snapshot":
            rows = normalizer.scan_needs_narrative(policy)
            _emit(_coverage_snapshot(policy, rows))
            return 0

        store: normalizer.NarrativeOutboxStore | None = None
        trust_service: trust.NarrativeTrustService | None = None
        broker_client = _load_broker_client(args)
        if broker_client is not None:
            policy = replace(policy, review_authority_client=broker_client)
        if command not in {"normalize", "resume"}:
            if command in {"approve", "pass", "reject", "supersede", "verify"}:
                _require_review_authority(
                    policy,
                    broker_client,
                    allow_local_test_adapter=_allow_local_review_authority_for_tests,
                )
                trust_service = _load_trust_service(args)
                policy = replace(policy, narrative_trust_service=trust_service)
                store = normalizer.NarrativeOutboxStore(
                    policy,
                    trust_service=trust_service,
                    review_authority=broker_client,
                    permission_policy=permission_policy,
                )
            else:
                store = normalizer.NarrativeOutboxStore(
                    policy,
                    permission_policy=permission_policy,
                )
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if command == "list":
            assert store is not None
            _emit({"drafts": _safe_draft_rows(store)})
            return 0
        if command == "show":
            assert store is not None
            _emit(_safe_show(store.show(args.source_ref, args.source_digest)))
            return 0
        if command == "approve":
            assert store is not None
            _emit(asdict(store.approve(
                args.source_ref,
                args.source_digest,
                expected_draft_identity=args.expected_draft_identity,
                reviewed_at=now,
                operator_request_id=args.operator_request_id,
            )))
            return 0
        if command == "reject":
            assert store is not None
            result = store.reject(
                args.source_ref,
                args.source_digest,
                expected_draft_identity=args.expected_draft_identity,
                operator_request_id=args.operator_request_id,
                reason_codes=args.reason_code,
                reviewed_at=now,
            )
            _emit(asdict(result))
            return 0
        if command == "pass":
            assert store is not None
            result = store.pass_review(
                args.source_ref,
                args.source_digest,
                expected_draft_identity=args.expected_draft_identity,
                operator_request_id=args.operator_request_id,
                reviewed_at=now,
            )
            _emit(asdict(result))
            return 0
        if command == "supersede":
            assert store is not None
            result = store.supersede(
                old_source_ref=args.old_source_ref,
                old_source_digest=args.old_source_digest,
                old_source_identity=args.old_source_identity,
                old_draft_identity=args.old_draft_identity,
                new_source_ref=args.new_source_ref,
                new_source_digest=args.new_source_digest,
                new_source_identity=args.new_source_identity,
                new_draft_identity=args.new_draft_identity,
                operator_request_id=args.operator_request_id,
                reviewed_at=now,
            )
            _emit(asdict(result))
            return 0
        if command == "status":
            assert store is not None
            _emit(_status(store))
            return 0
        if command == "verify":
            assert store is not None
            assert trust_service is not None
            rows = store.list_drafts()
            verified_drafts: dict[str, dict[str, object]] = {}
            for item in rows:
                value = normalizer.validate_draft_directory(
                    store.root / str(item["source_identity"]),
                    expected_identity=str(item["source_identity"]),
                    validate_ready=broker_client is None,
                    trust_service=trust_service,
                    review_authority_root=policy.narrative_review_authority_root,
                    require_trust=True,
                )
                story = value["story"]
                claim = store.read_claim(
                    str(story["source_ref"]),
                    str(story["source_digest"]),
                )
                if claim is None or not store._claim_matches_draft(claim, value):
                    raise normalizer.NarrativeNormalizerError(
                        "narrative_normalizer_claim_uncertain"
                    )
                verified_drafts[str(item["source_identity"])] = value
                ready_path = (
                    store.root
                    / str(item["source_identity"])
                    / "narrative_ready.json"
                )
                if item["approved"] or (
                    broker_client is not None and ready_path.is_file()
                ):
                    quarantine.validate_narrative_ready_manifest(
                        policy,
                        str(value["story"]["source_ref"]),
                        trust_service=trust_service,
                    )
            for claim in store.list_claims():
                if claim["state"] != normalizer.CLAIM_COMPLETED:
                    continue
                value = verified_drafts.get(str(claim["source_identity"]))
                if value is None or not store._claim_matches_draft(claim, value):
                    raise normalizer.NarrativeNormalizerError(
                        "narrative_normalizer_claim_uncertain"
                    )
            _emit({"verified": len(rows), "passed": True})
            return 0
        if command in {"normalize", "resume"}:
            rows = normalizer.scan_needs_narrative(policy)
            if args.source_ref:
                rows = tuple(item for item in rows if item[0] == args.source_ref)
                if not rows:
                    raise normalizer.NarrativeNormalizerError("narrative_normalizer_source_invalid")
            elif args.source_identities:
                by_identity = {
                    normalizer.source_identity(ref, digest): (ref, digest)
                    for ref, digest in rows
                }
                if any(identity not in by_identity for identity in args.source_identities):
                    raise normalizer.NarrativeNormalizerError("narrative_normalizer_source_invalid")
                rows = tuple(by_identity[identity] for identity in args.source_identities)
            if args.limit is not None:
                if args.limit < 1:
                    raise normalizer.NarrativeNormalizerError("narrative_normalizer_cli_invalid")
                rows = rows[:args.limit]
            if args.dry_run:
                outcomes = []
                coverage_counts: dict[str, int] = {}
                for ref, digest in rows:
                    documents = normalizer.read_source_documents(
                        policy,
                        ref,
                        expected_digest=digest,
                    )
                    coverage = normalizer.evidence.classify_source_bundle(documents)
                    if coverage.classification not in {
                        "insufficient",
                        "sensitive",
                        "parse_error",
                        "unsupported_binary_container",
                    }:
                        source = normalizer.read_source_unit(
                            policy,
                            ref,
                            expected_digest=digest,
                            allow_insufficient=True,
                        )
                        fast_candidate = (
                            len(source.facts) >= normalizer.MIN_SOURCE_FACTS
                            and normalizer._source_semantically_closed(source)
                        )
                        coverage = normalizer.evidence.classify_source_bundle(
                            documents,
                            deterministic_fast_path=fast_candidate,
                        )
                    coverage_counts[coverage.classification] = (
                        coverage_counts.get(coverage.classification, 0) + 1
                    )
                    evidence_path = (
                        "deterministic_fast_path"
                        if coverage.classification == "known_deterministic_grammar"
                        else "generic"
                        if coverage.generic_fallback_candidate
                        else None
                    )
                    outcomes.append(normalizer.NormalizationOutcome(
                        hashlib.sha256(ref.encode("utf-8")).hexdigest()[:12],
                        documents.source_digest,
                        normalizer.OUTCOME_DRY_RUN,
                        (),
                        0,
                        None,
                        None,
                        evidence_path,
                    ))
                summary = normalizer.BatchResult(len(rows), tuple(outcomes)).safe_summary()
                summary.update({
                    "coverage_counts": {
                        key: coverage_counts[key]
                        for key in sorted(coverage_counts)
                    },
                    "known_rule_count": coverage_counts.get(
                        "known_deterministic_grammar", 0
                    ),
                    "generic_fallback_candidate_count": sum(
                        coverage_counts.get(name, 0)
                        for name in (
                            "unknown_but_text_readable",
                            "json_like",
                            "log_like",
                            "markdown_like",
                            "chat_email_like",
                        )
                    ),
                    "truly_insufficient_count": coverage_counts.get(
                        "insufficient", 0
                    ),
                    "manual_attention_count": (
                        coverage_counts.get("parse_error", 0)
                        + coverage_counts.get("unsupported_binary_container", 0)
                    ),
                    "sensitive_count": coverage_counts.get("sensitive", 0),
                })
                _emit(summary)
                return 0
            if not args.adapter:
                raise normalizer.NarrativeNormalizerError("narrative_normalizer_cli_invalid")
            retry_values = (
                args.manual_retry_request_id,
                args.expected_failed_attempt_id,
                args.expected_failed_claim_digest,
            )
            if args.retry_failed or any(value is not None for value in retry_values) != all(
                value is not None for value in retry_values
            ):
                raise normalizer.NarrativeNormalizerError(
                    "narrative_normalizer_manual_retry_invalid"
                )
            production_adapter_spec = (
                "narrative_normalizer_provider:production_adapter_factory"
            )
            live_summary: dict[str, object] | None = None
            dependencies: object
            if args.adapter == production_adapter_spec:
                if args.live_run_profile is None:
                    raise normalizer.NarrativeNormalizerError(
                        "narrative_normalizer_cli_invalid"
                    )
                _require_review_authority(
                    policy,
                    broker_client,
                    allow_local_test_adapter=_allow_local_review_authority_for_tests,
                )
                if (
                    args.enable_live_provider
                    and broker_client is None
                    and not _allow_local_review_authority_for_tests
                ):
                    raise normalizer.NarrativeNormalizerError(
                        "narrative_normalizer_review_authority_unavailable"
                    )
                trust_service = _load_trust_service(args)
                policy = replace(policy, narrative_trust_service=trust_service)
                try:
                    import narrative_normalizer_provider as production_provider

                    requested_identities = tuple(args.source_identities or ())
                    resolved_identities = tuple(
                        normalizer.source_identity(ref, digest) for ref, digest in rows
                    )
                    if args.limit is not None or requested_identities != resolved_identities:
                        raise production_provider.NormalizerProviderError(
                            production_provider.PROVIDER_CONFIGURATION_INVALID
                        )
                    authorization = production_provider.authorize_live_provider_run(
                        adapter_spec=args.adapter,
                        local_execution_enabled=args.enable_local_execution,
                        live_provider_enabled=args.enable_live_provider,
                        run_profile=args.live_run_profile,
                        source_identities=requested_identities,
                        env=os.environ,
                        trust_service=trust_service,
                        review_authority_root=policy.narrative_review_authority_root,
                    )
                    live_summary = authorization.safe_summary()
                    _emit({"live_provider_preflight": live_summary})
                    dependencies = production_provider.production_adapter_factory(
                        authorization
                    )
                    live_summary = None
                except (KeyboardInterrupt, SystemExit, GeneratorExit):
                    raise
                except Exception:
                    raise normalizer.NarrativeNormalizerError("narrative_normalizer_cli_invalid")
            else:
                if (
                    args.enable_live_provider
                    or not args.enable_local_execution
                    or args.live_run_profile is not None
                    or any(value is not None for value in retry_values)
                ):
                    raise normalizer.NarrativeNormalizerError("narrative_normalizer_cli_invalid")
                _require_review_authority(
                    policy,
                    broker_client,
                    allow_local_test_adapter=_allow_local_review_authority_for_tests,
                )
                trust_service = _load_trust_service(args)
                policy = replace(policy, narrative_trust_service=trust_service)
                dependencies = normalizer.load_adapter(args.adapter)
            if type(dependencies) is not tuple or len(dependencies) not in {2, 3}:
                raise normalizer.NarrativeNormalizerError("narrative_normalizer_cli_invalid")
            if len(dependencies) == 2:
                provider, generation_service = dependencies
                evidence_service = None
            else:
                provider, generation_service, evidence_service = dependencies
            service = normalizer.NarrativeNormalizerService(
                policy=policy,
                context_provider=provider,
                generation_service=generation_service,
                evidence_service=evidence_service,
                trust_service=trust_service,
                review_authority=broker_client,
                permission_policy=permission_policy,
            )
            manual_retry = None
            if all(value is not None for value in retry_values):
                if (
                    args.live_run_profile != run_profiles.CANARY_RUN_PROFILE
                    or len(rows) != 1
                    or len(tuple(args.source_identities or ())) != 1
                ):
                    raise normalizer.NarrativeNormalizerError(
                        "narrative_normalizer_manual_retry_invalid"
                    )
                ref, digest = rows[0]
                try:
                    manual_retry = normalizer.ManualRetryRequest(
                        normalizer.source_identity(ref, digest),
                        digest,
                        args.expected_failed_attempt_id,
                        args.expected_failed_claim_digest,
                        args.manual_retry_request_id,
                        args.live_run_profile,
                    )
                except TypeError:
                    raise normalizer.NarrativeNormalizerError(
                        "narrative_normalizer_manual_retry_invalid"
                    ) from None
            result = service.normalize_batch(
                rows,
                limit=None,
                max_workers=args.workers,
                dry_run=args.dry_run,
                retry_uncertain=args.retry_uncertain or command == "resume",
                retry_failed=False,
                manual_retry=manual_retry,
            )
            summary = result.safe_summary()
            _emit(summary)
            return 3 if summary["status_counts"].get(normalizer.OUTCOME_FAILED, 0) else 0
        raise normalizer.NarrativeNormalizerError("narrative_normalizer_cli_invalid")
    except normalizer.NarrativeNormalizerError as error:
        _emit(normalizer.safe_error(error))
        return 2
    except trust.TrustError:
        _emit(normalizer.safe_error(
            normalizer.NarrativeNormalizerError("narrative_normalizer_trust_invalid")
        ))
        return 2
    except (
        quarantine.QuarantineError,
        outbox_permissions.OutboxPermissionError,
        TypeError,
        ValueError,
        OSError,
    ):
        _emit(normalizer.safe_error(normalizer.NarrativeNormalizerError("narrative_normalizer_cli_invalid")))
        return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
