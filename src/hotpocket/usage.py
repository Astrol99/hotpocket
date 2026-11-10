"""What the API's replies say about usage: each account's limits, in its rate-limit headers, and the
tokens a reply used, with what they would cost at API prices."""

import datetime
import json
import time

from genai_prices import extract_usage

UNIFIED = "anthropic-ratelimit-unified-"  # the subscription's rate-limit headers
WINDOWS = {"five_hour": "5h", "seven_day": "7d"}  # our name for each limit -> its header infix
FALLBACK_BLOCK = 3600  # how long a usage-limit rejection without a reset time blocks an account
# An API key's answers when its credits or spend limit are used up (a 402 billing_error, or these in a 429 or 400).
OUT_OF_CREDIT = (b"enforced_spend_limit_reached", b"credit balance", b"reached your specified")
# Each plan's usage limits as a multiple of the Pro plan's, as Anthropic publishes them (support.claude.com). A Team
# seat's are published per 5-hour session only; its weekly limit is assumed to be the same multiple. The pool's
# totals weigh accounts by them.
PLANS = {"team_standard": ("Team Standard", 1.25), "team_premium": ("Team Premium", 6.25),
         "pro": ("Pro", 1), "max": ("Max", 5), "ultra": ("Ultra", 10)}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def num(headers, name):
    try:
        return float(headers[name])
    except (KeyError, ValueError):
        return None


def limits(headers):
    """Each window's use (0-1) and reset time, from the headers that have them: {"five_hour": 0.42, ...}."""
    found = ((window + suffix, num(headers, f"{UNIFIED}{infix}-{name}"))
             for window, infix in WINDOWS.items() for suffix, name in (("", "utilization"), ("_reset", "reset")))
    return {key: value for key, value in found if value is not None}


def next_month():
    """00:00 UTC on the 1st of next month, when a spent API key's monthly credits and limits come back."""
    now = datetime.datetime.now(datetime.UTC)
    return datetime.datetime(now.year + now.month // 12, now.month % 12 + 1, 1, tzinfo=datetime.UTC).timestamp()


def reset_time(headers):
    """When a usage-limit rejection lifts, from the response headers."""
    rejected = [num(headers, f"{UNIFIED}{infix}-reset") for infix in WINDOWS.values()
                if headers.get(f"{UNIFIED}{infix}-status") == "rejected"]
    retry = num(headers, "retry-after")
    return (num(headers, UNIFIED + "reset") or max(filter(None, rejected), default=None)
            or time.time() + (retry if retry is not None else FALLBACK_BLOCK))


def usage_of(reply):
    """The model and token usage in a reply. Streamed (SSE), message_start has them and message_delta
    updates the usage; any other reply is one JSON object. Replies without usage give {}."""
    model, usage = None, {}
    for line in reply.splitlines() if reply.startswith(b"event:") else [reply]:
        if b'"usage"' in line:
            try:
                event = json.loads(line.removeprefix(b"data:"))
                message = event.get("message", event)
                # A later event's null or 0 doesn't erase a count already heard (Claude Code merges them so too).
                fresh = {k: v for k, v in message["usage"].items() if v or k not in usage}
                model, usage = message.get("model", model), {**usage, **fresh}
            except (ValueError, AttributeError, KeyError, TypeError):
                pass
    return model, usage


def api_cost(model, usage):
    """What a reply would have cost at API list prices, from genai-prices (update it for new models).
    The seats themselves are flat-rate; 0 for a model it doesn't know or a reply without usage."""
    try:
        return float(extract_usage({"model": model, "usage": usage}, provider_id="anthropic").calc_price().total_price)
    except (LookupError, ValueError):
        return 0.0
