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
QUOTA_ENDPOINT = "https://openrouter.ai/api/v1/key"
MODEL = "openrouter/free"
MAX_BYTES = 1024 * 1024
RESPONSE_BUDGET_SECONDS = 120


class ProviderError(AnalysisError):
    """Only a public reason and bounded retry hint may leave the transport."""

    def __init__(self, message, *, code="provider_error", retry_after=None, diagnostics=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.diagnostics = public_diagnostics(diagnostics)


def public_diagnostics(value):
    """Only small typed fields can leave provider error handling."""
    if not isinstance(value, Mapping):
        return {}
    safe = {}
    source = value.get("rate_limit_source")
    if isinstance(source, str) and source in {"openrouter", "provider", "unknown"}:
        safe["rate_limit_source"] = source
    for field, low, high in [("http_status", 100, 599), ("error_status", 100, 599),
                             ("provider_code", 100, 599), ("limit", 0, 10_000_000),
                             ("remaining", 0, 10_000_000), ("reset_at", 1, 4_102_444_800)]:
        number = value.get(field)
        if type(number) is int and low <= number <= high:
            safe[field] = number
    return safe


def _error_diagnostics(status, failure=None, headers=None, http_status=None):
    safe = {"error_status": status, "http_status": http_status}
    if status != 429:
        return public_diagnostics(safe)
    safe["rate_limit_source"] = "unknown"
    metadata = failure.get("metadata") if isinstance(failure, Mapping) else None
    provider_code = metadata.get("provider_code") if isinstance(metadata, Mapping) else None
    if (type(provider_code) is int or isinstance(provider_code, str)) and provider_code != "":
        safe["rate_limit_source"] = "provider"
    if type(provider_code) is int and 100 <= provider_code <= 599:
        safe.update(provider_code=provider_code, rate_limit_source="provider")
    headers = headers or {}
    for header, field in [("X-RateLimit-Limit", "limit"), ("X-RateLimit-Remaining", "remaining"),
                          ("X-RateLimit-Reset", "reset_at")]:
        raw = headers.get(header)
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,13}", raw):
            continue
        number = int(raw)
        if field == "reset_at" and number > 4_102_444_800:
            number //= 1000
        safe[field] = number
    safe = public_diagnostics(safe)
    if safe.get("rate_limit_source") == "unknown" and any(field in safe for field in ("limit", "remaining", "reset_at")):
        safe["rate_limit_source"] = "openrouter"
    return safe


def _raise_provider_error(status, *, failure=None, headers=None, http_status=None):
    code = {400: "invalid_request", 401: "authorization", 402: "budget", 403: "authorization",
            404: "unavailable", 429: "rate_limit", 500: "unavailable", 502: "unavailable",
            503: "unavailable"}.get(status, "invalid_response") if type(status) is int else "invalid_response"
    retry_after = None
    try:
        raw = (headers or {}).get("Retry-After", "")
        number = float(raw)
        if math.isfinite(number) and 0 <= number <= 86400:
            retry_after = number
    except (ValueError, TypeError):
        pass
    raise ProviderError("The provider did not complete this request.", code=code, retry_after=retry_after,
                        diagnostics=_error_diagnostics(status, failure, headers, http_status))


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


def chat_payload(messages, *, schema=None, tools=None, require_tool=False, max_tokens=2500, require_zdr=False):
    """Build the same credential-free payload for planning and transmission."""
    payload = {"model": MODEL, "messages": messages, "stream": False, "max_tokens": max_tokens,
               "reasoning": {"enabled": False},
               "provider": {"require_parameters": True, "data_collection": "deny",
                            "max_price": {"prompt": 0, "completion": 0, "request": 0}},
               "plugins": [{"id": "context-compression", "enabled": False}]}
    if require_zdr:
        payload["provider"]["zdr"] = True
    if schema is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "reviewdesk_result", "strict": True, "schema": schema}}
    if tools is not None:
        payload.update(tools=tools, tool_choice="required" if require_tool else "auto")
    return payload


class ProviderRegistry:
    def __init__(self, environ=None, transport=None, quota_transport=None):
        self._environment = os.environ if environ is None else environ
        self._transport = transport
        self._quota_transport = quota_transport
        self._quota_lock = threading.Lock()
        self._session_key = ""
        self._require_zdr = True
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
                headers, status = getattr(response, "headers", {}), getattr(response, "status", 200)
            result = _loads(body.decode("utf-8"))
            if isinstance(result, Mapping) and result.get("error"):
                failure = result["error"]
                code = failure.get("code") if isinstance(failure, Mapping) else None
                _raise_provider_error(code, failure=failure, headers=headers, http_status=status)
            return result
        except error.HTTPError as exc:
            status = exc.code
            headers = exc.headers
            failure = None
            try:
                if exc.fp is not None:
                    body = bytearray()
                    started = time.monotonic()
                    while len(body) <= 16_384 and time.monotonic() - started < 5:
                        part = exc.read1(16_385 - len(body))
                        if not part:
                            break
                        body.extend(part)
                    raw = bytes(body)
                    if len(raw) <= 16_384:
                        parsed = _loads(raw.decode("utf-8"))
                        failure = parsed.get("error") if isinstance(parsed, Mapping) else None
            except (OSError, ValueError, UnicodeError, RecursionError):
                pass
            exc.close()
            _raise_provider_error(status, failure=failure, headers=headers, http_status=status)
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

    def check_quota(self):
        """Read account counters without review text or a generation request."""
        key, _ = self._key()
        if not key:
            raise ProviderError("Connect an OpenRouter key before checking its quota.", code="not_configured")
        if not self._quota_lock.acquire(blocking=False):
            raise ProviderError("A quota check is already running.", code="check_busy")
        try:
            if self._quota_transport is not None:
                result = self._quota_transport()
            else:
                outgoing = request.Request(QUOTA_ENDPOINT, method="GET", headers={
                    "Authorization": "Bearer " + key, "Accept": "application/json", "Cache-Control": "no-store"})
                started = time.monotonic()
                with request.build_opener(_NoRedirect()).open(outgoing, timeout=8) as response:
                    body = bytearray()
                    while True:
                        if time.monotonic() - started >= 10:
                            raise ProviderError("The account check timed out.", code="timeout")
                        part = response.read1(min(16_384, 65_537 - len(body)))
                        if not part:
                            break
                        body.extend(part)
                        if len(body) > 65_536:
                            raise ProviderError("The account response was too large.", code="invalid_response")
                result = _loads(body.decode("utf-8"))
            if isinstance(result, Mapping) and result.get("error"):
                failure = result["error"]
                status = failure.get("code") if isinstance(failure, Mapping) else None
                _raise_provider_error(status, failure=failure)
            if self._key()[0] != key:
                raise ProviderError("The configured key changed during the quota check.", code="configuration")
            if not isinstance(result, Mapping) or not isinstance(result.get("data"), Mapping):
                raise ProviderError("The account response was invalid.", code="invalid_response")
            counter = result["data"].get("free_model_daily_requests")
            quota = {"available": False, "used": None, "limit": None, "remaining": None}
            if counter is not None:
                if (not isinstance(counter, Mapping) or any(type(counter.get(field)) is not int
                        or not 0 <= counter[field] <= 10_000_000 for field in ("used", "limit", "remaining"))
                        or counter["remaining"] > counter["limit"]):
                    raise ProviderError("The account counter was invalid.", code="invalid_response")
                quota.update(available=True, **{field: counter[field] for field in ("used", "limit", "remaining")})
            checked = int(time.time())
            return {"quota": quota, "checked_at": checked, "resets_at": (checked // 86400 + 1) * 86400,
                    "model_calls": 0}
        except error.HTTPError as exc:
            status, headers = exc.code, exc.headers
            exc.close()
            _raise_provider_error(status, headers=headers, http_status=status)
        except (ProviderError, AnalysisCancelled):
            raise
        except TimeoutError:
            raise ProviderError("The account check timed out.", code="timeout") from None
        except OSError:
            raise ProviderError("The account check could not finish.", code="connection") from None
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderError("The account response was invalid.", code="invalid_response") from None
        except Exception:
            raise ProviderError("The account response could not be validated.", code="invalid_response") from None
        finally:
            self._quota_lock.release()

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

    def chat(self, messages, *, schema=None, tools=None, require_tool=False, max_tokens=2500, cancelled=None, transport=None):
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
        if not isinstance(require_tool, bool) or (require_tool and not tools):
            raise ProviderError("Required tool selection needs available tools.", code="invalid_request")
        payload = chat_payload(messages, schema=schema, tools=tools, require_tool=require_tool,
                               max_tokens=max_tokens, require_zdr=self._require_zdr)
        try:
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
                raise ProviderError("The model request is too large.", code="invalid_request")
            result = (transport or self._transport)(payload) if (transport or self._transport) else self._request(payload, key, cancelled=cancelled)
            if not isinstance(result, Mapping):
                raise ProviderError("The provider returned an error instead of an answer.", code="invalid_response")
            safe_usage = self._usage({} if result.get("usage") is None else result["usage"])
            _cancel(cancelled)
            if result.get("error"):
                failure = result["error"]
                status = failure.get("code") if isinstance(failure, Mapping) else None
                _raise_provider_error(status, failure=failure)
            choices = result.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
                raise ProviderError("The provider returned an invalid answer.", code="invalid_response")
            choice = choices[0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ProviderError("The provider did not finish its answer. No partial output was accepted.", code="incomplete")
            message = choice.get("message")
            if not isinstance(message, Mapping):
                raise ProviderError("The provider returned an invalid message.", code="invalid_response")
            content, calls = message.get("content"), message.get("tool_calls")
            if calls is None:
                calls = []
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
