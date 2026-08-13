"""从环境变量读取应用配置。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    """提供对话接口使用的模型连接配置。"""

    deepseek_api_key: str = Field(min_length=1)
    deepseek_base_url: str = Field(min_length=1)
    deepseek_model: str = Field(min_length=1)

    model_config = SettingsConfigDict(
        env_file=ENV_FILE_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """返回当前应用复用的配置实例。"""

    return Settings()
