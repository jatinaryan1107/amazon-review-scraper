"""A fixed, free OpenRouter transport with private server-side credentials."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import re
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from urllib import error, request

from review_analysis import AnalysisCancelled, AnalysisError


ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openrouter/free"
MAX_BYTES = 1024 * 1024
RESPONSE_BUDGET_SECONDS = 120


class ProviderError(AnalysisError):
    """Only a public reason and bounded retry hint may leave the transport."""

    def __init__(self, message, *, code="provider_error", retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _cancel(cancelled):
    if cancelled is not None and cancelled():
        raise AnalysisCancelled("Analysis was cancelled.")


def _loads(raw):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("Nonfinite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProviderRegistry:
    def __init__(self, environ=None, transport=None):
        self._environment = os.environ if environ is None else environ
        self._transport = transport
        self._session_key = ""
        self._require_zdr = False
        self._disabled = False
        self._lock = threading.RLock()

    def _key(self):
        with self._lock:
            value = self._environment.get("OPENROUTER_API_KEY", "")
            if isinstance(value, str) and value.strip():
                return value.strip(), "environment"
            if self._session_key:
                return self._session_key, "session"
            return "", "none"

    def public(self):
        key, source = self._key()
        return {"provider": "openrouter", "configured": bool(key) and not self._disabled, "key_source": source,
                "model": MODEL, "free_only": True, "privacy": "deny_training",
                "daily_limit": 50, "require_zdr": self._require_zdr, "disabled": self._disabled}

    def configure(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"api_key", "clear", "require_zdr"}:
            raise ProviderError("Supply a session API key or clear the saved session key.", code="configuration")
        if "require_zdr" in payload and not isinstance(payload["require_zdr"], bool):
            raise ProviderError("Zero data retention must be true or false.", code="configuration")
        if "api_key" in payload and "clear" in payload:
            raise ProviderError("Choose either a session key or clear, not both.", code="configuration")
        if "clear" in payload and payload["clear"] is not True:
            raise ProviderError("Clear must be true.", code="configuration")
        key = payload.get("api_key")
        if key is not None and (not isinstance(key, str) or not key.strip()
                                or len(key) > 512 or any(ord(char) < 33 or ord(char) > 126 for char in key.strip())):
            raise ProviderError("Enter a nonempty API key without whitespace or control characters.", code="configuration")
        if not payload or (key is None and "clear" not in payload and "require_zdr" not in payload):
            raise ProviderError("Supply a session API key or clear the saved session key.", code="configuration")
        with self._lock:
            if "api_key" in payload:
                if key is None:
                    raise ProviderError("Enter a nonempty API key.", code="configuration")
                if self._key()[1] == "environment":
                    raise ProviderError("The OpenRouter key is set in the server environment. Session keys cannot replace it.", code="environment_key")
                self._session_key = key.strip()
                self._disabled = False
            if payload.get("clear"):
                self._session_key = ""
            if "require_zdr" in payload:
                self._require_zdr = payload["require_zdr"]
        return self.public()

    def _request(self, payload, key, *, cancelled=None):
        outgoing = request.Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                   method="POST", headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json", "Accept": "application/json",
                                   "X-OpenRouter-Cache": "false"})
        started = time.monotonic()
        try:
            with request.build_opener(_NoRedirect()).open(outgoing, timeout=40) as response:
                body = bytearray()
                while True:
                    _cancel(cancelled)
                    if time.monotonic() - started >= RESPONSE_BUDGET_SECONDS:
                        raise ProviderError("The free provider did not finish within the response budget. Its attempt remains counted.", code="timeout")
                    # read1 returns available bytes, including provider heartbeats.
                    # A buffered read(n) can wait indefinitely for n bytes or EOF.
                    part = response.read1(min(65_536, MAX_BYTES + 1 - len(body)))
                    _cancel(cancelled)
                    if time.monotonic() - started >= RESPONSE_BUDGET_SECONDS:
                        raise ProviderError("The free provider did not finish within the response budget. Its attempt remains counted.", code="timeout")
                    if not part:
                        break
                    body.extend(part)
                    if len(body) > MAX_BYTES:
                        raise ProviderError("The provider response was too large.", code="invalid_response")
            return _loads(body.decode("utf-8"))
        except error.HTTPError as exc:
            status = exc.code
            retry_after = None
            try:
                value = float(exc.headers.get("Retry-After", ""))
                if math.isfinite(value) and 0 <= value <= 86400:
                    retry_after = value
            except (TypeError, ValueError):
                pass
            exc.close()
            if status in {401, 403}:
                raise ProviderError("OpenRouter could not authorize this request. Check the server key and account settings.", code="authorization") from None
            if status == 429:
                raise ProviderError("OpenRouter reached a free request limit. Completed chunks are saved; resume later.", code="rate_limit", retry_after=retry_after) from None
            if status == 404:
                raise ProviderError("No free endpoint currently meets the required features and privacy settings.", code="unavailable") from None
            if status == 402:
                raise ProviderError("OpenRouter declined the account or key budget. No paid fallback was attempted.", code="budget") from None
            raise ProviderError("OpenRouter could not complete this request. No paid fallback was attempted.", code="unavailable") from None
        except (ProviderError, AnalysisCancelled):
            raise
        except TimeoutError:
            raise ProviderError("The free provider timed out. Its attempt remains counted.", code="timeout") from None
        except error.URLError as exc:
            code = "timeout" if isinstance(exc.reason, TimeoutError) else "connection"
            raise ProviderError("The provider connection did not finish. Its attempt remains counted.", code=code) from None
        except OSError:
            raise ProviderError("The provider request could not finish. Its attempt remains counted for safety.", code="connection") from None
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderError("The provider response was not valid JSON.", code="invalid_response") from None

    def _usage(self, usage):
        if not isinstance(usage, Mapping):
            raise ProviderError("The provider returned invalid usage data.", code="invalid_response")
        safe = {"available": bool(usage), "cost": None}
        if usage.get("cost") is not None:
            try:
                cost = Decimal(str(usage["cost"]))
            except (InvalidOperation, ValueError):
                raise ProviderError("The provider returned invalid cost data.", code="invalid_response") from None
            if not cost.is_finite() or cost < 0:
                raise ProviderError("The provider returned invalid cost data.", code="invalid_response")
            if cost > 0:
                self._disabled = True
                raise ProviderError("OpenRouter reported a nonzero cost. Further AI requests are disabled; check the account before reconnecting.", code="cost_guard")
            safe["cost"] = 0.0
        for source, target in [("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")]:
            value = usage.get(source)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000_000):
                raise ProviderError("The provider returned invalid usage data.", code="invalid_response")
            safe[target] = value
        return safe

    def chat(self, messages, *, schema=None, tools=None, max_tokens=2500, cancelled=None, transport=None):
        _cancel(cancelled)
        if self._disabled:
            raise ProviderError("Free AI requests are disabled after a nonzero cost signal. Check the provider account before reconnecting.", code="cost_guard")
        key, _ = self._key()
        if not key:
            raise ProviderError("Connect an OpenRouter API key to use free AI analysis.", code="not_configured")
        if not isinstance(messages, list) or not messages or len(messages) > 30:
            raise ProviderError("The model conversation is invalid or too long.", code="invalid_request")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 6000:
            raise ProviderError("The model output budget is invalid.", code="invalid_request")
        payload = {"model": MODEL, "messages": messages, "stream": False, "max_tokens": max_tokens,
                   "reasoning": {"enabled": False},
                   "provider": {"require_parameters": True, "data_collection": "deny",
                                "max_price": {"prompt": 0, "completion": 0, "request": 0}},
                   "plugins": [{"id": "context-compression", "enabled": False}]}
        if self._require_zdr:
            payload["provider"]["zdr"] = True
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "reviewdesk_result", "strict": True, "schema": schema}}
        if tools is not None:
            payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        try:
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
                raise ProviderError("The model request is too large.", code="invalid_request")
            result = (transport or self._transport)(payload) if (transport or self._transport) else self._request(payload, key, cancelled=cancelled)
            if not isinstance(result, Mapping):
                raise ProviderError("The provider returned an error instead of an answer.", code="invalid_response")
            safe_usage = self._usage(result.get("usage", {}))
            _cancel(cancelled)
            if result.get("error"):
                raise ProviderError("The provider returned an error instead of an answer.", code="invalid_response")
            choices = result.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
                raise ProviderError("The provider returned an invalid answer.", code="invalid_response")
            choice = choices[0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ProviderError("The provider did not finish its answer. No partial output was accepted.", code="incomplete")
            message = choice.get("message")
            if not isinstance(message, Mapping):
                raise ProviderError("The provider returned an invalid message.", code="invalid_response")
            content, calls = message.get("content"), message.get("tool_calls", [])
            if content is None:
                content = ""
            if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_BYTES or not isinstance(calls, list) or len(calls) > 3:
                raise ProviderError("The provider returned an invalid message.", code="invalid_response")
            if message.get("refusal") or (not content.strip() and not calls):
                raise ProviderError("The model declined this request or returned no answer.", code="refusal")
            model = result.get("model")
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
                raise ProviderError("The provider omitted its model identity.", code="invalid_response")
            return {"content": content, "tool_calls": calls, "model": model, "usage": safe_usage}
        except (ProviderError, AnalysisCancelled):
            raise
        except Exception:
            raise ProviderError("The provider response could not be validated.", code="invalid_response") from None
