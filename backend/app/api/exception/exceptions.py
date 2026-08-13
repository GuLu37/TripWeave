"""自定义异常类和统一错误响应结构。"""

from typing import Any

from pydantic import BaseModel


class ErrorBody(BaseModel):
    """统一错误响应中的错误详情。"""

    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    """所有接口错误统一返回的数据结构。"""

    error: ErrorBody


class AppException(Exception):
    """可被统一异常处理器识别的业务异常。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: object | None = None,
    ) -> None:
        """保存业务错误的 HTTP 状态码、错误码和中文提示。"""

        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)
