"""统一管理接口错误响应和异常处理器。"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.exception.exceptions import AppException, ErrorBody, ErrorResponse


def build_error_response(
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
) -> JSONResponse:
    """按统一结构构造接口错误响应。"""

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
    if isinstance(detail, dict):
        code = str(detail.get("code", "HTTP_ERROR"))
        message = str(detail.get("message", "请求处理失败。"))
        details = detail.get("details")
    else:
        code = "HTTP_ERROR"
        message = str(detail)
        details = None
    return build_error_response(exc.status_code, code, message, details)


async def handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """将请求参数校验失败转换为统一错误响应。"""

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

    return build_error_response(
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        message="服务内部错误，请稍后再试。",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """为 FastAPI 应用注册统一异常处理器。"""

    app.add_exception_handler(AppException, handle_app_exception)
    app.add_exception_handler(HTTPException, handle_http_exception)
    app.add_exception_handler(
        RequestValidationError,
        handle_request_validation_error,
    )
    app.add_exception_handler(Exception, handle_unexpected_error)
