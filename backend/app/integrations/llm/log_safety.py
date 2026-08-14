"""LLM 调试日志的凭据脱敏工具。"""

import re
from collections.abc import Iterable

REDACTED_VALUE = "***REDACTED***"
# 同时处理常见键值、Bearer 和 OpenAI 风格密钥，避免调试日志意外留下凭据。
INLINE_SECRET_PATTERNS = (
    re.compile(
        r"(?i)((?:api[_ -]?key|authorization|token|secret|password)"
        r"\s*[\"']?\s*[:=]\s*[\"']?)([^\s,\"'}]+)"
    ),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._-]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def redact_debug_text(
    text: str,
    configured_secrets: Iterable[str | None] = (),
) -> str:
    """替换调试文本中的已配置密钥和常见内联凭据。"""

    # 第一步：先替换当前调用已加载的真实密钥，防止其出现在上游响应或用户内容中。
    redacted = text
    for secret in configured_secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED_VALUE)

    # 第二步：再处理常见的键值、Bearer 和 sk- 格式，覆盖意外回显的其他凭据。
    for index, pattern in enumerate(INLINE_SECRET_PATTERNS):
        if index < 2:
            redacted = pattern.sub(rf"\1{REDACTED_VALUE}", redacted)
        else:
            redacted = pattern.sub(REDACTED_VALUE, redacted)
    return redacted
