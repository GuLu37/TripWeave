"""应用日志初始化配置。"""

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.settings import get_settings

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def _configure_third_party_loggers() -> None:
    """限制可能打印完整请求 URL 的第三方日志级别。"""

    # 第一步：httpx 的 INFO 日志会输出完整查询参数，高德 Key 位于查询参数中，必须禁止写入日志。
    logging.getLogger("httpx").setLevel(logging.WARNING)


def setup_logging() -> None:
    """初始化控制台日志和按大小滚动的文件日志。"""

    settings = get_settings()
    # 日志目录不存在时首次启动自动创建；.gitkeep 仅用于保留空目录。
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 每次实际服务启动生成独立文件，便于按启动批次回溯问题。
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{timestamp}.log"

    formatter = logging.Formatter(LOG_FORMAT)
    # 单个文件超过配置大小后自动生成 .1、.2 等滚动文件，限制单次运行日志膨胀。
    file_handler = RotatingFileHandler(
        filename=log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # 根日志统一输出到文件与控制台，避免不同模块各自配置造成重复打印。
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # 请求访问日志已由 main.py 的中间件统一记录，关闭 Uvicorn 的重复访问日志。
    logging.getLogger("uvicorn.access").disabled = True
    # 第四步：保留业务工具自己的安全摘要，关闭第三方库可能泄露认证参数的访问日志。
    _configure_third_party_loggers()
    # 文件变化提示不属于业务日志，且热重载时会产生大量噪声。
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("watchfiles.main").setLevel(logging.WARNING)
    logging.getLogger(__name__).info("日志系统已初始化：%s", log_path)
