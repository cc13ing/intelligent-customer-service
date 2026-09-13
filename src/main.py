"""智能客服 API 入口：创建应用、启动资源、挂载前端。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src.api.routes import admin, admin_auth, agent_desk, auth, chat, feedback, health, inference, knowledge, order_ops, orders
from src.core.config import get_settings
from src.core.rate_limit import limiter
from src.core.security import check_api_key, validate_settings_on_startup
from src.core.tracing import setup_tracing
from src.db import engine
from src.models import Base
from src.rag.retriever import build_retriever
from src.rag.embeddings import release_ml_resources
from src.redis_client import close_redis, get_redis
from src.services.agent_service import AgentService
from src.services.llm.kimi_client import KimiClient
from src.services.llm.local_qwen import LocalQwenService
from src.services.session_service import SessionService
from src.services.order_store import init_order_store
from src.core.app_state import refresh_app_order_store

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时准备资源，关闭时释放资源。

    启动顺序：
    1. 读配置
    2. 建检索器（知识库）
    3. 接通 LLM「大脑」
    4. 可选加载本地 Qwen
    5. 连接 Redis，创建 Agent / 会话服务
    6. PostgreSQL 建表（打开档案室）
    yield 之后表示开始对外服务；退出时做清理。
    """
    settings = get_settings()
    validate_settings_on_startup(settings)
    logger.info("starting_app", rag_backend=settings.rag_backend, env=settings.env)

    # 准备知识库检索器
    retriever = build_retriever(settings)
    # 接通 LLM API（名字叫 kimi，实际可接 DeepSeek 等）
    kimi = KimiClient(settings)

    qwen = None
    if settings.rag_backend == "local":
        if settings.qwen_inference_url:
            from src.services.llm.remote_qwen import RemoteQwenClient

            qwen = RemoteQwenClient(settings)
            logger.info("qwen_remote_mode", url=settings.qwen_inference_url)
        else:
            try:
                qwen = LocalQwenService.get_instance(settings)
                qwen.load()
                await qwen.start_worker()
                app.state.qwen_service = qwen
                logger.info("qwen_model_loaded")
            except Exception as e:
                logger.warning("qwen_load_failed", error=str(e))

    redis = await get_redis()
    # 雇好「项目经理」Agent，以及会话服务
    app.state.agent_service = AgentService(kimi, retriever, qwen)
    app.state.session_service = SessionService(redis)
    app.state.retriever = retriever

    # 加载订单 CSV 仓库（支持管理后台热更新）
    init_order_store(settings.orders_dir)
    refresh_app_order_store(app, full_reload=False)

    # 按 models 定义在 PostgreSQL 中建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 从数据库回灌订单（CSV 之外的持久化数据）
    try:
        from src.db import async_session_factory
        from src.services.order_persist import load_orders_into_store
        from src.services.user_service import get_user_by_email, register_user

        async with async_session_factory() as db:
            await load_orders_into_store(db)
            # 开发环境种子演示账号（可改密）
            if settings.env == "development":
                demo = await get_user_by_email(db, "demo@gulf.ae")
                if not demo or not demo.password_hash:
                    await register_user(
                        db, "demo@gulf.ae", "demo123", name="Demo"
                    )
                    await db.commit()
                    logger.info("demo_user_seeded", email="demo@gulf.ae")
    except Exception as exc:
        logger.warning("orders_db_load_failed", error=str(exc))

    yield  # 此处之后应用开始处理请求

    # ---------- 关闭清理 ----------
    if qwen and hasattr(qwen, "shutdown"):
        await qwen.shutdown()
    elif qwen and hasattr(qwen, "unload"):
        qwen.unload()
    await close_redis()
    await engine.dispose()
    release_ml_resources()
    logger.info("app_shutdown")


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用：路由、中间件、静态前端页面。"""
    settings = get_settings()
    app = FastAPI(
        title="智能客服 API",
        description="Production intelligent customer service system",
        version="1.0.0",
        lifespan=lifespan,
    )

    if setup_tracing(app):
        logger.info("otel_tracing_enabled", service=settings.otel_service_name)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # #region debug-point B:request-validation
    @app.exception_handler(RequestValidationError)
    async def _debug_request_validation_handler(request: Request, exc: RequestValidationError):
        import json
        import urllib.request

        _p = ".dbg/login-chat-timeout.env"
        _u = "http://127.0.0.1:7778/event"
        _s = "login-chat-timeout"
        try:
            with open(_p, encoding="utf-8") as _f:
                _c = _f.read().splitlines()
                _u = next((line.split("=", 1)[1] for line in _c if line.startswith("DEBUG_SERVER_URL=")), _u)
                _s = next((line.split("=", 1)[1] for line in _c if line.startswith("DEBUG_SESSION_ID=")), _s)
        except Exception:
            pass

        try:
            _data = {
                "path": str(request.url.path),
                "method": request.method,
                "content_type": request.headers.get("content-type"),
                "content_length": request.headers.get("content-length"),
                "origin": request.headers.get("origin"),
            }
            urllib.request.urlopen(
                urllib.request.Request(
                    _u,
                    data=json.dumps(
                        {
                            "sessionId": _s,
                            "runId": "pre",
                            "hypothesisId": "B",
                            "location": "src/main.py:request_validation",
                            "msg": "[DEBUG] request_validation_error",
                            "data": _data,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=2,
            ).read()
        except Exception:
            pass

        return await request_validation_exception_handler(request, exc)

    # #endregion

    @app.middleware("http")
    async def metrics_auth_middleware(request: Request, call_next):
        """拦截 /metrics：若开启鉴权，则必须带正确 API Key。"""
        if request.url.path == "/metrics" and settings.metrics_require_auth:
            api_key = request.headers.get("X-API-Key")
            if not check_api_key(api_key, settings):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key"},
                )
        return await call_next(request)

    # 允许浏览器跨域访问（前后端不同端口时需要）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册各业务路由（接待员窗口）
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(agent_desk.router)
    app.include_router(feedback.router)
    app.include_router(orders.router)
    app.include_router(order_ops.router)
    app.include_router(knowledge.router)
    app.include_router(admin.router)
    app.include_router(admin_auth.router)
    app.include_router(inference.router)

    @app.get("/config.js")
    async def frontend_config_js():
        """前端运行时配置。故意不注入管理端 API Key，避免密钥进浏览器。"""
        payload = {
            "apiKey": "",
            "chatPublic": bool(settings.chat_public_access),
            "wsPath": "/api/v1/ws/chat",
            "env": settings.env,
        }
        return Response(
            content=f"window.__KEFU_CONFIG__ = {json.dumps(payload)};",
            media_type="application/javascript",
        )

    @app.get("/metrics")
    async def metrics_endpoint():
        """Prometheus 监控指标接口。"""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    try:
        from pathlib import Path

        from fastapi.responses import FileResponse

        # 挂载管理后台（React SPA：子路由刷新也回 index.html）
        admin_dir = Path(__file__).parent.parent / "frontend-admin" / "dist"
        if admin_dir.exists():

            @app.get("/admin")
            @app.get("/admin/")
            async def admin_root():
                return FileResponse(admin_dir / "index.html")

            @app.get("/admin/assets/{asset_path:path}")
            async def admin_assets(asset_path: str):
                target = (admin_dir / "assets" / asset_path).resolve()
                if not str(target).startswith(str((admin_dir / "assets").resolve())):
                    return JSONResponse({"detail": "Not Found"}, status_code=404)
                if not target.is_file():
                    return JSONResponse({"detail": "Not Found"}, status_code=404)
                return FileResponse(target)

            @app.get("/admin/{spa_path:path}")
            async def admin_spa(spa_path: str):
                # 已知静态文件直接返回；其余交给前端路由
                candidate = (admin_dir / spa_path).resolve()
                if str(candidate).startswith(str(admin_dir.resolve())) and candidate.is_file():
                    return FileResponse(candidate)
                return FileResponse(admin_dir / "index.html")

        # 挂载聊天网页（注意：要放在最后，否则会挡住 API）
        frontend_dir = Path(__file__).parent.parent / "frontend"
        if frontend_dir.exists():
            app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    except Exception:
        pass

    return app


# 模块导入时就创建好 app，供 uvicorn 使用：src.main:app
app = create_app()


def run():
    """命令行启动入口：用 uvicorn 监听配置中的 host/port。"""
    import os

    import uvicorn

    settings = get_settings()
    try:
        uvicorn.run(
            "src.main:app",
            host=settings.api_host,
            port=settings.api_port,
            reload=False,
            timeout_graceful_shutdown=5,
        )
    finally:
        # Windows 上部分 ML 线程可能阻拦正常退出，强制收尾
        release_ml_resources()
        os._exit(0)


if __name__ == "__main__":
    run()
