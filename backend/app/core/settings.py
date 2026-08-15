"""从环境变量读取应用配置。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE_PATH = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """提供对话接口使用的模型连接配置。"""

    # 仅当前主供应商和实际参与兜底的供应商需要填写对应三项配置。
    llm_provider: str = Field(default="deepseek", min_length=1)
    deepseek_api_key: str | None = None
    deepseek_base_url: str | None = None
    deepseek_model: str | None = None
    # 审核总结使用独立 Pro 模型，避免入口与规划模型配置变化影响审批质量。
    deepseek_review_model: str = Field(default="deepseek-v4-pro", min_length=1)
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str | None = None
    proxy_api_key: str | None = None
    proxy_base_url: str | None = None
    proxy_model: str | None = None
    llm_fallback_providers: str = ""
    llm_max_retries: int = Field(default=3, ge=1, le=10)
    # 调试开关默认关闭，避免正常日志写入对话原文或模型原始响应。
    llm_debug_log_raw_output: bool = False
    llm_debug_log_raw_request: bool = False
    # 仅供后端调用高德地图 Web 服务，不能与前端 Web JS Key 混用。
    amap_web_service_key: str | None = None
    amap_web_service_base_url: str = Field(
        default="https://restapi.amap.com",
        min_length=1,
    )
    # 和风天气要求使用控制台分配的专属 API Host，不能使用即将停止服务的公共域名。
    qweather_api_host: str | None = None
    # API Key 仅由后端通过 X-QW-Api-Key 请求头发送，禁止暴露给浏览器。
    qweather_api_key: str | None = None
    # 浏览器查询由 MCP Tool 执行，本地不直接抓取网站。
    browser_search_provider: Literal["mcp"] = "mcp"
    browser_search_timeout_seconds: float = Field(default=90, gt=0, le=300)
    mcp_server_url: str | None = None
    mcp_auth_token: str | None = None
    mcp_protocol_version: str = Field(default="2025-06-18", min_length=1)
    mcp_max_steps: int = Field(default=12, ge=1, le=30)
    mcp_max_observation_chars: int = Field(default=12_000, ge=1_000, le=50_000)
    cors_allow_origins: str = Field(min_length=1)
    log_max_bytes: int = Field(default=1_048_576, ge=1)
    log_backup_count: int = Field(default=5, ge=1)

    model_config = SettingsConfigDict(
        env_file=ENV_FILE_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        """将 .env 中的 CORS 地址配置拆分为列表。"""

        # 去除空白和空项，避免配置中的多余逗号导致无效来源进入中间件。
        return [
            origin.strip()
            for origin in self.cors_allow_origins.split(",")
            if origin.strip()
        ]

    @property
    def fallback_providers(self) -> list[str]:
        """将 .env 中的备用供应商配置拆分为列表。"""

        # 统一转小写，使 .env 中的供应商名称与客户端注册表稳定匹配。
        return [
            provider.strip().lower()
            for provider in self.llm_fallback_providers.split(",")
            if provider.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    """返回当前应用复用的配置实例。"""

    # 配置在进程生命周期内保持一致，避免每次请求重复读取 .env。
    return Settings()
