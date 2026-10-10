from __future__ import annotations
import re
from dataclasses import dataclass

@dataclass(frozen=True)
class SensitiveMatch:
    category: str
    redacted_hint: str

_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I)),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I)),
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|(?:api[_-]?key|secret[_-]?key)\s*[:=]\s*[A-Za-z0-9._~+/=-]{12,})\b", re.I)),
    ("password", re.compile(r"\b(?:password|passwd|pwd)\s*[:=]\s*\S+", re.I)),
    ("access_token", re.compile(r"\b(?:access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+", re.I)),
    ("national_id", re.compile(r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\d)")),
)

def find_sensitive(text: str) -> list[SensitiveMatch]:
    if not isinstance(text, str):
        raise TypeError("sensitive_scan_requires_text")
    return [SensitiveMatch(category, "redacted") for category, pattern in _PATTERNS if pattern.search(text)]

def assert_safe_memory_content(*parts: str) -> None:
    findings = []
    for part in parts:
        findings.extend(find_sensitive(part))
    if findings:
        categories = ",".join(sorted({item.category for item in findings}))
        raise ValueError(f"sensitive_memory_content_rejected:{categories}")
