from __future__ import annotations

import structlog
from fastapi import FastAPI

from src.core.config import get_settings
from src.rag.retriever import build_retriever
from src.services.order_store import get_order_store

logger = structlog.get_logger()


def refresh_app_retriever(app: FastAPI) -> None:
    """Rebuild BM25 + Chroma (synced) and hot-swap in-memory retriever."""
    settings = get_settings()
    retriever = build_retriever(settings)
    app.state.retriever = retriever
    app.state.agent_service.retriever = retriever
    logger.info("retriever_refreshed", retriever_type=settings.retriever_type)


def refresh_app_order_store(app: FastAPI | None = None, *, full_reload: bool = True) -> dict:
    """热更新订单仓库。full_reload=True 时从 data/orders 全量重载。"""
    settings = get_settings()
    store = get_order_store()
    if full_reload:
        store.load_directory(settings.orders_dir)
    if app is not None:
        app.state.order_store = store
    stats = store.stats()
    logger.info("order_store_refreshed", **stats)
    return stats
