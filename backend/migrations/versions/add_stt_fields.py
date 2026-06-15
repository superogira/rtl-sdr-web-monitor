"""
Add stt_provider and stt_error columns to recordings table.
"""
from sqlalchemy import text
from backend.database import async_session


async def upgrade():
    """Add stt_provider and stt_error columns to recordings table."""
    async with async_session() as session:
        try:
            result = await session.execute(text("PRAGMA table_info(recordings)"))
            columns = result.fetchall()
            column_names = [col[1] for col in columns]

            if "stt_provider" not in column_names:
                await session.execute(text(
                    "ALTER TABLE recordings ADD COLUMN stt_provider VARCHAR(20)"
                ))
                print("✅ Added stt_provider column to recordings table")
            else:
                print("ℹ️ stt_provider column already exists")

            if "stt_error" not in column_names:
                await session.execute(text(
                    "ALTER TABLE recordings ADD COLUMN stt_error TEXT"
                ))
                print("✅ Added stt_error column to recordings table")
            else:
                print("ℹ️ stt_error column already exists")

            await session.commit()
        except Exception as e:
            print(f"⚠️ Error adding STT columns: {e}")
            raise


async def downgrade():
    """Downgrade not supported for SQLite (would require table rebuild)."""
    print("⚠️ Downgrade not supported for SQLite. Use a backup to revert.")


if __name__ == "__main__":
    import asyncio
    print("Running migration: Add stt_provider/stt_error columns to recordings")
    asyncio.run(upgrade())
    print("Migration completed!")