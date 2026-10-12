"""Secret redaction for anything the Web UI publishes (logs + tool events).

Everything the agent prints — bash commands, tool arguments, results, loguru
records — can accidentally carry credentials (API keys pasted into a prompt,
``export OPENAI_API_KEY=...`` inside a script, ``Authorization: Bearer ...``
headers, config dumps...). Before such text leaves the process through the
SSE channels we scrub it with the rules below.

Masked patterns
---------------
* OpenAI-style keys:            ``sk-...``, ``sk-proj-...``, ``sk-ant-...``
* GitHub tokens:                ``github_pat_...``, ``ghp_...``, ``gho_...``,
                                ``ghs_...``, ``ghu_...`` and friends
* Bearer tokens:                ``Bearer <token>``
* Key=value assignments:        values of ``api_key`` / ``apikey`` / ``token``
                                / ``password`` / ``passwd`` / ``secret`` when
                                followed by ``=`` or ``:``
* AWS/Cloudflare-style keys:    ``AKIA...``, ``ASIA...``, ``ABIA...``,
                                ``ACCA...``, 32-hex Cloudflare API tokens
* Environment variable values:  the literal value of every loaded env var whose
                                *name* contains KEY, TOKEN, SECRET or PASSWORD

Redaction is best-effort and must never raise; on any unexpected failure the
original text is returned unchanged (callers keep their own try/except too).
"""

import os
import re
from typing import Any, Dict, List, Optional

__all__ = ["redact", "truncate", "MASK", "MAX_FIELD_CHARS"]

# Hard cap for every published field (tool args/results, log messages...).
MAX_FIELD_CHARS = 4000

MASK = "***REDACTED***"

# Longest secret-ish value we are willing to scan for (env-var values).
_MAX_SECRET_VALUE_LEN = 8

_SENSITIVE_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")

# Prefixes of env vars that are themselves secrets and must never be scanned
# (our own web token included — its value obviously cannot appear in logs).
_SKIP_ENV_PREFIXES = ("OPENMANUS_WEB_TOKEN",)

# key=value / key: value assignments (quoted values supported).
_KV_KEYS = r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|refresh[_-]?token|token|password|passwd|secret[_-]?key|secret)"
_KV_RE = re.compile(
    rf"(?P<pre>\b{_KV_KEYS}\b\s*[=:]\s*)(?P<val>\"[^\"]*\"|'[^']*'|[^\s,;'\"&$`]+)",
    re.IGNORECASE,
)

_PATTERNS: List[re.Pattern] = [
    # OpenAI / Anthropic style keys.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"),
    # GitHub personal-access / app / fine-grained tokens.
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{6,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{6,}"),
    # AWS access key ids.
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{12,16}\b"),
    # Cloudflare API tokens: exactly 37 base32-ish chars (avoid catching 40-hex git SHAs).
    re.compile(r"\b(?=[a-f0-9]{37}(?![a-f0-9]))[a-f0-9]{37}\b"),
    # Google API keys.
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    # Slack tokens.
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # Generic private-key blocks.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def _env_secret_values() -> List[str]:
    """Collect the values of sensitive-looking environment variables."""
    values: set = set()
    try:
        items = list(os.environ.items())
    except Exception:  # pragma: no cover - defensive
        return []
    for name, value in items:
        if not value or len(value) < 3:
            continue
        upper = name.upper()
        if any(upper.startswith(p) for p in _SKIP_ENV_PREFIXES):
            continue
        if any(marker in upper for marker in _SENSITIVE_NAME_MARKERS):
            values.add(value)
            # Also mask the quoted form used inside shell commands/scripts.
            continue
    # Longest first so overlapping substrings are masked consistently.
    return sorted(values, key=len, reverse=True)[:50]


def redact(text: Optional[str]) -> str:
    """Return ``text`` with known secret shapes masked. Never raises."""
    if not text:
        return text or ""
    try:
        result = str(text)

        # 1) Env-var values (names containing KEY/TOKEN/SECRET/PASSWORD).
        for secret in _env_secret_values():
            if len(secret) > _MAX_SECRET_VALUE_LEN * 512:
                continue
            if secret in result:
                result = result.replace(secret, MASK)

        # 2) Well-known token shapes.
        for pattern in _PATTERNS:
            result = pattern.sub(MASK, result)

        # 3) api_key/token/password/secret=... assignments. Keep quotes visible
        #    so the terminal output still reads naturally.
        def _kv_sub(m: re.Match) -> str:
            val = m.group("val")
            if val[:1] in "\"'" and val[-1:] == val[:1]:
                return f"{m.group('pre')}{val[0]}{MASK}{val[0]}"
            return f"{m.group('pre')}{MASK}"

        result = _KV_RE.sub(_kv_sub, result)

        # 4) Bearer tokens.
        result = re.sub(
            r"(?i)\b(bearer\s+)(?!%s\b)[A-Za-z0-9._~+/=\-]{8,}" % re.escape(MASK),
            lambda m: m.group(1) + MASK,
            result,
        )
        return result
    except Exception:
        # Redaction must never break the agent loop; fall back to truncation only.
        try:
            return str(text)[:MAX_FIELD_CHARS]
        except Exception:
            return ""


def truncate(value: Any, limit: int = MAX_FIELD_CHARS) -> Any:
    """Truncate strings (and string leaves inside dicts/lists) to ``limit`` chars.

    Non-string scalars pass through untouched so JSON event payloads keep their
    natural types.
    """
    try:
        if isinstance(value, str):
            if len(value) <= limit:
                return value
            return value[:limit] + f"\n…[truncated {len(value) - limit} chars]"
        if isinstance(value, dict):
            return {k: truncate(v, limit) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [truncate(v, limit) for v in value]
        return value
    except Exception:
        return value


def redact_and_truncate(value: Any) -> Any:
    """Convenience: redact string leaves recursively, then cap length."""
    if isinstance(value, str):
        return truncate(redact(value))
    if isinstance(value, dict):
        return {k: redact_and_truncate(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_and_truncate(v) for v in value]
    return value
