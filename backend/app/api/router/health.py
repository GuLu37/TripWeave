"""健康检查路由。"""

from fastapi import APIRouter, Request

from app.schemas import HealthResponse

router = APIRouter(tags=["健康检查"])


@router.get("/health", response_model=HealthResponse)
@router.get("/api/v1/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """返回 Demo 后端的可用状态。"""

    # 版本号从应用实例读取，避免健康检查路由与 main.py 分别维护版本常量。
    return HealthResponse(
        service="TripWeave API",
        status="ok",
        version=request.app.version,
    )
