"""
Helper functions for reading/writing global app settings (AppSetting table).

Usage:
    from backend.services.app_settings import get_setting, set_setting

    api_key = await get_setting("stt_speechmatics_api_key", default="")
    await set_setting("stt_language", "th")
"""
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import async_session
from backend.models.app_setting import AppSetting


async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a single app setting value by key. Returns default if not found."""
    async with async_session() as session:
        result = await session.execute(select(AppSetting).where(AppSetting.key == key))
        row = result.scalar_one_or_none()
        return row.value if row else default


async def get_setting_with_session(session: AsyncSession, key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a single app setting using an existing session (no commit)."""
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else default


async def set_setting(key: str, value: str) -> None:
    """Insert or update a single app setting value."""
    async with async_session() as session:
        result = await session.execute(select(AppSetting).where(AppSetting.key == key))
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            session.add(AppSetting(key=key, value=value))
        await session.commit()


async def set_settings_batch(updates: dict) -> None:
    """Upsert many app settings in ONE session + ONE commit.

    `set_setting()` opens a session and commits for every single key. A page
    like the STT settings form can update ~15 keys at once → ~15 separate
    sessions and commits (~15 × the per-commit fsync cost). Batching them cuts
    that to a single transaction. Keys already present are updated in place;
    missing keys are inserted.
    """
    if not updates:
        return
    async with async_session() as session:
        for key, value in updates.items():
            result = await session.execute(select(AppSetting).where(AppSetting.key == key))
            row = result.scalar_one_or_none()
            if row:
                row.value = value
            else:
                session.add(AppSetting(key=key, value=value))
        await session.commit()


async def get_settings_dict(prefix: str = "") -> dict[str, str]:
    """Read all app settings (optionally filtered by key prefix)."""
    async with async_session() as session:
        query = select(AppSetting)
        if prefix:
            query = query.where(AppSetting.key.like(f"{prefix}%"))
        result = await session.execute(query)
        rows = result.scalars().all()
        return {row.key: row.value for row in rows}