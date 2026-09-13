"""数据库连接：打开 PostgreSQL「档案室」的钥匙。"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import get_settings

settings = get_settings()
# 根据 .env 里的 DATABASE_URL 创建异步引擎
engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
# 会话工厂：每次业务操作借一个数据库会话
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入用：请求期间提供数据库会话，结束时提交或回滚。"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
