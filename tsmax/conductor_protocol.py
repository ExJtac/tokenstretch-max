"""Small shared decoding helpers; provider token meanings stay in adapters."""
import json
import math


def json_objects(text):
    try:
        obj = json.loads(text)
        return [obj] if isinstance(obj, dict) else []
    except (ValueError, RecursionError):
        objects = []
        for line in text.splitlines():
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    objects.append(obj)
            except (ValueError, RecursionError):
                if line.strip():
                    return []
        return objects


def nonnegative(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value if isinstance(value, int) or math.isfinite(value) else None


def failure_status(text):
    text = (text or "").lower()
    if any(s in text for s in ("unauthorized", "authentication", "not logged in", "sign in", "login required", "401")):
        return "auth-required"
    if any(s in text for s in ("quota", "rate limit", "rate_limit", "usage limit", "insufficient credit", "429")):
        return "quota-exhausted"
    return "failed"
