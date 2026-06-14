from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from backend.config import settings


# Async engine for FastAPI
# SQLite doesn't support pool_size/max_overflow
engine_kwargs = {"echo": settings.DEBUG}
if "sqlite" not in settings.DATABASE_URL:
    engine_kwargs["pool_size"] = 20
    engine_kwargs["max_overflow"] = 10

engine = create_async_engine(settings.DATABASE_URL, **engine_kwargs)

# Session factory
async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


async def get_db() -> AsyncSession:
    """Dependency for getting async database sessions."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def _run_migrations(conn):
    """Run manual migrations (add columns to existing tables).

    SQLite < 3.35 doesn't support ALTER TABLE ... ADD COLUMN ... IF NOT EXISTS,
    so we check pragma_table_info first.
    """
    from sqlalchemy import text

    # Migration: add 'source' column to recordings table
    result = conn.execute(
        text("PRAGMA table_info(recordings)")
    )
    columns = [row[1] for row in result]
    if "source" not in columns:
        conn.execute(
            text(
                "ALTER TABLE recordings ADD COLUMN source VARCHAR(20) "
                "DEFAULT 'auto' NOT NULL"
            )
        )
        print("[migration] Added 'source' column to recordings table")


async def init_db():
    """Initialize database (create tables if needed)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Run migrations (add columns to existing tables)
        await conn.run_sync(_run_migrations)
