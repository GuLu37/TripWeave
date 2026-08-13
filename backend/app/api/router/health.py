"""健康检查路由。"""

from fastapi import APIRouter, Request

from app.schemas import HealthResponse

router = APIRouter(tags=["健康检查"])


@router.get("/health", response_model=HealthResponse)
@router.get("/api/v1/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """返回 Demo 后端的可用状态。"""

    return HealthResponse(
        service="TripWeave API",
        status="ok",
        version=request.app.version,
    )
