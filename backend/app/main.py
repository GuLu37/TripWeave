"""TripWeave 对话 Demo 的 FastAPI 应用入口。"""

import logging
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# 直接运行本文件时，将 backend 目录加入模块搜索路径。
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.router.chat import router as chat_router
from app.api.router.health import router as health_router
from app.api.exception.exception_handlers import register_exception_handlers
from app.core.logging import LOG_DIR, setup_logging
from app.core.settings import get_settings
from app.workflows.trip_conversation_graph import (
    close_trip_conversation_graph,
    initialize_trip_conversation_graph,
)

APP_VERSION = "0.1.0"
# 启动阶段读取一次全局配置，供 CORS 等应用级能力复用。
settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """在实际服务进程启动时初始化全局资源。"""

    # 使用生命周期而非模块顶层初始化，避免热重载父子进程重复创建日志处理器。
    setup_logging()
    # 第二步：打开 SQLite Checkpointer，确保服务重启后可按 conversation_id 恢复图状态。
    await initialize_trip_conversation_graph()
    logger.info("TripWeave API 已启动：version=%s", app.version)
    yield
    # 第三步：服务停止时关闭检查点连接，避免 SQLite 文件句柄泄漏。
    await close_trip_conversation_graph()
    logger.info("TripWeave API 正在停止。")


# 将生命周期交给 FastAPI 管理，保证启动和关闭资源的顺序一致。
app = FastAPI(
    title="TripWeave API",
    version=APP_VERSION,
    lifespan=lifespan,
)
# CORS 来源完全由 .env 提供，避免将部署地址硬编码在应用代码中。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录基础请求访问日志。"""

    # 第一步：为每个请求生成短标识，用于关联入口、异常和完成日志。
    request_id = uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    # 第二步：从请求进入开始计时，覆盖路由处理、模型调用和响应生成的完整耗时。
    start_time = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start_time) * 1000
    # 第三步：将请求标识回传给前端，并统一记录访问结果和慢请求定位信息。
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "请求完成：request_id=%s method=%s path=%s status_code=%s duration_ms=%.2f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# 按模块注册接口，main.py 只负责应用组装，不承载具体业务实现。
app.include_router(health_router)
app.include_router(chat_router)
# 在所有路由之后注册统一异常处理，确保业务异常以一致结构返回。
register_exception_handlers(app)


if __name__ == "__main__":
    import uvicorn

    # 使用模块路径启动，确保开启热重载后的子进程可正确导入应用。
    # 排除日志目录，避免日志写入触发无休止的热重载。
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[str(Path(__file__).resolve().parents[1])],
        reload_excludes=[str(LOG_DIR)],
    )
