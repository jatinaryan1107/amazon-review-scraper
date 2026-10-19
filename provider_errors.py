"""Public provider failures shared by HTTP handlers and background jobs."""

import math

from llm_provider import public_diagnostics


PROVIDER_MESSAGES = {
    "configuration": "Check the OpenRouter settings and try again.",
    "environment_key": "OpenRouter is configured by the server environment. Change its key there.",
    "storage": "Windows could not update the saved key. Existing settings were kept. Try again or use a session-only key.",
    "not_configured": "Add an OpenRouter key in Settings before using AI.",
    "invalid_request": "The AI request could not be accepted. Check the selected scope and settings.",
    "authorization": "OpenRouter could not authorize this request. Check the configured key.",
    "rate_limit": "The free AI service is limiting requests. Your daily allowance may still be available; local evidence remains usable.",
    "cooldown": "Free AI is cooling down after a provider limit. Local evidence remains available. Try AI again after the displayed wait.",
    "research_storage": "The local research cache or request ledger is unavailable. Related source reviews are shown; check the local data storage before another AI attempt.",
    "check_busy": "An account quota check is already running. Wait for its result.",
    "unavailable": "A compatible free model route is unavailable. The saved response does not identify why.",
    "budget": "The analysis call budget was reached. Completed chunks are saved.",
    "connection": "The provider request could not be completed. Inspect the job before trying again.",
    "timeout": "The free model took too long. No answer was accepted. This attempt still counts; check OpenRouter Logs before retrying.",
    "invalid_response": "The provider returned a response that could not be validated.",
    "invalid_json": "The model did not return valid JSON. No answer was saved; this attempt still counts.",
    "report_format": "The model response did not match the required report format. No answer was saved.",
    "evidence_reference": "The model cited missing or duplicate review evidence. No answer was saved.",
    "evidence_quote": "A model quote did not match its source review. No answer was saved.",
    "incomplete": "The provider did not finish its response. Completed chunks are saved.",
    "refusal": "The model declined this request. Local analysis remains available.",
    "daily_limit": "The daily free request limit was reached. Try again after the limit resets.",
    "cost_guard": "The provider reported a charge. Further AI requests are disabled for this server session.",
    "call_budget": "The per-job AI call budget was reached. Completed chunks are saved.",
}


def provider_failure(error):
    """Expose bounded provider diagnostics without exception or request content."""
    code = getattr(error, "code", "provider_error")
    if not isinstance(code, str) or code not in PROVIDER_MESSAGES:
        code = "provider_error"
    result = {"error_code": code}
    retry = getattr(error, "retry_after", None)
    if (not isinstance(retry, bool) and isinstance(retry, (int, float))
            and math.isfinite(retry) and 0 <= retry <= 86400):
        result["retry_after"] = math.ceil(retry)
    details = public_diagnostics(getattr(error, "diagnostics", None))
    if details:
        result["diagnostics"] = details
    message = PROVIDER_MESSAGES.get(code, "AI analysis could not finish. Completed chunks are retained.")
    if code == "evidence_reference":
        message = {
            "unknown_passage": "The model cited a passage label that was not supplied. No answer was saved.",
            "repeated_passage": "The model repeated one passage within a topic. No answer was saved.",
            "same_review": "The model used multiple passages from one review as separate sources within a topic. No answer was saved.",
        }.get(details.get("evidence_reason"), message)
    if code == "rate_limit" and details.get("rate_limit_source") == "provider":
        message = "The model provider is limiting requests. Your daily OpenRouter allowance may still be available."
    elif code == "rate_limit" and details.get("rate_limit_source") == "openrouter":
        message = "OpenRouter blocked this request under a platform rate limit. Check the account counter and reset details."
    if code in {"unavailable", "invalid_request"}:
        message = {
            "privacy_policy": "OpenRouter reported a data-policy routing restriction. Check account and key policies before another run.",
            "required_parameters": "OpenRouter reported no route supporting the required parameters. Review the response format and requested features.",
            "region_policy": "OpenRouter reported a region routing restriction. Review the account and provider region requirements.",
            "model_unavailable": "OpenRouter reported that the requested model is unavailable.",
            "no_matching_endpoint": "OpenRouter reported no matching endpoint, without identifying which requirement excluded it.",
        }.get(details.get("routing_reason"), message)
    return message, result


