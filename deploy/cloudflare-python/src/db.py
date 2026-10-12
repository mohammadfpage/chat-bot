"""Minimal D1 repositories. Kept dependency-free so they can be unit tested."""


def _to_dict(row):
    if row is None:
        return None
    if hasattr(row, "to_py"):
        row = row.to_py()
    return dict(row)


class UserRepository:
    def __init__(self, db) -> None:
        self.db = db

    async def upsert(self, user: dict) -> None:
        await (
            self.db.prepare(
                "INSERT INTO users "
                "(telegram_id, username, first_name, language_code, last_seen_at) "
                "VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(telegram_id) DO UPDATE SET "
                "username = excluded.username, "
                "first_name = excluded.first_name, "
                "language_code = excluded.language_code, "
                "last_seen_at = datetime('now')"
            )
            .bind(
                user.get("id"),
                user.get("username"),
                user.get("first_name"),
                user.get("language_code"),
            )
            .run()
        )

    async def count(self) -> int:
        row = _to_dict(await self.db.prepare("SELECT COUNT(*) AS n FROM users").first())
        return int(row["n"]) if row else 0
