from __future__ import annotations
import re
from dataclasses import dataclass

@dataclass(frozen=True)
class SensitiveMatch:
    category: str
    redacted_hint: str

# 身份证号单独处理，理由见 _national_id_hits 的注释：
# 只是"18 位连续数字 + 像日期"的串在随机摘要、流水号里会偶然出现，必须加 token 边界与校验位。
_NATIONAL_ID = re.compile(r"(?<![0-9A-Za-z])[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?![0-9A-Za-z])")
_NATIONAL_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_NATIONAL_ID_CHECK = "10X98765432"

_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I)),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I)),
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|(?:api[_-]?key|secret[_-]?key)\s*[:=]\s*[A-Za-z0-9._~+/=-]{12,})\b", re.I)),
    ("password", re.compile(r"\b(?:password|passwd|pwd)\s*[:=]\s*\S+", re.I)),
    ("access_token", re.compile(r"\b(?:access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+", re.I)),
)

def _national_id_checksum_ok(code: str) -> bool:
    total = sum(int(ch) * weight for ch, weight in zip(code[:17], _NATIONAL_ID_WEIGHTS))
    return _NATIONAL_ID_CHECK[total % 11] == code[17].upper()

def _national_id_hits(text: str) -> bool:
    """只有当整段是独立 token 且 GB 11643 校验位正确时才算命中。

    - token 边界：记忆 id、sha256 指纹、uuid 这类十六进制串里偶然出现的
      18 位数字段前后都紧挨着字母数字，不能当成身份证号（这是此前并行回归中
      偶发 sensitive_memory_content_rejected:national_id 的来源）。
    - 校验位：纯数字流水号、时间戳拼接串仍可能恰好凑出合法日期，校验位把它们
      再挡掉约 10/11；真实身份证号（含 X 结尾）一律通过，所以不会漏报。
    """
    for match in _NATIONAL_ID.finditer(text):
        if _national_id_checksum_ok(match.group(0)):
            return True
    return False

def find_sensitive(text: str) -> list[SensitiveMatch]:
    if not isinstance(text, str):
        raise TypeError("sensitive_scan_requires_text")
    findings = [SensitiveMatch(category, "redacted") for category, pattern in _PATTERNS if pattern.search(text)]
    if _national_id_hits(text):
        findings.append(SensitiveMatch("national_id", "redacted"))
    return findings

def assert_safe_memory_content(*parts: str) -> None:
    findings = []
    for part in parts:
        findings.extend(find_sensitive(part))
    if findings:
        categories = ",".join(sorted({item.category for item in findings}))
        raise ValueError(f"sensitive_memory_content_rejected:{categories}")
