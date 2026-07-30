import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main
import operator_events


PROJECT = "Naz_AI_Bot_clean"
DATE = "2026-07-11"
TOPIC_ID = "abcdef123456"
SOURCE_HASH = "1" * 64
SECOND_TOPIC_ID = "fedcba654321"
SECOND_SOURCE_HASH = "5" * 64
SESSION_REF = "session-" + "2" * 24
MESSAGE_REF = "message-" + "3" * 24
PLAN_ID = "a" * 24
SIDECAR_ONLY_MARKER = "SIDECAR_ONLY_MARKER"


def fact(value=None, refs=None):
    return {
        "value": value,
        "source_message_refs": list(refs if refs is not None else ([MESSAGE_REF] if value else [])),
    }


def event_set(
    *,
    topic_id=TOPIC_ID,
    source_hash=SOURCE_HASH,
    project=PROJECT,
    date_text=DATE,
    summary=SIDECAR_ONLY_MARKER,
    contract_version=operator_events.OPERATOR_EVENT_CONTRACT,
    publication_copy_ref=None,
    ready=False,
):
    if ready:
        actual_cause = fact("A visible configuration conflict was confirmed.")
        evidence = [fact("The same local check passed after one bounded change.")]
        technical_result = fact("The bounded local check completed successfully.")
        status = "ready"
        reasons = []
    else:
        actual_cause = fact()
        evidence = []
        technical_result = fact()
        status = "needs_review"
        reasons = [
            "actual_cause_unconfirmed",
            "evidence_unconfirmed",
            "technical_result_unconfirmed",
        ]
    return {
        "contract_version": contract_version,
        "project": project,
        "date": date_text,
        "topic_id": topic_id,
        "source_hash": source_hash,
        "events": [
            {
                "event_id": operator_events.expected_event_id(
                    project, date_text, topic_id, source_hash
                ),
                "event_type": "work_event",
                "occurred_at": "2026-07-11T12:00:00+03:00",
                "source_session_refs": [SESSION_REF],
                "source_message_refs": [MESSAGE_REF],
                "event_facts": {
                    "event_summary": fact(summary),
                    "initial_state": fact(),
                    "trigger": fact(),
                    "initial_assumption": fact(),
                    "actual_cause": actual_cause,
                    "change": fact(),
                    "evidence": evidence,
                    "technical_result": technical_result,
                },
                "operator_commentary": {
                    "human_consequence": None,
                    "lesson": None,
                    "open_questions": [],
                },
                "publication_copy_ref": publication_copy_ref,
                "privacy_status": "clear",
                "content_status": status,
                "reason_codes": reasons,
            }
        ],
    }


class OperatorEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.markdown_root = self.root / "content_inbox" / "agent_content"
        self.story_day = self.markdown_root / PROJECT / DATE
        self.source_root = self.root / "content_inbox" / "operator_events"
        self.private_root = self.root / "private" / "operator-event-bindings"
        self.story_day.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_story(self, topic_id=TOPIC_ID, source_hash=SOURCE_HASH, name="story.md"):
        path = self.story_day / name
        path.write_text(
            "\n".join(
                (
                    "# Safe story fixture",
                    f"Тема-ID: t-{topic_id}",
                    f"Источник-хеш: sha256:{source_hash}",
                    "This Markdown remains the only editorial source for the text draft.",
                )
            ),
            encoding="utf-8",
        )
        return path

    def write_sidecar(self, payload, *, filename=None):
        day = self.source_root / PROJECT / DATE
        day.mkdir(parents=True, exist_ok=True)
        topic_id = payload.get("topic_id", TOPIC_ID)
        path = day / (filename or f"t-{topic_id}.json")
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return path

    def bind_batch(self, *, mode="shadow", plan_id=PLAN_ID):
        return operator_events.bind_plan_to_operator_events(
            mode=mode,
            source_root=self.source_root,
            private_root=self.private_root,
            markdown_root=self.markdown_root,
            project=PROJECT,
            date_text=DATE,
            plan_id=plan_id,
            story_dirs=(self.story_day,),
        )

    def bind(self, *, mode="shadow", plan_id=PLAN_ID):
        batch = self.bind_batch(mode=mode, plan_id=plan_id)
        self.assertEqual(batch.discovered_count, 1)
        return batch.outcomes[0]

    def test_off_mode_has_zero_runtime_effects_even_with_overlapping_roots(self):
        absent = self.root / "absent"
        result = operator_events.bind_plan_to_operator_events(
            mode="off",
            source_root=absent,
            private_root=absent,
            markdown_root=absent,
            project=PROJECT,
            date_text=DATE,
            plan_id=PLAN_ID,
            story_dirs=(self.root / "never-read",),
        )
        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.discovered_count, 0)
        self.assertFalse(absent.exists())

    def test_valid_shadow_binding_is_idempotent_and_preserves_null_unknowns(self):
        self.write_story()
        self.write_sidecar(event_set())

        first = self.bind()
        before = first.record_path.read_bytes()
        second = self.bind()

        self.assertEqual(first.status, "bound")
        self.assertEqual(first.event_id, event_set()["events"][0]["event_id"])
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.event_id, first.event_id)
        self.assertEqual(first.record_path.read_bytes(), before)
        self.assertEqual(len(list(self.private_root.rglob("*.json"))), 1)
        record = json.loads(first.record_path.read_text(encoding="utf-8"))
        self.assertEqual(
            record["contract_version"], operator_events.OPERATOR_EVENT_BINDING_CONTRACT
        )
        self.assertEqual(
            record["binding_scope"], operator_events.OPERATOR_EVENT_BINDING_SCOPE
        )
        self.assertEqual(first.record_path.parent.name, PLAN_ID)
        self.assertEqual(first.record_path.stem, first.event_id)
        stored = record["operator_event"]
        self.assertIsNone(stored["event_facts"]["actual_cause"]["value"])
        self.assertEqual(stored["event_facts"]["evidence"], [])
        self.assertIsNone(stored["event_facts"]["technical_result"]["value"])
        self.assertEqual(record["content_status"], "needs_review")
        self.assertIsNone(stored["publication_copy_ref"])

    def test_transient_missing_sidecar_can_upgrade_one_rejected_record(self):
        self.write_story()
        missing = self.bind()
        self.assertEqual(missing.status, "rejected")
        self.write_sidecar(event_set())

        bound = self.bind()

        self.assertEqual(bound.status, "bound")
        self.assertFalse(bound.created)
        self.assertEqual(len(list(self.private_root.rglob("*.json"))), 1)

    def test_persistent_unlocked_lock_file_does_not_poison_later_sync(self):
        self.write_story()
        self.write_sidecar(event_set())
        event_id = event_set()["events"][0]["event_id"]
        plan_dir = self.private_root / PLAN_ID
        plan_dir.mkdir(parents=True)
        (plan_dir / f".{event_id}.json.lock").write_bytes(b"\0")

        result = self.bind()

        self.assertEqual(result.status, "bound")
        self.assertTrue(result.created)

    def test_source_hash_mismatch_rejects_binding(self):
        self.write_story()
        self.write_sidecar(event_set(source_hash="4" * 64))
        result = self.bind()
        self.assertEqual(result.status, "rejected")
        self.assertIn("operator_event_source_hash_mismatch", result.reason_codes)
        self.assertEqual(
            result.event_id,
            operator_events.expected_event_id(PROJECT, DATE, TOPIC_ID, SOURCE_HASH),
        )

    def test_topic_id_mismatch_rejects_binding(self):
        self.write_story()
        self.write_sidecar(event_set(topic_id="fedcba654321"))
        result = self.bind()
        self.assertEqual(result.status, "rejected")
        self.assertIn("operator_event_sidecar_missing", result.reason_codes)
        self.assertIn("operator_event_unmatched_sidecar", self.bind_batch().reason_codes)

    def test_filename_topic_id_mismatch_rejects_binding(self):
        self.write_story()
        self.write_sidecar(event_set(), filename="t-fedcba654321.json")
        result = self.bind()
        self.assertEqual(result.status, "rejected")
        self.assertIn("operator_event_sidecar_missing", result.reason_codes)
        self.assertIn("operator_event_unmatched_sidecar", self.bind_batch().reason_codes)

    def test_two_valid_topics_bind_independently_under_one_plan(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())
        self.write_sidecar(event_set(
            topic_id=SECOND_TOPIC_ID, source_hash=SECOND_SOURCE_HASH,
            summary="A second privacy-safe event.",
        ))

        batch = self.bind_batch()

        self.assertEqual(batch.discovered_count, 2)
        self.assertEqual(batch.bound_count, 2)
        self.assertEqual(batch.already_bound_count, 0)
        self.assertEqual(batch.rejected_count, 0)
        self.assertEqual(batch.created_count, 2)
        self.assertEqual(len(batch.reason_codes_by_event), 2)
        self.assertEqual(len(list((self.private_root / PLAN_ID).glob("*.json"))), 2)

    def test_one_valid_and_one_source_mismatch_are_independent(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())
        self.write_sidecar(event_set(
            topic_id=SECOND_TOPIC_ID,
            source_hash="6" * 64,
            summary="A mismatched source event.",
        ))

        batch = self.bind_batch()
        outcomes = {item.event_id: item for item in batch.outcomes}
        first_id = operator_events.expected_event_id(PROJECT, DATE, TOPIC_ID, SOURCE_HASH)
        second_id = operator_events.expected_event_id(
            PROJECT, DATE, SECOND_TOPIC_ID, SECOND_SOURCE_HASH
        )

        self.assertEqual(outcomes[first_id].status, "bound")
        self.assertEqual(outcomes[second_id].status, "rejected")
        self.assertIn("operator_event_source_hash_mismatch", outcomes[second_id].reason_codes)
        rejected_record = json.loads(
            outcomes[second_id].record_path.read_text(encoding="utf-8")
        )
        self.assertIsNone(rejected_record["operator_event"])
        self.assertEqual((batch.bound_count, batch.rejected_count), (1, 1))

    def test_one_valid_and_one_topic_mismatch_are_independent(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())
        wrong_topic = "012345abcdef"
        mismatched = event_set(
            topic_id=wrong_topic,
            source_hash=SECOND_SOURCE_HASH,
            summary="A topic-mismatched event.",
        )
        self.write_sidecar(mismatched, filename=f"t-{SECOND_TOPIC_ID}.json")

        batch = self.bind_batch()

        self.assertEqual((batch.bound_count, batch.rejected_count), (1, 1))
        rejected = next(item for item in batch.outcomes if item.status == "rejected")
        self.assertIn("operator_event_topic_id_mismatch", rejected.reason_codes)

    def test_ambiguous_boundary_rejects_only_its_own_event(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())
        ambiguous = event_set(
            topic_id=SECOND_TOPIC_ID,
            source_hash=SECOND_SOURCE_HASH,
            summary="An event whose boundary requires review.",
        )
        ambiguous["events"][0]["reason_codes"].append("ambiguous_event_boundary")
        self.write_sidecar(ambiguous)

        batch = self.bind_batch()

        self.assertEqual((batch.bound_count, batch.rejected_count), (1, 1))
        rejected = next(item for item in batch.outcomes if item.status == "rejected")
        self.assertEqual(rejected.reason_codes, ("ambiguous_event_boundary",))

    def test_missing_sidecar_rejects_only_the_missing_event(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())

        batch = self.bind_batch()

        self.assertEqual((batch.bound_count, batch.rejected_count), (1, 1))
        rejected = next(item for item in batch.outcomes if item.status == "rejected")
        self.assertIn("operator_event_sidecar_missing", rejected.reason_codes)
        self.assertIsNotNone(rejected.event_id)

    def test_invalid_json_rejects_only_its_own_event(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())
        invalid = self.source_root / PROJECT / DATE / f"t-{SECOND_TOPIC_ID}.json"
        invalid.write_text("{not-json", encoding="utf-8")

        batch = self.bind_batch()

        self.assertEqual((batch.bound_count, batch.rejected_count), (1, 1))
        rejected = next(item for item in batch.outcomes if item.status == "rejected")
        self.assertIn("operator_event_sidecar_parse_failed", rejected.reason_codes)

    def test_repeated_batch_is_byte_stable_and_reports_already_bound(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())
        self.write_sidecar(event_set(
            topic_id=SECOND_TOPIC_ID,
            source_hash=SECOND_SOURCE_HASH,
            summary="A second idempotent event.",
        ))

        first = self.bind_batch()
        before = {item.event_id: item.record_path.read_bytes() for item in first.outcomes}
        second = self.bind_batch()

        self.assertEqual(second.bound_count, 0)
        self.assertEqual(second.already_bound_count, 2)
        self.assertEqual(second.created_count, 0)
        self.assertEqual(len(list((self.private_root / PLAN_ID).glob("*.json"))), 2)
        self.assertEqual(
            {item.event_id: item.record_path.read_bytes() for item in second.outcomes},
            before,
        )

    def test_rejected_to_bound_update_leaves_sibling_byte_stable(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())
        first = self.bind_batch()
        bound = next(item for item in first.outcomes if item.status == "bound")
        sibling_before = bound.record_path.read_bytes()
        self.write_sidecar(event_set(
            topic_id=SECOND_TOPIC_ID,
            source_hash=SECOND_SOURCE_HASH,
            summary="A recovered second event.",
        ))

        second = self.bind_batch()

        self.assertEqual(second.bound_count, 1)
        self.assertEqual(second.already_bound_count, 1)
        updated = next(item for item in second.outcomes if item.write_status == "updated")
        self.assertEqual(updated.status, "bound")
        self.assertEqual(bound.record_path.read_bytes(), sibling_before)

    def test_bound_to_missing_reports_current_rejection_without_mutating_siblings(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())
        second_sidecar = self.write_sidecar(event_set(
            topic_id=SECOND_TOPIC_ID,
            source_hash=SECOND_SOURCE_HASH,
            summary="A second accepted event.",
        ))
        first = self.bind_batch()
        before = {item.event_id: item.record_path.read_bytes() for item in first.outcomes}
        second_event_id = operator_events.expected_event_id(
            PROJECT, DATE, SECOND_TOPIC_ID, SECOND_SOURCE_HASH
        )
        second_sidecar.unlink()

        rerun = self.bind_batch()

        self.assertEqual(rerun.bound_count, 0)
        self.assertEqual(rerun.already_bound_count, 1)
        self.assertEqual(rerun.rejected_count, 1)
        rejected = next(item for item in rerun.outcomes if item.event_id == second_event_id)
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.write_status, "retained")
        self.assertIn("operator_event_sidecar_missing", rejected.reason_codes)
        self.assertIn(
            "operator_event_sidecar_missing",
            rerun.reason_codes_by_event[second_event_id],
        )
        self.assertEqual(
            {item.event_id: item.record_path.read_bytes() for item in rerun.outcomes},
            before,
        )

    def test_bound_to_corrupt_reports_current_rejection_and_keeps_accepted_bytes(self):
        self.write_story()
        sidecar = self.write_sidecar(event_set())
        first = self.bind()
        before = first.record_path.read_bytes()
        sidecar.write_text("{corrupt", encoding="utf-8")

        rerun = self.bind()

        self.assertEqual(rerun.status, "rejected")
        self.assertEqual(rerun.write_status, "retained")
        self.assertIn("operator_event_sidecar_parse_failed", rerun.reason_codes)
        self.assertEqual(rerun.record_path.read_bytes(), before)

    def test_tamper_conflicts_only_with_the_accepted_event_record(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        self.write_sidecar(event_set())
        second_path = self.write_sidecar(event_set(
            topic_id=SECOND_TOPIC_ID,
            source_hash=SECOND_SOURCE_HASH,
            summary="The original second event.",
        ))
        first = self.bind_batch()
        before = {item.event_id: item.record_path.read_bytes() for item in first.outcomes}
        second_path.write_text(
            json.dumps(event_set(
                topic_id=SECOND_TOPIC_ID,
                source_hash=SECOND_SOURCE_HASH,
                summary="A changed second event payload.",
            ), ensure_ascii=False),
            encoding="utf-8",
        )

        rerun = self.bind_batch()

        self.assertEqual((rerun.bound_count, rerun.already_bound_count), (0, 1))
        self.assertEqual(rerun.rejected_count, 1)
        conflict = next(item for item in rerun.outcomes if item.status == "rejected")
        self.assertEqual(
            conflict.reason_codes, ("operator_event_plan_binding_conflict",)
        )
        self.assertEqual(
            {item.event_id: item.record_path.read_bytes() for item in rerun.outcomes},
            before,
        )

    def test_duplicate_exact_story_metadata_is_rejected_for_that_event(self):
        self.write_story()
        self.write_story(TOPIC_ID, SOURCE_HASH, "duplicate.md")
        self.write_sidecar(event_set())

        batch = self.bind_batch()

        self.assertEqual(batch.discovered_count, 1)
        self.assertEqual(batch.rejected_count, 1)
        self.assertIn(
            "operator_event_story_metadata_duplicate",
            batch.outcomes[0].reason_codes,
        )
        record = json.loads(
            batch.outcomes[0].record_path.read_text(encoding="utf-8")
        )
        self.assertEqual(record["binding_status"], "rejected")
        self.assertIsNone(record["operator_event"])

    def test_same_topic_with_two_hashes_rejects_each_expected_pair(self):
        self.write_story()
        other_hash = "7" * 64
        self.write_story(TOPIC_ID, other_hash, "conflicting.md")
        self.write_sidecar(event_set())

        batch = self.bind_batch()

        self.assertEqual(batch.discovered_count, 2)
        self.assertEqual(batch.rejected_count, 2)
        self.assertEqual(len(batch.reason_codes_by_event), 2)
        for outcome in batch.outcomes:
            self.assertIn(
                "operator_event_story_metadata_ambiguous", outcome.reason_codes
            )

    def test_colliding_event_ids_reject_all_candidates_before_persistence(self):
        self.write_story()
        self.write_story(SECOND_TOPIC_ID, SECOND_SOURCE_HASH, "second.md")
        collision_id = "oev-" + "9" * 24
        with patch.object(
            operator_events, "expected_event_id", return_value=collision_id
        ):
            self.write_sidecar(event_set())
            self.write_sidecar(event_set(
                topic_id=SECOND_TOPIC_ID,
                source_hash=SECOND_SOURCE_HASH,
                summary="A colliding second candidate.",
            ))
            batch = self.bind_batch()

        self.assertEqual(batch.discovered_count, 2)
        self.assertEqual(batch.bound_count, 0)
        self.assertEqual(batch.rejected_count, 2)
        self.assertEqual(
            {outcome.reason_codes for outcome in batch.outcomes},
            {("operator_event_duplicate_binding_conflict",)},
        )
        self.assertFalse((self.private_root / PLAN_ID).exists())

    def test_extra_unmatched_sidecar_is_only_a_batch_diagnostic(self):
        self.write_story()
        self.write_sidecar(event_set())
        self.write_sidecar(event_set(
            topic_id=SECOND_TOPIC_ID,
            source_hash=SECOND_SOURCE_HASH,
            summary="An unmatched sidecar.",
        ))

        batch = self.bind_batch()

        self.assertEqual(batch.bound_count, 1)
        self.assertEqual(batch.rejected_count, 0)
        self.assertIn("operator_event_unmatched_sidecar", batch.reason_codes)

    def test_legacy_singleton_remains_untouched_beside_v2_records(self):
        self.write_story()
        self.write_sidecar(event_set())
        self.private_root.mkdir(parents=True)
        legacy = self.private_root / f"{PLAN_ID}.json"
        legacy_bytes = b'{"contract_version":"operator-event-binding.v1"}\n'
        legacy.write_bytes(legacy_bytes)

        batch = self.bind_batch()

        self.assertEqual(batch.bound_count, 1)
        self.assertIn("legacy_single_binding_present", batch.reason_codes)
        self.assertEqual(legacy.read_bytes(), legacy_bytes)
        self.assertEqual(len(list((self.private_root / PLAN_ID).glob("*.json"))), 1)

    def test_traversal_plan_id_is_rejected_before_private_storage(self):
        self.write_story()
        self.write_sidecar(event_set())

        with self.assertRaises(operator_events.OperatorEventValidationError) as raised:
            self.bind_batch(plan_id="../escape")

        self.assertEqual(
            raised.exception.reason_codes, ("operator_event_plan_id_invalid",)
        )
        self.assertFalse(self.private_root.exists())

    def test_traversal_event_id_is_rejected_at_sidecar_validation_and_stays_nested(self):
        self.write_story()
        payload = event_set()
        payload["events"][0]["event_id"] = "../../escaped"
        self.write_sidecar(payload)

        batch = self.bind_batch()

        self.assertEqual(batch.rejected_count, 1)
        outcome = batch.outcomes[0]
        self.assertIn("operator_event_id_invalid", outcome.reason_codes)
        expected_id = operator_events.expected_event_id(
            PROJECT, DATE, TOPIC_ID, SOURCE_HASH
        )
        self.assertEqual(outcome.event_id, expected_id)
        self.assertEqual(outcome.record_path.parent, self.private_root / PLAN_ID)
        self.assertEqual(outcome.record_path.name, f"{expected_id}.json")
        self.assertFalse((self.root / "escaped").exists())

    def test_atomic_replace_failure_removes_per_record_temporary_file(self):
        self.write_story()
        self.write_sidecar(event_set())

        with patch.object(operator_events.os, "replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.bind_batch()

        plan_dir = self.private_root / PLAN_ID
        self.assertEqual(list(plan_dir.glob("*.tmp")), [])
        self.assertEqual(list(plan_dir.glob("*.json")), [])

    def test_invalid_contract_is_fail_closed(self):
        self.write_story()
        self.write_sidecar(event_set(contract_version="operator-event-set.v0"))
        result = self.bind()
        self.assertEqual(result.status, "rejected")
        self.assertIn("operator_event_contract_version_invalid", result.reason_codes)

    def test_tampered_ready_status_without_proof_is_rejected(self):
        payload = event_set()
        event = payload["events"][0]
        event["content_status"] = "ready"
        event["reason_codes"] = []
        with self.assertRaises(operator_events.OperatorEventValidationError) as raised:
            operator_events.validate_operator_event_set(payload)
        self.assertIn("operator_event_proof_status_invalid", raised.exception.reason_codes)

    def test_unconfirmed_summary_remains_null_and_needs_review(self):
        payload = event_set(summary=None)
        payload["events"][0]["reason_codes"].append("event_summary_unconfirmed")
        validated = operator_events.validate_operator_event_set(payload)
        self.assertIsNone(validated.event["event_facts"]["event_summary"]["value"])
        self.assertEqual(validated.event["content_status"], "needs_review")

    def test_publication_copy_reference_is_rejected_not_promoted_to_facts(self):
        payload = event_set(publication_copy_ref={"artifact_id": "draft-1"})
        with self.assertRaises(operator_events.OperatorEventValidationError) as raised:
            operator_events.validate_operator_event_set(payload)
        self.assertIn(
            "operator_event_publication_copy_forbidden", raised.exception.reason_codes
        )

    def test_hostile_private_values_are_rejected_independently(self):
        private_values = (
            "owner@example.com",
            "server 192.168.1.20",
            "http://localhost:8080/private",
            "hf_" + "x" * 24,
            "/opt/naz/private.json",
        )
        for value in private_values:
            with self.subTest(value=value):
                payload = event_set(summary=value)
                with self.assertRaises(operator_events.OperatorEventValidationError):
                    operator_events.validate_operator_event_set(payload)

    def test_overlapping_markdown_and_event_roots_fail_closed(self):
        self.write_story()
        self.write_sidecar(event_set())
        with self.assertRaises(operator_events.OperatorEventValidationError) as raised:
            operator_events.bind_plan_to_operator_events(
                mode="shadow",
                source_root=self.markdown_root / "operator_events",
                private_root=self.private_root,
                markdown_root=self.markdown_root,
                project=PROJECT,
                date_text=DATE,
                plan_id=PLAN_ID,
                story_dirs=(self.story_day,),
            )
        self.assertEqual(raised.exception.reason_codes, ("operator_event_root_overlap",))

    def test_differing_event_cannot_replace_an_accepted_binding(self):
        self.write_story()
        path = self.write_sidecar(event_set())
        first = self.bind()
        before = first.record_path.read_bytes()
        changed = event_set(summary="A different but still safe event summary.")
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")

        second = self.bind()

        self.assertEqual(second.status, "rejected")
        self.assertEqual(
            second.reason_codes, ("operator_event_plan_binding_conflict",)
        )
        self.assertEqual(first.record_path.read_bytes(), before)

    def test_manual_mode_only_shadow_binds_with_explicit_reason(self):
        self.write_story()
        self.write_sidecar(event_set())
        result = self.bind(mode="manual")
        self.assertEqual(result.status, "bound")
        self.assertIn(
            "character_manual_phase_not_implemented", result.reason_codes
        )

    def _run_text_route(self, *, mode, payload, second_payload=None):
        self.write_story()
        self.write_sidecar(payload)
        if second_payload is not None:
            self.write_story(
                second_payload["topic_id"],
                second_payload["source_hash"],
                "second.md",
            )
            self.write_sidecar(second_payload)
        plan = SimpleNamespace(
            plan_id=PLAN_ID,
            slot="agent_content_sync",
            production_mode="standard",
            topic="Safe text draft",
            semantic_theme="work_chronicle",
            semantic_card="fixture",
            to_dict=lambda: {"plan_id": PLAN_ID, "source_ref": "fixture"},
        )
        package = SimpleNamespace(final_text="Existing text draft remains unchanged.")
        bot = SimpleNamespace(send_message=AsyncMock())
        safe_context = "A bounded editorial context used by the existing text pipeline."

        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "NAZ_CHARACTER_REELS_MODE", mode))
            stack.enter_context(patch.object(main, "NAZ_OPERATOR_EVENT_ROOT", self.source_root))
            stack.enter_context(patch.object(main, "NAZ_OPERATOR_EVENT_BINDING_ROOT", self.private_root))
            stack.enter_context(patch.object(main, "AGENT_CONTENT_INBOX", self.markdown_root))
            stack.enter_context(patch.object(main, "AGENT_CONTENT_PROJECT", PROJECT))
            stack.enter_context(patch.object(
                main, "agent_content_source_dirs_for_date", return_value=[self.story_day]
            ))
            stack.enter_context(patch.object(
                main, "agent_content_hash_for_date", return_value="aggregate-manifest-hash"
            ))
            stack.enter_context(patch.object(main, "load_agent_content_seen", return_value={}))
            stack.enter_context(patch.object(
                main, "collect_agent_materials", return_value=(safe_context, [], DATE)
            ))
            stack.enter_context(patch.object(
                main.naz_character, "apply_event",
                return_value=main.naz_character.CharacterState(),
            ))
            stack.enter_context(patch.object(
                main.memory, "load_character_state",
                return_value=main.naz_character.CharacterState(),
            ))
            stack.enter_context(patch.object(main, "scheduled_plan", return_value=plan))
            stack.enter_context(patch.object(main.memory, "update_editorial_release_event"))
            text_model = stack.enter_context(patch.object(
                main, "generate_scheduled_package", new=AsyncMock(return_value=package)
            ))
            stack.enter_context(patch.object(main.memory, "save_generated_post"))
            draft_notifier = stack.enter_context(patch.object(
                main, "notify_admin_generated", new=AsyncMock()
            ))
            failure_notifier = stack.enter_context(patch.object(
                main, "notify_admin", new=AsyncMock()
            ))
            channel_publisher = stack.enter_context(patch.object(
                main, "send_observed_scheduled_post", new=AsyncMock()
            ))
            stack.enter_context(patch.object(main, "mark_agent_content_seen"))
            director = stack.enter_context(patch.object(
                main, "generate_reels_director_treatment", new=AsyncMock()
            ))
            story_pack = stack.enter_context(patch.object(main, "queue_story_first_pack"))
            story_persist = stack.enter_context(patch.object(
                main.story_production, "persist_story_queue"
            ))
            image_provider = stack.enter_context(patch.object(
                main, "generate_images_with_retries", new=AsyncMock()
            ))
            legacy_provider = stack.enter_context(patch.object(
                main, "generate_image_bytes", new=AsyncMock()
            ))
            result = asyncio.run(
                main.process_agent_content_date(
                    bot, 42, DATE, force=True, publish=False
                )
            )

        self.assertIn("orchestrated draft imported", result)
        text_model.assert_awaited_once()
        self.assertNotIn(
            SIDECAR_ONLY_MARKER,
            text_model.await_args.kwargs["source_material"],
        )
        director.assert_not_awaited()
        story_pack.assert_not_called()
        story_persist.assert_not_called()
        image_provider.assert_not_awaited()
        legacy_provider.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        draft_notifier.assert_awaited_once()
        failure_notifier.assert_not_awaited()
        channel_publisher.assert_not_awaited()
        return result

    def test_shadow_route_adds_no_director_story_pack_or_media_calls(self):
        self._run_text_route(
            mode="shadow",
            payload=event_set(),
            second_payload=event_set(
                topic_id=SECOND_TOPIC_ID,
                source_hash=SECOND_SOURCE_HASH,
                summary="A separate shadow-only event.",
            ),
        )

    def test_off_route_never_invokes_binding_or_creates_private_storage(self):
        with patch.object(
            main.operator_events,
            "bind_plan_to_operator_events",
            side_effect=AssertionError("off mode must not call OperatorEvent binding"),
        ) as binding:
            self._run_text_route(mode="off", payload=event_set())
        binding.assert_not_called()
        self.assertFalse(self.private_root.exists())

    def test_invalid_sidecar_does_not_break_existing_text_draft(self):
        self._run_text_route(
            mode="shadow",
            payload=event_set(contract_version="operator-event-set.v0"),
        )

    def test_manual_route_remains_non_producing(self):
        self._run_text_route(mode="manual", payload=event_set())
        record_path = next((self.private_root / PLAN_ID).glob("*.json"))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertIn(
            "character_manual_phase_not_implemented", record["reason_codes"]
        )


if __name__ == "__main__":
    unittest.main()
