from __future__ import annotations

import uuid

from passlib.context import CryptContext
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.user import User

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return _pwd.verify(password, password_hash)
    except Exception:
        return False


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def get_or_create_user(
    db: AsyncSession,
    email: str,
    *,
    name: str | None = None,
    oauth_provider: str | None = None,
    oauth_sub: str | None = None,
) -> User:
    email = email.lower().strip()
    user = await get_user_by_email(db, email)
    if user:
        if name and not user.name:
            user.name = name
        return user

    user = User(
        id=str(uuid.uuid4()),
        email=email,
        name=name,
        oauth_provider=oauth_provider,
        oauth_sub=oauth_sub,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def register_user(
    db: AsyncSession,
    email: str,
    password: str,
    *,
    name: str | None = None,
) -> User:
    email = email.lower().strip()
    existing = await get_user_by_email(db, email)
    if existing:
        if existing.password_hash:
            raise ValueError("该邮箱已注册")
        # 旧无密账号：补设密码
        existing.password_hash = hash_password(password)
        if name:
            existing.name = name
        existing.is_active = True
        await db.flush()
        return existing

    user = User(
        id=str(uuid.uuid4()),
        email=email,
        name=name,
        password_hash=hash_password(password),
        oauth_provider="email",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    user = await get_user_by_email(db, email.lower().strip())
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def change_password(
    db: AsyncSession,
    user: User,
    old_password: str,
    new_password: str,
) -> None:
    if not verify_password(old_password, user.password_hash):
        raise ValueError("原密码不正确")
    if len(new_password) < 6:
        raise ValueError("新密码至少 6 位")
    user.password_hash = hash_password(new_password)
    await db.flush()


async def list_users(
    db: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    active_only: bool = False,
) -> tuple[list[User], int]:
    filters = []
    if active_only:
        filters.append(User.is_active.is_(True))
    count_q = select(func.count()).select_from(User)
    list_q = select(User).order_by(User.created_at.desc())
    if filters:
        count_q = count_q.where(*filters)
        list_q = list_q.where(*filters)
    total = int(await db.scalar(count_q) or 0)
    rows = list((await db.scalars(list_q.offset(offset).limit(limit))).all())
    return rows, total


async def deactivate_user(db: AsyncSession, user_id: str) -> User | None:
    user = await get_user_by_id(db, user_id)
    if not user:
        return None
    user.is_active = False
    await db.flush()
    return user
