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
        retryable: bool = False,
        fallbackable: bool = False,
    ) -> None:
        """保存业务错误的 HTTP 状态码、错误码和中文提示。"""

        # retryable 控制同一供应商重试，fallbackable 控制是否切换至下一个供应商。
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable
        self.fallbackable = fallbackable
        super().__init__(message)


class ModelException(AppException):
    """大模型调用相关业务异常。"""

    @classmethod
    def config_missing(
        cls,
        provider: str,
        missing_settings: str,
    ) -> "ModelException":
        """创建模型配置缺失异常。"""

        # 配置错误不会因重试而恢复，调用方会跳过当前供应商。
        return cls(
            status_code=503,
            code="LLM_CONFIG_MISSING",
            message=f"backend/.env 缺少 {provider} 配置：{missing_settings}。",
            fallbackable=True,
        )

    @classmethod
    def provider_unsupported(cls, provider: str) -> "ModelException":
        """创建不支持的大模型供应商异常。"""

        # 未注册的供应商不发起网络请求，直接记录后继续检查下一个备用项。
        return cls(
            status_code=503,
            code="LLM_PROVIDER_UNSUPPORTED",
            message=f"不支持的 LLM_PROVIDER：{provider}。",
        )

    @classmethod
    def api_rejected(
        cls,
        provider: str,
        details: object | None = None,
        retryable: bool = False,
    ) -> "ModelException":
        """创建模型服务拒绝请求异常。"""

        # 仅 429 和 5xx 会由调用层传入 retryable=True。
        return cls(
            status_code=502,
            code="LLM_API_REJECTED",
            message=f"{provider} 拒绝了本次请求。",
            details=details,
            retryable=retryable,
            fallbackable=retryable,
        )

    @classmethod
    def api_unreachable(cls, provider: str) -> "ModelException":
        """创建模型服务不可达异常。"""

        # 网络连接和超时通常是短暂故障，允许当前供应商重试后再切换。
        return cls(
            status_code=502,
            code="LLM_API_UNREACHABLE",
            message=f"暂时无法连接 {provider} API。",
            retryable=True,
            fallbackable=True,
        )

    @classmethod
    def bad_response(
        cls,
        provider: str,
        *,
        retryable: bool = False,
    ) -> "ModelException":
        """创建模型响应格式异常。"""

        # 调用方可对领域契约漂移启用当前供应商重试，协议级异常仍默认直接切换备用。
        return cls(
            status_code=502,
            code="LLM_API_BAD_RESPONSE",
            message=f"{provider} 返回了无法识别的响应。",
            retryable=retryable,
            fallbackable=True,
        )

    @classmethod
    def fallback_exhausted(
        cls,
        failures: list[dict[str, object]],
    ) -> "ModelException":
        """创建所有可用供应商均失败的异常。"""

        # details 只包含供应商、尝试次数和错误码，不包含密钥或上游原始响应。
        return cls(
            status_code=502,
            code="LLM_FALLBACK_EXHAUSTED",
            message="当前没有候选大模型完成本次调用，请检查主模型响应与备用模型配置。",
            details=failures,
        )


class AmapException(AppException):
    """高德地图 Web 服务调用相关业务异常。"""

    @classmethod
    def config_missing(cls) -> "AmapException":
        """创建高德 Web 服务 Key 缺失异常。"""

        # 第一步：配置缺失无需发起网络请求，避免将空 Key 传给第三方服务。
        return cls(
            status_code=503,
            code="AMAP_CONFIG_MISSING",
            message="backend/.env 缺少 AMAP_WEB_SERVICE_KEY 配置。",
        )

    @classmethod
    def invalid_parameter(cls, parameter_name: str) -> "AmapException":
        """创建地图工具参数格式异常。"""

        # 第一步：在调用高德接口前拦截不符合官方参数格式的输入。
        return cls(
            status_code=422,
            code="AMAP_PARAMETER_INVALID",
            message=f"高德地图参数 {parameter_name} 格式不正确。",
            details={"parameter": parameter_name},
        )

    @classmethod
    def api_rejected(
        cls,
        status_code: int,
    ) -> "AmapException":
        """创建高德 HTTP 错误响应异常。"""

        # 第一步：限流和服务端错误通常可恢复，供后续 ToolExecutor 决定是否重试。
        retryable = status_code == 429 or status_code >= 500
        return cls(
            status_code=502,
            code="AMAP_API_REJECTED",
            message="高德地图服务拒绝了本次请求。",
            details={"upstream_status_code": status_code},
            retryable=retryable,
        )

    @classmethod
    def api_unreachable(cls) -> "AmapException":
        """创建高德服务不可达异常。"""

        # 第一步：网络连接、DNS 与超时通常是短暂故障，允许上层按策略重试。
        return cls(
            status_code=502,
            code="AMAP_API_UNREACHABLE",
            message="暂时无法连接高德地图服务。",
            retryable=True,
        )

    @classmethod
    def bad_response(cls) -> "AmapException":
        """创建高德响应协议异常。"""

        # 第一步：非 JSON 或非对象响应不能交给规划 Agent 继续解释。
        return cls(
            status_code=502,
            code="AMAP_API_BAD_RESPONSE",
            message="高德地图服务返回了无法识别的响应。",
            retryable=True,
        )

    @classmethod
    def business_rejected(
        cls,
        info: str | None,
        infocode: str | int | None,
    ) -> "AmapException":
        """创建高德业务状态失败异常。"""

        # 第一步：只返回官方错误摘要和错误码，不携带请求地址、Key 等敏感参数。
        return cls(
            status_code=502,
            code="AMAP_BUSINESS_REJECTED",
            message="高德地图服务未能完成本次查询。",
            details={
                "amap_info": info,
                "amap_infocode": str(infocode) if infocode is not None else None,
            },
        )


class QWeatherException(AppException):
    """和风天气 API 调用相关业务异常。"""

    @classmethod
    def config_missing(
        cls,
        missing_settings: str,
    ) -> "QWeatherException":
        """创建和风天气配置缺失异常。"""

        # 第一步：配置不完整时不发送请求，避免空凭据或公共地址导致无意义失败。
        return cls(
            status_code=503,
            code="QWEATHER_CONFIG_MISSING",
            message=f"backend/.env 缺少和风天气配置：{missing_settings}。",
        )

    @classmethod
    def invalid_parameter(cls, parameter_name: str) -> "QWeatherException":
        """创建天气工具参数格式异常。"""

        # 第一步：将本地输入校验失败转成统一业务异常，供 Agent 明确纠正入参。
        return cls(
            status_code=422,
            code="QWEATHER_PARAMETER_INVALID",
            message=f"和风天气参数 {parameter_name} 格式不正确。",
            details={"parameter": parameter_name},
        )

    @classmethod
    def api_rejected(
        cls,
        status_code: int,
    ) -> "QWeatherException":
        """创建和风天气 HTTP 错误响应异常。"""

        # 第一步：限流与服务端错误可由后续 ToolExecutor 重试，其他状态码直接交给上层处理。
        retryable = status_code == 429 or status_code >= 500
        return cls(
            status_code=502,
            code="QWEATHER_API_REJECTED",
            message="和风天气服务拒绝了本次请求。",
            details={"upstream_status_code": status_code},
            retryable=retryable,
        )

    @classmethod
    def api_unreachable(cls) -> "QWeatherException":
        """创建和风天气服务不可达异常。"""

        # 第一步：网络、连接和超时故障通常可恢复，允许调用编排层按策略重试。
        return cls(
            status_code=502,
            code="QWEATHER_API_UNREACHABLE",
            message="暂时无法连接和风天气服务。",
            retryable=True,
        )

    @classmethod
    def bad_response(cls) -> "QWeatherException":
        """创建和风天气响应协议异常。"""

        # 第一步：成功响应缺少 metadata 时不可交给 Agent 解释，避免错误对象污染规划结果。
        return cls(
            status_code=502,
            code="QWEATHER_API_BAD_RESPONSE",
            message="和风天气服务返回了无法识别的响应。",
            retryable=True,
        )

    @classmethod
    def business_rejected(
        cls,
        provider_code: str | int | None,
    ) -> "QWeatherException":
        """创建和风天气业务状态失败异常。"""

        # 第一步：仅保留第三方状态码，不暴露请求坐标、API Host 或 API Key。
        return cls(
            status_code=502,
            code="QWEATHER_BUSINESS_REJECTED",
            message="和风天气服务未能完成本次查询。",
            details={
                "qweather_code": str(provider_code)
                if provider_code is not None
                else None,
            },
        )
