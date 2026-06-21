import time
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db
from backend.auth.jwt import decode_token
from backend.models.user import User, UserRole


security = HTTPBearer()

# ---------------------------------------------------------------------------
# Lightweight in-process TTL cache for auth user lookups.
#
# Why: every authenticated API request runs `get_current_user`, which issues
# a `SELECT * FROM users WHERE id = ?` query. On a page like Recordings /
# Settings that fires many requests at once, this turns into one DB round-trip
# per request even though the user row almost never changes mid-session.
#
# The cache holds the (detached) User attributes keyed by user_id for a short
# TTL (default 15s). It is invalidated explicitly on any user mutation
# (toggle-active, delete, change-password, timezone update) via
# `invalidate_user_cache(user_id)`, so security-sensitive changes take effect
# immediately. A short TTL also bounds staleness as a safety net.
# ---------------------------------------------------------------------------
_USER_CACHE_TTL_SECONDS = 15.0  # bound staleness; explicit invalidation is primary
_user_cache: dict[str, tuple[float, dict]] = {}


def invalidate_user_cache(user_id: Optional[str] = None) -> None:
    """Drop cached user info so the next request re-reads from the DB.

    Call after any mutation that affects auth decisions (is_active, role,
    password hash, timezone, deletion). If `user_id` is None, clears all.
    """
    if user_id is None:
        _user_cache.clear()
    else:
        _user_cache.pop(str(user_id), None)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Get the current authenticated user from JWT token.

    Uses a short TTL cache for the user lookup to avoid a DB round-trip on
    every authenticated request. Mutations call `invalidate_user_cache()`.
    """
    token = credentials.credentials
    payload = decode_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # --- Try the in-process TTL cache first ---
    now = time.monotonic()
    cached = _user_cache.get(str(user_id))
    if cached is not None:
        expires_at, attrs = cached
        if now < expires_at:
            # Reconstruct a detached User from cached attrs so downstream code
            # that inspects attributes keeps working without a DB session.
            user = User(**attrs)
            if not user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User account is disabled",
                )
            return user
        else:
            # expired → fall through to DB read and refresh cache
            _user_cache.pop(str(user_id), None)

    # --- Cache miss / expired: read from DB ---
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    # Cache the column values (NOT the ORM instance, to avoid session coupling).
    # `__table__.columns` gives us the persisted column names only.
    try:
        attrs = {c.name: getattr(user, c.name) for c in user.__table__.columns}
        _user_cache[str(user_id)] = (now + _USER_CACHE_TTL_SECONDS, attrs)
    except Exception:
        # Caching is best-effort; never let it break auth.
        pass

    return user


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Get current user or None (for optional auth)."""
    try:
        return await get_current_user(credentials, db)
    except HTTPException:
        return None


async def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Require the current user to be an admin."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user