"""Provider tests use synthetic credentials and fake transports only."""

import json
import io
import unittest
from unittest.mock import patch
from urllib import error

from llm_provider import MODEL, ProviderError, ProviderRegistry
from review_analysis import AnalysisCancelled


KEY = "synthetic-private-provider-key"


def completion(content='{"ok":true}', **changes):
    return {"model": "fictional/model:free", "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0}, **changes}


class ProviderTests(unittest.TestCase):
    def registry(self, transport=None, environment=None):
        registry = ProviderRegistry(environ=environment or {}, transport=transport)
        if environment is None:
            registry.configure({"api_key": KEY})
        return registry

    def test_configuration_never_echoes_key_and_clear_preserves_environment(self):
        registry = ProviderRegistry(environ={})
        self.assertFalse(registry.public()["configured"])
        public = registry.configure({"api_key": KEY, "require_zdr": True})
        self.assertEqual(public["key_source"], "session")
        self.assertTrue(public["require_zdr"])
        self.assertNotIn(KEY, json.dumps(public))
        registry.configure({"clear": True})
        self.assertFalse(registry.public()["configured"])
        registry = ProviderRegistry(environ={"OPENROUTER_API_KEY": KEY})
        with self.assertRaises(ProviderError):
            registry.configure({"api_key": "another-synthetic-key"})
        self.assertEqual(registry.configure({"clear": True})["key_source"], "environment")
        self.assertTrue(registry.configure({"require_zdr": True})["configured"])

    def test_transport_uses_free_only_policy_and_normalizes_actual_model_usage(self):
        sent = []
        def transport(payload):
            sent.append(payload)
            return completion()
        registry = self.registry(transport)
        registry.configure({"require_zdr": True})
        result = registry.chat([{"role": "user", "content": "Synthetic request"}], schema={"type": "object"})
        payload = sent[0]
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["provider"], {"require_parameters": True, "data_collection": "deny", "zdr": True,
                                               "max_price": {"prompt": 0, "completion": 0, "request": 0}})
        self.assertEqual(payload["plugins"], [{"id": "context-compression", "enabled": False}])
        self.assertNotIn(KEY, json.dumps(payload))
        self.assertEqual(result["model"], "fictional/model:free")
        self.assertEqual(result["usage"]["input_tokens"], 10)

    def test_nonzero_cost_disables_subsequent_calls_without_another_request(self):
        calls = []
        registry = self.registry(lambda payload: calls.append(payload) or completion(usage={"cost": "0.0001"}))
        with self.assertRaises(ProviderError) as caught:
            registry.chat([{"role": "user", "content": "Synthetic"}])
        self.assertEqual(caught.exception.code, "cost_guard")
        with self.assertRaises(ProviderError):
            registry.chat([{"role": "user", "content": "Synthetic"}])
        self.assertEqual(len(calls), 1)
        self.assertTrue(registry.public()["disabled"])
        self.assertFalse(registry.public()["configured"])

    def test_missing_usage_is_unknown_and_raw_transport_errors_are_private(self):
        registry = self.registry(lambda payload: completion(usage={}))
        result = registry.chat([{"role": "user", "content": "Synthetic"}])
        self.assertIsNone(result["usage"]["input_tokens"])
        self.assertIsNone(result["usage"]["cost"])
        def fail(payload):
            raise RuntimeError(KEY)
        registry = self.registry(fail)
        with self.assertRaises(ProviderError) as caught:
            registry.chat([{"role": "user", "content": "Synthetic"}])
        self.assertNotIn(KEY, str(caught.exception))

    def test_network_policy_has_no_redirects_remote_cache_or_retries(self):
        registry = self.registry()
        response = io.BytesIO(json.dumps(completion()).encode())
        captured = []
        class Opener:
            def open(self, outgoing, timeout):
                captured.append((outgoing, timeout))
                return response
        with patch("llm_provider.request.build_opener", return_value=Opener()) as build:
            registry.chat([{"role": "user", "content": "Synthetic"}])
        outgoing, timeout = captured[0]
        self.assertEqual(outgoing.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(outgoing.get_header("X-openrouter-cache"), "false")
        self.assertEqual(outgoing.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(timeout, 40)
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_rate_error_exposes_only_public_retry_hint_and_no_raw_response(self):
        registry = self.registry()
        failure = error.HTTPError("https://openrouter.ai/api/v1/chat/completions", 429, KEY, {"Retry-After": "9"}, None)
        with patch("llm_provider.request.build_opener") as build:
            build.return_value.open.side_effect = failure
            with self.assertRaises(ProviderError) as caught:
                registry.chat([{"role": "user", "content": "Synthetic"}])
        self.assertEqual((caught.exception.code, caught.exception.retry_after), ("rate_limit", 9))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(build.return_value.open.call_count, 1)

    def test_malformed_or_truncated_outputs_are_not_accepted(self):
        for response in [completion(choices=[]), completion(choices=[{"finish_reason": "length", "message": {"content": "{"}}]),
                         completion(model="<script>bad</script>"), completion(usage={"cost": "NaN"}),
                         completion(usage={"prompt_tokens": True})]:
            with self.subTest(response=response), self.assertRaises(ProviderError):
                self.registry(lambda payload: response).chat([{"role": "user", "content": "Synthetic"}])

    def test_keepalive_bytes_cannot_extend_the_response_budget(self):
        clock = [0]
        class Heartbeats(io.BytesIO):
            def read1(self, limit):
                clock[0] += 30
                return b" "
        body = Heartbeats()
        with patch("llm_provider.time.monotonic", side_effect=lambda: clock[0]), patch("llm_provider.request.build_opener") as build:
            build.return_value.open.return_value = body
            with self.assertRaises(ProviderError) as caught:
                self.registry().chat([{"role": "user", "content": "Synthetic"}])
        self.assertEqual(caught.exception.code, "connection")
        self.assertEqual(clock[0], 120)
        self.assertTrue(body.closed)
        self.assertEqual(build.return_value.open.call_count, 1)

    def test_cancellation_during_provider_heartbeat_closes_response(self):
        cancelled = [False]
        class Heartbeats(io.BytesIO):
            def read1(self, limit):
                cancelled[0] = True
                return b" "
        body = Heartbeats()
        with patch("llm_provider.request.build_opener") as build:
            build.return_value.open.return_value = body
            with self.assertRaises(AnalysisCancelled):
                self.registry().chat([{"role": "user", "content": "Synthetic"}], cancelled=lambda: cancelled[0])
        self.assertTrue(body.closed)
        self.assertEqual(build.return_value.open.call_count, 1)

    def test_incremental_read_still_enforces_response_size(self):
        body = io.BytesIO(b"x" * 1025)
        with patch("llm_provider.MAX_BYTES", 1024), patch("llm_provider.request.build_opener") as build:
            build.return_value.open.return_value = body
            with self.assertRaises(ProviderError) as caught:
                self.registry().chat([{"role": "user", "content": "S"}])
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
