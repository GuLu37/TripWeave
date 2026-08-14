"""统一管理接口错误响应和异常处理器。"""

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.exception.exceptions import AppException, ErrorBody, ErrorResponse

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    """读取请求中间件生成的追踪标识。"""

    # 第一步：优先读取中间件已写入的请求标识，异常发生在初始化阶段时使用占位符。
    return str(getattr(request.state, "request_id", "-"))


def build_error_response(
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
) -> JSONResponse:
    """按统一结构构造接口错误响应。"""

    # 先用 Pydantic 约束错误体，再转换为 JSON，避免各处理器返回不同字段结构。
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, details=details)
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(),
    )


async def handle_app_exception(
    request: Request,
    exc: AppException,
) -> JSONResponse:
    """将自定义业务异常转换为统一错误响应。"""

    # 业务异常预期可控，记录必要请求上下文但不记录密钥和完整对话内容。
    logger.warning(
        "业务异常：request_id=%s method=%s path=%s code=%s message=%s",
        _request_id(request),
        request.method,
        request.url.path,
        exc.code,
        exc.message,
    )
    return build_error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def handle_http_exception(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """将 FastAPI HTTP 异常转换为统一错误响应。"""

    detail = exc.detail
    # 支持业务代码传入结构化 detail，也兼容 FastAPI 默认的字符串 detail。
    if isinstance(detail, dict):
        code = str(detail.get("code", "HTTP_ERROR"))
        message = str(detail.get("message", "请求处理失败。"))
        details = detail.get("details")
    else:
        code = "HTTP_ERROR"
        message = str(detail)
        details = None
    logger.warning(
        "HTTP 异常：request_id=%s method=%s path=%s status_code=%s message=%s",
        _request_id(request),
        request.method,
        request.url.path,
        exc.status_code,
        message,
    )
    return build_error_response(exc.status_code, code, message, details)


async def handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """将请求参数校验失败转换为统一错误响应。"""

    # 参数校验错误包含字段位置等信息，可安全返回给前端用于提示输入问题。
    logger.warning(
        "请求参数校验失败：request_id=%s method=%s path=%s errors=%s",
        _request_id(request),
        request.method,
        request.url.path,
        exc.errors(),
    )
    return build_error_response(
        status_code=422,
        code="REQUEST_VALIDATION_ERROR",
        message="请求参数格式不正确。",
        details=exc.errors(),
    )


async def handle_unexpected_error(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """将未预期异常转换为统一错误响应，避免泄露内部实现。"""

    # 日志保留完整堆栈供排查，响应只暴露通用中文提示。
    logger.exception(
        "未预期异常：request_id=%s method=%s path=%s",
        _request_id(request),
        request.method,
        request.url.path,
    )
    return build_error_response(
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        message="服务内部错误，请稍后再试。",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """为 FastAPI 应用注册统一异常处理器。"""

    # 自定义业务异常优先注册，其余框架异常再按类别进行统一转换。
    app.add_exception_handler(AppException, handle_app_exception)
    app.add_exception_handler(HTTPException, handle_http_exception)
    app.add_exception_handler(
        RequestValidationError,
        handle_request_validation_error,
    )
    app.add_exception_handler(Exception, handle_unexpected_error)
