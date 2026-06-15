"""
Add timezone column to users table
"""
from sqlalchemy import text
from backend.database import async_session
from backend.models.user import User
from sqlalchemy import select


async def upgrade():
    """Add timezone column to users table with default value 'Asia/Bangkok'"""
    async with async_session() as session:
        try:
            # Check if column already exists
            result = await session.execute(text("PRAGMA table_info(users)"))
            columns = result.fetchall()
            column_names = [col[1] for col in columns]
            
            if 'timezone' not in column_names:
                # Add timezone column
                await session.execute(text(
                    "ALTER TABLE users ADD COLUMN timezone VARCHAR(50) DEFAULT 'Asia/Bangkok'"
                ))
                await session.commit()
                print("✅ Added timezone column to users table")
            else:
                print("ℹ️ Timezone column already exists in users table")
        except Exception as e:
            print(f"⚠️ Error adding timezone column: {e}")
            raise


async def downgrade():
    """Remove timezone column from users table"""
    async with async_session() as session:
        try:
            # SQLite doesn't support DROP COLUMN directly
            # Need to recreate table without the column
            await session.execute(text(
                """
                CREATE TABLE users_new (
                    id VARCHAR(36) PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    email VARCHAR(100) UNIQUE,
                    display_name VARCHAR(100),
                    hashed_password VARCHAR(255) NOT NULL,
                    role VARCHAR(20) DEFAULT 'user' NOT NULL,
                    is_active BOOLEAN DEFAULT 1 NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    last_login DATETIME
                )
                """
            ))
            
            # Copy data from old table
            await session.execute(text(
                """
                INSERT INTO users_new (id, username, email, display_name, hashed_password, 
                                     role, is_active, created_at, updated_at, last_login)
                SELECT id, username, email, display_name, hashed_password,
                       role, is_active, created_at, updated_at, last_login
                FROM users
                """
            ))
            
            # Drop old table
            await session.execute(text("DROP TABLE users"))
            
            # Rename new table
            await session.execute(text("ALTER TABLE users_new RENAME TO users"))
            
            await session.commit()
            print("✅ Removed timezone column from users table")
        except Exception as e:
            print(f"⚠️ Error removing timezone column: {e}")
            raise


if __name__ == "__main__":
    import asyncio
    print("Running migration: Add timezone column to users table")
    asyncio.run(upgrade())
    print("Migration completed!")
