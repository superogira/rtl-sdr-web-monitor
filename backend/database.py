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
        ("stt_model", "VARCHAR(255)"),
        ("stt_error", "TEXT"),
        ("stt_duration", "FLOAT"),
        ("audio_deleted", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("recorded_by_user_id", "VARCHAR(36)"),
        ("quality_rating", "INTEGER"),
        ("quality_rated_by_user_id", "VARCHAR(36)"),
        ("quality_rated_at", "DATETIME"),
        ("device_id", "VARCHAR(36)"),
        ("recording_job_id", "VARCHAR(36)"),
    ]

    for col_name, col_type in migrations:
        if col_name not in columns:
            conn.execute(
                text(f"ALTER TABLE recordings ADD COLUMN {col_name} {col_type}")
            )
            print(f"[migration] Added '{col_name}' column to recordings table")

    # === Data migration: convert legacy string quality_rating → integer scale ===
    # Previous schema used "good"|"fair"|"bad". New schema uses 1-6 integers.
    # SQLite dynamic typing allows storing ints in a VARCHAR column, so we just
    # UPDATE the values in place. Idempotent: only touches rows still holding
    # the old string values.
    if "quality_rating" in columns:
        legacy_map = {
            "good": 5,    # ดีมาก (close to old "good")
            "fair": 3,    # พอใช้
            "bad": 2,     # แย่
        }
        for old_val, new_val in legacy_map.items():
            # case-insensitive match; update to bare integer
            result = conn.execute(text(
                "UPDATE recordings SET quality_rating = :new "
                "WHERE LOWER(CAST(quality_rating AS TEXT)) = :old"
            ), {"new": new_val, "old": old_val})
            if result.rowcount > 0:
                print(f"[migration] Converted {result.rowcount} quality_rating "
                      f"'{old_val}' → {new_val}")


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
        "manual_recording_enabled": "true",
        # Server timezone for scheduled recording jobs (admin-configurable).
        # IANA name, e.g. "Asia/Bangkok". Used by the recording scheduler.
        "server_timezone": "Asia/Bangkok",
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


def _seed_sdr_devices(conn):
    """Seed the default SDR device from data/sdr_settings.json if none exist.

    Idempotent: only runs when the sdr_devices table is empty. This migrates
    the legacy single-device config (sdr_settings.json) into the new device
    registry so existing deployments keep their working RTL-TCP connection.
    NOTE: MUST be sync (def) — called via conn.run_sync().
    """
    from sqlalchemy import text
    import json
    import os

    # Check if any device already exists
    result = conn.execute(text("SELECT COUNT(*) FROM sdr_devices"))
    if result.scalar() > 0:
        return

    # Load legacy settings from data/sdr_settings.json (best-effort)
    config = {
        "host": "127.0.0.1",
        "port": 1234,
        "frequency": 145500000,
        "sample_rate": 2048000,
        "gain": 30,
        "freq_correction_ppm": 0,
    }
    try:
        legacy_path = os.path.join("data", "sdr_settings.json")
        if os.path.exists(legacy_path):
            with open(legacy_path, "r", encoding="utf-8") as f:
                legacy = json.load(f)
            for key in config:
                if key in legacy and legacy[key] is not None:
                    config[key] = legacy[key]
            print(f"[seed] Loaded legacy SDR settings from {legacy_path}")
    except Exception as e:
        print(f"[seed] Could not load legacy sdr_settings.json: {e}")

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    device_id = str(__import__("uuid").uuid4())
    conn.execute(text(
        "INSERT INTO sdr_devices (id, name, type, config, is_active, created_at, updated_at) "
        "VALUES (:id, :name, :type, :config, 1, :ts, :ts)"
    ), {
        "id": device_id,
        "name": "RTL-TCP (ค่าเริ่มต้น)",
        "type": "rtl_tcp",
        "config": json.dumps(config, ensure_ascii=False),
        "ts": now_iso,
    })
    print(f"[seed] Created default SDR device '{device_id}' "
          f"(host={config['host']}:{config['port']})")


async def init_db():
    """Initialize database (create tables if needed)."""
    # Import models so Base.metadata.create_all picks them up.
    from backend.models import app_setting  # noqa: F401  (register AppSetting model)
    from backend.models import recording  # noqa: F401  (register Recording model)
    from backend.models import sdr_device  # noqa: F401  (register SdrDevice model)
    from backend.models import recording_job  # noqa: F401  (register RecordingJob models)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Run migrations (add columns to existing tables)
        await conn.run_sync(_run_migrations)
        # Seed default app settings
        await conn.run_sync(_seed_app_settings)
        # Seed default SDR device from legacy config (if none exist)
        await conn.run_sync(_seed_sdr_devices)
