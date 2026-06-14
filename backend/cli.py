"""
CLI commands for RTL-SDR Web Monitor.
Usage: python -m backend.cli <command>
"""
import sys
import asyncio
import getpass
from sqlalchemy import select
import bcrypt

from backend.database import async_session, init_db
from backend.models.user import User, UserRole


async def create_superuser():
    """Create a superuser account interactively."""
    print("\n🔧 RTL-SDR Monitor - Create Superuser\n")

    username = input("Username: ").strip()
    if not username or len(username) < 3:
        print("❌ Username must be at least 3 characters")
        return

    email = input("Email (optional, press Enter to skip): ").strip() or None

    display_name = input("Display name (optional): ").strip() or username

    password = getpass.getpass("Password: ")
    if len(password) < 6:
        print("❌ Password must be at least 6 characters")
        return

    password_confirm = getpass.getpass("Confirm password: ")
    if password != password_confirm:
        print("❌ Passwords do not match")
        return

    # Init DB
    await init_db()

    async with async_session() as session:
        # Check if username exists
        result = await session.execute(select(User).where(User.username == username))
        if result.scalar_one_or_none():
            print(f"❌ Username '{username}' already exists")
            return

        # Create user
        user = User(
            username=username,
            email=email,
            display_name=display_name,
            hashed_password=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
            role=UserRole.ADMIN,
            is_active=True,
        )
        session.add(user)
        await session.commit()

        print(f"\n✅ Superuser '{username}' created successfully!")
        print(f"   Role: Admin")
        print(f"   You can now login at the web interface.\n")


async def create_user(username: str, password: str, role: str = "user"):
    """Create a user non-interactively (for scripts)."""
    await init_db()

    async with async_session() as session:
        result = await session.execute(select(User).where(User.username == username))
        if result.scalar_one_or_none():
            print(f"❌ Username '{username}' already exists")
            return

        user = User(
            username=username,
            hashed_password=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
            role=UserRole.ADMIN if role == "admin" else UserRole.USER,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        print(f"✅ User '{username}' ({role}) created successfully!")


async def reset_password(username: str):
    """Reset a user's password."""
    password = getpass.getpass(f"New password for '{username}': ")
    if len(password) < 6:
        print("❌ Password must be at least 6 characters")
        return

    await init_db()

    async with async_session() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if not user:
            print(f"❌ User '{username}' not found")
            return

        user.hashed_password = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        await session.commit()
        print(f"✅ Password for '{username}' updated successfully!")


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m backend.cli <command>")
        print("\nCommands:")
        print("  createsuperuser    Create an admin user interactively")
        print("  createuser <username> <password> [admin]  Create a user")
        print("  resetpassword <username>  Reset a user's password")
        return

    command = sys.argv[1]

    if command == "createsuperuser":
        asyncio.run(create_superuser())
    elif command == "createuser":
        if len(sys.argv) < 4:
            print("Usage: python -m backend.cli createuser <username> <password> [admin]")
            return
        uname = sys.argv[2]
        pwd = sys.argv[3]
        role = sys.argv[4] if len(sys.argv) > 4 else "user"
        asyncio.run(create_user(uname, pwd, role))
    elif command == "resetpassword":
        if len(sys.argv) < 3:
            print("Usage: python -m backend.cli resetpassword <username>")
            return
        asyncio.run(reset_password(sys.argv[2]))
    else:
        print(f"Unknown command: {command}")


if __name__ == "__main__":
    main()