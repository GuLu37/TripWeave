"""TripWeave 对话 Demo 的 FastAPI 应用入口。"""

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 直接运行本文件时，将 backend 目录加入模块搜索路径。
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.router.chat import router as chat_router
from app.api.router.health import router as health_router
from app.api.exception.exception_handlers import register_exception_handlers

APP_VERSION = "0.1.0"
app = FastAPI(title="TripWeave API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health_router)
app.include_router(chat_router)
register_exception_handlers(app)


if __name__ == "__main__":
    import uvicorn

    # 使用模块路径启动，确保开启热重载后的子进程可正确导入应用。
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[str(Path(__file__).resolve().parents[1])],
    )
