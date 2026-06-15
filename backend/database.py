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


def _run_migrations(conn):
    """Run manual migrations (add columns to existing tables).

    NOTE: This function MUST be synchronous (def, not async def) because it is
    called via conn.run_sync(). The previous async version silently failed with
    'coroutine was never awaited'.

    SQLite < 3.35 doesn't support ALTER TABLE ... ADD COLUMN ... IF NOT EXISTS,
    so we check pragma_table_info first.
    """
    from sqlalchemy import text

    result = conn.execute(text("PRAGMA table_info(recordings)"))
    columns = [row[1] for row in result]

    # Define all columns that might need adding (column_name, SQL type)
    migrations = [
        ("source", "VARCHAR(20) DEFAULT 'auto' NOT NULL"),
        ("signal_strength", "FLOAT"),
        ("audio_file_path", "VARCHAR(500)"),
        ("audio_file_name", "VARCHAR(255)"),
        ("duration", "FLOAT"),
        ("transcript", "TEXT"),
        ("transcript_language", "VARCHAR(10) DEFAULT 'th'"),
        ("status", "VARCHAR(20) DEFAULT 'completed' NOT NULL"),
        ("is_unread", "INTEGER DEFAULT 1 NOT NULL"),
        ("recorded_at", "DATETIME"),
        ("frequency_mhz", "FLOAT"),
        ("stt_provider", "VARCHAR(20)"),
        ("stt_error", "TEXT"),
    ]

    for col_name, col_type in migrations:
        if col_name not in columns:
            conn.execute(
                text(f"ALTER TABLE recordings ADD COLUMN {col_name} {col_type}")
            )
            print(f"[migration] Added '{col_name}' column to recordings table")


def _seed_app_settings(conn):
    """Seed default global app settings (STT config, etc.) if not present.

    Idempotent: only inserts rows that don't exist yet.
    NOTE: MUST be sync (def) — called via conn.run_sync().
    """
    from sqlalchemy import text

    defaults = {
        "stt_provider": "speechmatics",
        "stt_speechmatics_api_key": "XJ4zppb60QnkpznSfpy59udFfp9iK22O",
        "stt_language": "th",
        "stt_auto_transcribe": "false",
        "stt_operating_point": "standard",
        "stt_diarization": "none",
        "stt_enable_entities": "false",
    }

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    for key, value in defaults.items():
        result = conn.execute(text("SELECT value FROM app_settings WHERE key = :key"), {"key": key})
        if result.fetchone() is None:
            conn.execute(
                text("INSERT INTO app_settings (key, value, updated_at) VALUES (:key, :value, :ts)"),
                {"key": key, "value": value, "ts": now_iso},
            )
            print(f"[seed] App setting '{key}' initialized")


async def init_db():
    """Initialize database (create tables if needed)."""
    # Import models so Base.metadata.create_all picks them up.
    from backend.models import app_setting  # noqa: F401  (register AppSetting model)
    from backend.models import recording  # noqa: F401  (register Recording model)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Run migrations (add columns to existing tables)
        await conn.run_sync(_run_migrations)
        # Seed default app settings
        await conn.run_sync(_seed_app_settings)
