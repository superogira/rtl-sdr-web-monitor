from sqlalchemy import event
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


# ---------------------------------------------------------------------------
# SQLite performance pragmas.
#
# Default SQLite (journal_mode=delete, synchronous=FULL) fsyncs the WHOLE
# database file on every commit. Measured on this server: ~75-95 ms per
# COMMIT, which blocks the single asyncio event loop that also runs live audio
# streaming → audio underruns/re-buffering whenever anyone logs in or saves a
# setting.
#
# WAL mode (write-ahead logging) writes append-only to a small -wal file and
# only fsyncs that, allowing readers and the writer to run concurrently.
# Combined with synchronous=NORMAL (the SQLite-recommended pairing for WAL),
# COMMIT drops to <5 ms. WAL is durable across app crashes; the only thing at
# risk under NORMAL is the very last transaction on a HARD OS crash / power
# loss (acceptable trade-off for a monitoring app, and the standard guidance).
#
# These PRAGMAs are applied per-connection via the SQLAlchemy "connect" event
# on the underlying sync_engine (aiosqlite wraps sqlite3, so this fires for
# every real connection). Only attached when the URL is SQLite so a future
# Postgres/etc. deployment is unaffected.
# ---------------------------------------------------------------------------
if "sqlite" in settings.DATABASE_URL:
    _SQLITE_PRAGMAS = (
        ("journal_mode", "WAL"),
        ("synchronous", "NORMAL"),
        ("mmap_size", "268435456"),   # 256 MB memory-mapped I/O
        ("cache_size", "-65536"),      # ~64 MB page cache (negative = KB)
        ("temp_store", "MEMORY"),      # temp tables/indices in RAM
        ("busy_timeout", "5000"),      # ms to wait on lock contention
        ("foreign_keys", "ON"),
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cur = dbapi_conn.cursor()
        try:
            for key, value in _SQLITE_PRAGMAS:
                cur.execute(f"PRAGMA {key}={value}")
        finally:
            cur.close()

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
        ("file_size", "BIGINT"),
    ]

    for col_name, col_type in migrations:
        if col_name not in columns:
            conn.execute(
                text(f"ALTER TABLE recordings ADD COLUMN {col_name} {col_type}")
            )
            print(f"[migration] Added '{col_name}' column to recordings table")

    # === Backfill file_size for existing recordings (one-time) ===
    # For rows where file_size IS NULL and an audio file still exists on disk,
    # stat() the file and store its size. Runs at most once per NULL row.
    import os
    rows = conn.execute(text(
        "SELECT id, audio_file_path FROM recordings "
        "WHERE file_size IS NULL AND audio_file_path IS NOT NULL"
    )).fetchall()
    backfilled = 0
    for rec_id, audio_file_path in rows:
        try:
            if audio_file_path and os.path.exists(audio_file_path):
                size = os.path.getsize(audio_file_path)
                conn.execute(text(
                    "UPDATE recordings SET file_size = :size WHERE id = :rid"
                ), {"size": size, "rid": rec_id})
                backfilled += 1
        except Exception:
            # Best-effort: skip files that can't be stat'd (missing/permission)
            continue
    if backfilled > 0:
        print(f"[migration] Backfilled file_size for {backfilled} recordings")

    # === Recordings performance indexes ===
    # The Recordings list page (and its count/sum aggregate) orders by
    # recorded_at DESC with optional filters on source/device_id/frequency.
    # Without these, EXPLAIN shows `SCAN recordings` + `USE TEMP B-TREE FOR
    # ORDER BY` on every page load. As the table grows this becomes painful.
    # Idempotent: CREATE INDEX IF NOT EXISTS is a no-op if the index exists.
    recordings_indexes = [
        # Covers the default ORDER BY recorded_at DESC LIMIT/OFFSET.
        ("ix_recordings_recorded_at", "recordings", "recorded_at DESC"),
        # Composite indexes for filter+sort combos used by _apply_recording_filters.
        ("ix_recordings_source_recorded_at", "recordings", "source, recorded_at"),
        ("ix_recordings_device_recorded_at", "recordings", "device_id, recorded_at"),
        # Standalone index for the frequency filter (range match ±tolerance).
        ("ix_recordings_frequency", "recordings", "frequency"),
    ]
    existing_idx = {
        row[0]
        for row in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='recordings'")
        )
    }
    for idx_name, tbl, cols in recordings_indexes:
        if idx_name not in existing_idx:
            conn.execute(
                text(f"CREATE INDEX {idx_name} ON {tbl} ({cols})")
            )
            print(f"[migration] Created index '{idx_name}' on {tbl}({cols})")

    # === recording_job_frequencies: add auto_squelch column ===
    result = conn.execute(text("PRAGMA table_info(recording_job_frequencies)"))
    freq_columns = [row[1] for row in result]
    if "auto_squelch" not in freq_columns:
        conn.execute(text(
            "ALTER TABLE recording_job_frequencies "
            "ADD COLUMN auto_squelch BOOLEAN DEFAULT 0 NOT NULL"
        ))
        print("[migration] Added 'auto_squelch' column to "
              "recording_job_frequencies table")

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

    # === keywords table: add group_id, type, score columns (keyword alerting) ===
    result = conn.execute(text("PRAGMA table_info(keywords)"))
    kw_columns = [row[1] for row in result]
    kw_migrations = [
        ("group_id", "VARCHAR(36)"),
        ("type", "VARCHAR(30) DEFAULT 'other' NOT NULL"),
        ("score", "INTEGER DEFAULT 1 NOT NULL"),
    ]
    for col_name, col_type in kw_migrations:
        if col_name not in kw_columns:
            conn.execute(text(
                f"ALTER TABLE keywords ADD COLUMN {col_name} {col_type}"
            ))
            print(f"[migration] Added '{col_name}' column to keywords table")
    # Index on group_id for efficient group lookups.
    existing_idx = {
        row[0]
        for row in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='keywords'")
        )
    }
    if "ix_keywords_group_id" not in existing_idx:
        conn.execute(text("CREATE INDEX ix_keywords_group_id ON keywords (group_id)"))
        print("[migration] Created index 'ix_keywords_group_id' on keywords(group_id)")

    # === alert_events: add device_name snapshot column ===
    # Lets alerts carry the SDR device name they were recorded on, shown in the
    # Telegram message and the alerts/stats pages.
    result = conn.execute(text("PRAGMA table_info(alert_events)"))
    ev_columns = [row[1] for row in result]
    if "device_name" not in ev_columns:
        conn.execute(text(
            "ALTER TABLE alert_events ADD COLUMN device_name VARCHAR(100)"
        ))
        print("[migration] Added 'device_name' column to alert_events table")


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
    from backend.models import keyword  # noqa: F401  (register Keyword model)
    from backend.models import keyword_group  # noqa: F401  (register KeywordGroup model)
    from backend.models import notification_channel  # noqa: F401  (NotificationChannel/Group)
    from backend.models import alert_rule  # noqa: F401  (AlertRule + association tables)
    from backend.models import alert_event  # noqa: F401  (AlertJob/Event/Delivery)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Run migrations (add columns to existing tables)
        await conn.run_sync(_run_migrations)
        # Seed default app settings
        await conn.run_sync(_seed_app_settings)
        # Seed default SDR device from legacy config (if none exist)
        await conn.run_sync(_seed_sdr_devices)
