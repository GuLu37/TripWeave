"""Unsplash 图片搜索服务。"""

import logging

import httpx

from app.core.settings import get_settings

logger = logging.getLogger(__name__)


class UnsplashService:
    """异步搜索 Unsplash 图片，并只返回前端需要的字段。"""

    def __init__(
        self,
        access_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        settings = get_settings()
        self._access_key = (
            access_key if access_key is not None else settings.unsplash_access_key
        )
        self._base_url = (
            base_url if base_url is not None else settings.unsplash_base_url
        ).rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def search_photos(
        self,
        query: str,
        *,
        per_page: int = 3,
    ) -> list[dict[str, object]]:
        """搜索图片；未配置或请求失败时返回空列表。"""

        normalized_query = query.strip()
        if not normalized_query or not self._access_key:
            logger.info(
                "Unsplash 图片查询跳过：configured=%s query_present=%s",
                bool(self._access_key),
                bool(normalized_query),
            )
            return []
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.get(
                    "/search/photos",
                    params={
                        "query": normalized_query,
                        "per_page": max(1, min(per_page, 10)),
                        "client_id": self._access_key,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning(
                "Unsplash 图片查询失败：error_type=%s query_chars=%s",
                type(error).__name__,
                len(normalized_query),
            )
            return []

        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(results, list):
            return []
        photos: list[dict[str, object]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            urls = item.get("urls")
            links = item.get("links")
            user = item.get("user")
            if not isinstance(urls, dict):
                continue
            url = urls.get("regular")
            if not isinstance(url, str) or not url.strip():
                continue
            photos.append(
                {
                    "id": item.get("id"),
                    "url": url,
                    "thumb_url": urls.get("thumb"),
                    "alt_text": item.get("alt_description")
                    or item.get("description"),
                    "photographer": (
                        user.get("name")
                        if isinstance(user, dict)
                        else None
                    ),
                    "source_url": (
                        links.get("html")
                        if isinstance(links, dict)
                        else None
                    ),
                }
            )
        logger.info(
            "Unsplash 图片查询完成：query_chars=%s result_count=%s",
            len(normalized_query),
            len(photos),
        )
        return photos
