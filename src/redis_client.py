from __future__ import annotations

import redis.asyncio as aioredis

from src.core.config import get_settings

_redis_client: aioredis.Redis | None = None
_redis_failed: bool = False


async def get_redis() -> aioredis.Redis | None:
    """获取 Redis；旧版 Windows Redis 不支持 RESP3 HELLO，强制 protocol=2。"""
    global _redis_client, _redis_failed
    if _redis_failed and _redis_client is None:
        # 允许稍后重试：进程内短暂失败后下次再连
        pass
    settings = get_settings()
    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                protocol=2,  # RESP2：兼容不支持 HELLO 的 Redis
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await _redis_client.ping()
            _redis_failed = False
        except Exception:
            if _redis_client is not None:
                try:
                    await _redis_client.aclose()
                except Exception:
                    pass
            _redis_client = None
            _redis_failed = True
    return _redis_client


async def close_redis() -> None:
    global _redis_client, _redis_failed
    if _redis_client:
        try:
            await _redis_client.aclose()
        except Exception:
            try:
                await _redis_client.close()
            except Exception:
                pass
        _redis_client = None
    _redis_failed = False
