"""Generate a temporary Meta App Review deep link and server-side code hash."""

import argparse
import hashlib
import secrets
from datetime import datetime, timedelta, timezone


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate temporary Telegram access for Meta App Review"
    )
    parser.add_argument(
        "--bot-username",
        required=True,
        help="Telegram bot username without @",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Review access lifetime in days (default: 30)",
    )
    args = parser.parse_args()
    if not 1 <= args.days <= 30:
        parser.error("--days must be between 1 and 30")

    username = args.bot_username.strip().lstrip("@")
    if not username or not username.replace("_", "").isalnum():
        parser.error("--bot-username is invalid")

    code = secrets.token_urlsafe(24)
    code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=args.days)

    print("Store only these values in the server .env:")
    print(f"THREADS_REVIEW_ACCESS_CODE_HASH={code_hash}")
    print(
        "THREADS_REVIEW_ACCESS_EXPIRES_AT="
        f"{expires_at.isoformat(timespec='seconds')}"
    )
    print("\nGive only this link to Meta in App Review instructions:")
    print(f"https://t.me/{username}?start=review_{code}")
    print("\nThe raw code cannot be recovered from the server-side hash.")


if __name__ == "__main__":
    main()
