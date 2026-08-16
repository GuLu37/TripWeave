"""Agent、Tool 和接口异常的集中归一化与日志处理。"""

import logging
import re
from typing import Any

import httpx
from pydantic import ValidationError

from app.api.exception.exceptions import AppException

logger = logging.getLogger("app.error_handler")
_MAX_ERROR_TEXT = 300
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|token|password)\s*[:=]\s*[^,\s]+"
)


def record_error(
    error: Exception,
    *,
    component: str,
    source: str,
    operation: str,
    context: dict[str, object] | None = None,
    default_code: str = "UNEXPECTED_ERROR",
    default_message: str = "执行过程中发生未预期错误。",
    level: int = logging.WARNING,
) -> dict[str, object]:
    """归一化异常并输出一条可检索的结构化摘要。"""

    info = describe_error(
        error,
        default_code=default_code,
        default_message=default_message,
    )
    log_message = (
        "集中错误处理：component=%s source=%s operation=%s code=%s "
        "error_type=%s message=%s details=%s context=%s"
    )
    logger.log(
        level,
        log_message,
        component,
        source,
        operation,
        info["code"],
        info["error_type"],
        info["message"],
        info["details"],
        context or {},
        exc_info=not isinstance(
            error,
            (AppException, httpx.HTTPError, ValidationError),
        ),
    )
    return info


def describe_error(
    error: Exception,
    *,
    default_code: str,
    default_message: str,
) -> dict[str, object]:
    """将不同异常类型转换成安全、稳定的错误信息。"""

    if isinstance(error, AppException):
        return {
            "code": error.code,
            "message": error.message,
            "error_type": type(error).__name__,
            "details": error.details,
        }

    if isinstance(error, ValidationError):
        return {
            "code": "VALIDATION_ERROR",
            "message": "数据契约校验失败。",
            "error_type": type(error).__name__,
            "details": _validation_details(error),
        }

    if isinstance(error, httpx.HTTPStatusError):
        details: dict[str, object] = {
            "upstream_status_code": error.response.status_code,
        }
        response_text = error.response.text.strip()
        if response_text:
            details["upstream_message"] = _safe_error_text(response_text)
        return {
            "code": default_code,
            "message": default_message,
            "error_type": type(error).__name__,
            "details": details,
        }

    if isinstance(error, httpx.HTTPError):
        return {
            "code": default_code,
            "message": default_message,
            "error_type": type(error).__name__,
            "details": {
                "exception_message": _safe_error_text(str(error)),
            },
        }

    return {
        "code": default_code,
        "message": default_message,
        "error_type": type(error).__name__,
        "details": {
            "exception_message": _safe_error_text(str(error)),
        },
    }


def _validation_details(error: ValidationError) -> dict[str, object]:
    """只保留校验位置和类型，不把用户输入原文写入日志。"""

    fields: list[dict[str, object]] = []
    for item in error.errors():
        fields.append(
            {
                "location": ".".join(str(part) for part in item.get("loc", ())),
                "type": item.get("type"),
            }
        )
    return {
        "error_count": len(fields),
        "fields": fields[:20],
    }


def _safe_error_text(value: str) -> str:
    """截断并脱敏异常正文，避免密钥或超长网页内容进入日志。"""

    normalized = _SECRET_PATTERN.sub(r"\1=***", value)
    return normalized[:_MAX_ERROR_TEXT]


def error_response_details(error: Exception) -> Any:
    """提取可安全返回给 API 客户端的异常详情。"""

    return describe_error(
        error,
        default_code="INTERNAL_SERVER_ERROR",
        default_message="服务内部错误，请稍后再试。",
    )["details"]
