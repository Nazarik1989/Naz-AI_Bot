import re
import unittest
from unittest.mock import patch

from story_audio_provider import (
    ABSOLUTE_MAX_RESPONSE_BYTES,
    AUDIO_RESULTS_PATH,
    DEFAULT_MAX_RESPONSE_BYTES,
    STABLE_AUDIO_MODEL,
    TEXT_TO_AUDIO_PATH,
    AudioGenerationRequest,
    AudioProviderError,
    AudioTransportError,
    HttpResponse,
    StableAudioProvider,
    UrllibTransport,
    provider_from_environment,
)


JOB_ID = "a" * 64
OTHER_JOB_ID = "b" * 64
MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"audio-fixture"
WAV_BYTES = b"RIFF" + (20).to_bytes(4, "little") + b"WAVE" + b"audio-fixture"


def pending(job_id=JOB_ID):
    return HttpResponse(
        202,
        {"Content-Type": "application/json"},
        ('{"id":"%s","status":"in-progress"}' % job_id).encode("ascii"),
    )


def completed(body=MP3_BYTES, content_type="audio/mpeg"):
    return HttpResponse(
        200,
        {
            "Content-Type": content_type,
            "finish-reason": "SUCCESS",
            "seed": "343940597",
            "x-request-id": "request-fixture",
        },
        body,
    )


class FakeTransport:
    """Deterministic transport: any unplanned request fails the test."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(
        self,
        method,
        url,
        *,
        headers,
        body,
        timeout,
        max_response_bytes,
    ):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": dict(headers),
            "body": body,
            "timeout": timeout,
            "max_response_bytes": max_response_bytes,
        })
        if not self.responses:
            raise AssertionError("unplanned network request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def provider(transport, **changes):
    return StableAudioProvider(
        api_key="secret-stability-key",
        transport=transport,
        **changes,
    )


def request(**changes):
    values = {
        "prompt": "Instrumental dark melodic house, 124 BPM, no vocals.",
        "duration_seconds": 60,
    }
    values.update(changes)
    return AudioGenerationRequest(**values)


def multipart_fields(call):
    content_type = call["headers"]["Content-Type"]
    match = re.fullmatch(r"multipart/form-data; boundary=([A-Za-z0-9-]+)", content_type)
    if match is None:
        raise AssertionError(f"invalid multipart content type: {content_type!r}")
    boundary = match.group(1)
    marker = ("--" + boundary).encode("ascii")
    fields = {}
    for part in call["body"].split(marker):
        part = part.lstrip(b"\r\n")
        if not part or part.startswith(b"--"):
            continue
        header, value = part.split(b"\r\n\r\n", 1)
        name_match = re.search(br'Content-Disposition: form-data; name="([A-Za-z0-9_-]+)"', header)
        if name_match is None:
            raise AssertionError("missing multipart field name")
        if value.endswith(b"\r\n"):
            value = value[:-2]
        fields[name_match.group(1).decode("ascii")] = value.decode("utf-8")
    return boundary, fields


class SubmissionTests(unittest.TestCase):
    def test_exact_endpoint_model_and_safe_multipart(self):
        transport = FakeTransport([
            HttpResponse(202, {"Content-Type": "application/json"}, ('{"id":"%s"}' % JOB_ID).encode()),
        ])
        job = provider(transport).submit(request())

        self.assertEqual((job.external_job_id, job.status, job.artifact), (JOB_ID, "submitted", None))
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.stability.ai" + TEXT_TO_AUDIO_PATH)
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-stability-key")
        self.assertEqual(call["headers"]["Accept"], "audio/*")
        self.assertEqual(call["headers"]["User-Agent"], "NazAudioLibrary/1.0")
        boundary, fields = multipart_fields(call)
        self.assertTrue(boundary.startswith("----NazStableAudio"))
        self.assertEqual(fields, {
            "prompt": "Instrumental dark melodic house, 124 BPM, no vocals.",
            "model": STABLE_AUDIO_MODEL,
            "duration": "60",
            "seed": "0",
            "steps": "8",
            "cfg_scale": "1",
            "output_format": "mp3",
        })
        self.assertNotIn(b"secret-stability-key", call["body"])
        self.assertTrue(call["body"].endswith(("--" + boundary + "--\r\n").encode("ascii")))

    def test_boundary_changes_between_requests(self):
        transport = FakeTransport([
            HttpResponse(202, {}, ('{"id":"%s"}' % JOB_ID).encode()),
            HttpResponse(202, {}, ('{"id":"%s"}' % OTHER_JOB_ID).encode()),
        ])
        client = provider(transport)
        client.submit(request())
        client.submit(request(seed=1))
        first, _ = multipart_fields(transport.calls[0])
        second, _ = multipart_fields(transport.calls[1])
        self.assertNotEqual(first, second)

    def test_boundary_collision_with_prompt_is_regenerated(self):
        collision = "----NazStableAudiocollision"
        transport = FakeTransport([
            HttpResponse(202, {}, ('{"id":"%s"}' % JOB_ID).encode()),
        ])
        with patch("story_audio_provider.secrets.token_hex", side_effect=["collision", "safe"]):
            provider(transport).submit(request(prompt=f"sound containing {collision}"))
        boundary, fields = multipart_fields(transport.calls[0])
        self.assertEqual(boundary, "----NazStableAudiosafe")
        self.assertIn(collision, fields["prompt"])

    def test_invalid_requests_are_rejected_before_transport(self):
        invalid = [
            request(prompt="   "),
            request(prompt="x" * 10_001),
            request(model="stable-audio-3.0"),
            request(duration_seconds=0),
            request(duration_seconds=381),
            request(duration_seconds=float("nan")),
            request(output_format="ogg"),
            request(seed=-1),
            request(seed=4_294_967_295),
            request(steps=3),
            request(steps=9),
            request(cfg_scale=0),
            request(cfg_scale=26),
        ]
        transport = FakeTransport()
        client = provider(transport)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(AudioProviderError):
                client.submit(item)
        self.assertEqual(transport.calls, [])

    def test_http_error_is_redacted_non_retryable_and_never_reposted(self):
        secret_prompt = "private prompt that must not enter an error"
        secret_key = "private-key-in-response"
        response_body = ('{"errors":["%s %s"]}' % (secret_prompt, secret_key)).encode()
        transport = FakeTransport([HttpResponse(500, {}, response_body)])
        client = provider(transport)

        with self.assertRaises(AudioProviderError) as raised:
            client.submit(request(prompt=secret_prompt))
        error = raised.exception
        self.assertEqual(error.code, "audio_provider_unavailable")
        self.assertEqual(error.status_code, 500)
        self.assertFalse(error.retryable)
        self.assertNotIn(secret_prompt, str(error))
        self.assertNotIn(secret_key, str(error))
        self.assertEqual([call["method"] for call in transport.calls], ["POST"])

    def test_transport_failure_has_unknown_outcome_and_no_retry(self):
        transport = FakeTransport([AudioTransportError()])
        with self.assertRaises(AudioProviderError) as raised:
            provider(transport).submit(request())
        self.assertEqual(raised.exception.code, "audio_submit_outcome_unknown")
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(transport.calls), 1)

    def test_malformed_202_response_does_not_cause_second_post(self):
        transport = FakeTransport([HttpResponse(202, {}, b'{"id":"private-malformed"}')])
        with self.assertRaisesRegex(AudioProviderError, "audio_submit_response_invalid"):
            provider(transport).submit(request())
        self.assertEqual([call["method"] for call in transport.calls], ["POST"])

    def test_only_202_is_accepted_for_submission(self):
        transport = FakeTransport([HttpResponse(200, {}, ('{"id":"%s"}' % JOB_ID).encode())])
        with self.assertRaises(AudioProviderError) as raised:
            provider(transport).submit(request())
        self.assertEqual(raised.exception.status_code, 200)
        self.assertEqual(len(transport.calls), 1)


class PollTests(unittest.TestCase):
    def test_pending_poll_uses_canonical_get_only(self):
        transport = FakeTransport([pending()])
        job = provider(transport).poll(JOB_ID)
        self.assertEqual((job.external_job_id, job.status, job.artifact), (JOB_ID, "in_progress", None))
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://api.stability.ai" + AUDIO_RESULTS_PATH + "/" + JOB_ID)
        self.assertIsNone(call["body"])
        self.assertNotIn("Content-Type", call["headers"])
        self.assertEqual(call["headers"]["User-Agent"], "NazAudioLibrary/1.0")

    def test_completed_mp3_is_validated_and_returned(self):
        transport = FakeTransport([completed()])
        job = provider(transport).poll(JOB_ID)
        self.assertEqual(job.status, "completed")
        self.assertIsNotNone(job.artifact)
        self.assertEqual(job.artifact.data, MP3_BYTES)
        self.assertEqual(job.artifact.content_type, "audio/mpeg")
        self.assertEqual(job.artifact.output_format, "mp3")
        self.assertEqual(job.artifact.seed, 343940597)
        self.assertEqual(job.artifact.finish_reason, "SUCCESS")
        self.assertEqual(job.artifact.request_id, "request-fixture")
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_completed_wav_is_validated_and_returned(self):
        job = provider(FakeTransport([completed(WAV_BYTES, "audio/wav; charset=binary")])).poll(JOB_ID)
        self.assertEqual(job.artifact.output_format, "wav")
        self.assertEqual(job.artifact.data, WAV_BYTES)

    def test_mp3_frame_sync_without_id3_is_accepted(self):
        frame = b"\xff\xfb\x90\x64" + b"frame-data"
        job = provider(FakeTransport([completed(frame)])).poll(JOB_ID)
        self.assertEqual(job.artifact.output_format, "mp3")

    def test_content_type_and_file_signature_must_agree(self):
        cases = [
            (completed(b"<html>not audio</html>", "text/html"), "audio_result_content_type_invalid"),
            (completed(WAV_BYTES, "audio/mpeg"), "audio_result_file_invalid"),
            (completed(MP3_BYTES, "audio/wav"), "audio_result_file_invalid"),
            (completed(b"ID3", "audio/mpeg"), "audio_result_file_invalid"),
        ]
        for response, code in cases:
            with self.subTest(code=code), self.assertRaises(AudioProviderError) as raised:
                provider(FakeTransport([response])).poll(JOB_ID)
            self.assertEqual(raised.exception.code, code)

    def test_finish_reason_and_seed_are_strictly_validated(self):
        bad_finish = completed()
        bad_finish = HttpResponse(200, {**bad_finish.headers, "finish-reason": "CONTENT_FILTERED"}, MP3_BYTES)
        bad_seed = completed()
        bad_seed = HttpResponse(200, {**bad_seed.headers, "seed": "not-a-number"}, MP3_BYTES)
        for response, code in [
            (bad_finish, "audio_result_finish_reason_invalid"),
            (bad_seed, "audio_result_seed_invalid"),
        ]:
            with self.subTest(code=code), self.assertRaises(AudioProviderError) as raised:
                provider(FakeTransport([response])).poll(JOB_ID)
            self.assertEqual(raised.exception.code, code)

    def test_body_and_declared_response_sizes_are_bounded(self):
        oversized_body = completed(MP3_BYTES + b"x" * 20)
        declared = completed(MP3_BYTES)
        declared = HttpResponse(200, {**declared.headers, "Content-Length": "1000"}, MP3_BYTES)
        for response in [oversized_body, declared]:
            with self.subTest(headers=response.headers), self.assertRaises(AudioProviderError) as raised:
                provider(FakeTransport([response]), max_response_bytes=len(MP3_BYTES)).poll(JOB_ID)
            self.assertEqual(raised.exception.code, "audio_response_too_large")

    def test_poll_transport_error_is_retryable_without_post(self):
        transport = FakeTransport([AudioTransportError()])
        with self.assertRaises(AudioProviderError) as raised:
            provider(transport).poll(JOB_ID)
        self.assertEqual(raised.exception.code, "audio_poll_transport_error")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_poll_http_retryability_is_limited_to_transient_statuses(self):
        for status, retryable in [(404, False), (429, True), (500, True)]:
            with self.subTest(status=status), self.assertRaises(AudioProviderError) as raised:
                provider(FakeTransport([HttpResponse(status, {}, b"private-error-body")])).poll(JOB_ID)
            self.assertEqual(raised.exception.status_code, status)
            self.assertEqual(raised.exception.retryable, retryable)
            self.assertNotIn("private-error-body", str(raised.exception))

    def test_invalid_job_id_never_reaches_transport(self):
        transport = FakeTransport()
        with self.assertRaisesRegex(AudioProviderError, "audio_job_id_invalid"):
            provider(transport).poll("../private")
        self.assertEqual(transport.calls, [])

    def test_schema_valid_unusual_job_id_is_confined_to_one_url_segment(self):
        unusual_id = "/" * 64
        transport = FakeTransport([pending(unusual_id)])
        job = provider(transport).poll(unusual_id)
        self.assertEqual(job.external_job_id, unusual_id)
        self.assertTrue(transport.calls[0]["url"].endswith("/" + "%2F" * 64))

    def test_pending_response_must_match_requested_job(self):
        transport = FakeTransport([pending(OTHER_JOB_ID)])
        with self.assertRaises(AudioProviderError) as raised:
            provider(transport).poll(JOB_ID)
        self.assertEqual(raised.exception.code, "audio_poll_response_invalid")
        self.assertTrue(raised.exception.retryable)


class TransportAndConfigurationTests(unittest.TestCase):
    def test_urllib_transport_reads_at_most_limit_plus_one_offline(self):
        class FakeUrlResponse:
            status = 200
            headers = {"Content-Type": "audio/mpeg"}

            def __init__(self):
                self.read_sizes = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                self.read_sizes.append(size)
                return b"x" * size

        response = FakeUrlResponse()
        with patch("story_audio_provider.urllib.request.urlopen", return_value=response):
            with self.assertRaises(AudioTransportError) as raised:
                UrllibTransport().request(
                    "GET",
                    "https://api.stability.ai/test",
                    headers={},
                    body=None,
                    timeout=10,
                    max_response_bytes=12,
                )
        self.assertEqual(raised.exception.code, "audio_response_too_large")
        self.assertEqual(response.read_sizes, [13])

    def test_urllib_transport_rejects_declared_oversize_without_reading(self):
        class FakeUrlResponse:
            status = 200
            headers = {"Content-Length": "13"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                raise AssertionError("oversized response must not be read")

        with patch("story_audio_provider.urllib.request.urlopen", return_value=FakeUrlResponse()):
            with self.assertRaisesRegex(AudioTransportError, "audio_response_too_large"):
                UrllibTransport().request(
                    "GET",
                    "https://api.stability.ai/test",
                    headers={},
                    body=None,
                    timeout=10,
                    max_response_bytes=12,
                )

    def test_environment_factory_is_offline_and_exact(self):
        result = provider_from_environment({
            "NAZ_AUDIO_PROVIDER": "stability",
            "NAZ_AUDIO_API_KEY": "dedicated-audio-key",
            "NAZ_AUDIO_MODEL": "stable-audio-3",
        })
        self.assertIsInstance(result, StableAudioProvider)
        self.assertEqual(result.model, STABLE_AUDIO_MODEL)
        self.assertEqual(result.base_url, "https://api.stability.ai")
        self.assertEqual(result.max_response_bytes, DEFAULT_MAX_RESPONSE_BYTES)

    def test_environment_factory_rejects_disabled_unknown_and_wrong_model(self):
        cases = [
            ({}, "audio_provider_disabled"),
            ({"NAZ_AUDIO_PROVIDER": "other"}, "audio_provider_unknown"),
            ({
                "NAZ_AUDIO_PROVIDER": "stability",
                "NAZ_AUDIO_API_KEY": "key",
                "NAZ_AUDIO_MODEL": "stable-audio-3.0",
            }, "audio_model_invalid"),
        ]
        for env, code in cases:
            with self.subTest(code=code), self.assertRaises(AudioProviderError) as raised:
                provider_from_environment(env)
            self.assertEqual(raised.exception.code, code)

    def test_configuration_cannot_remove_the_hard_response_bound(self):
        with self.assertRaisesRegex(AudioProviderError, "audio_response_limit_invalid"):
            provider(FakeTransport(), max_response_bytes=ABSOLUTE_MAX_RESPONSE_BYTES + 1)

    def test_api_key_header_injection_is_rejected(self):
        with self.assertRaises(AudioProviderError) as raised:
            StableAudioProvider(api_key="key\r\nX-Evil: yes", transport=FakeTransport())
        self.assertEqual(raised.exception.code, "audio_api_key_missing")


if __name__ == "__main__":
    unittest.main()
