"""Pure-Python tests. No Workers runtime required (uses asyncio.run + fakes)."""

import asyncio
from types import SimpleNamespace

import config
from db import UserRepository
from router import HELP, WELCOME, Router


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.answers = []

    async def send_message(self, chat_id, text, *, reply_markup=None):
        self.sent.append((chat_id, text))
        return {"ok": True}

    async def answer_callback(self, callback_query_id):
        self.answers.append(callback_query_id)
        return {"ok": True}


class FakeUsers:
    def __init__(self):
        self.seen = []

    async def upsert(self, user):
        self.seen.append(user)


def _private(text, **extra):
    message = {
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "username": "ali", "first_name": "Ali"},
        "text": text,
    }
    message.update(extra)
    return message


def test_start_records_user_and_welcomes():
    tg, users = FakeTelegram(), FakeUsers()
    router = Router(users, tg)
    asyncio.run(router.handle({"message": _private("/start")}))
    assert users.seen and users.seen[0]["id"] == 42
    assert tg.sent == [(42, WELCOME)]


def test_help():
    tg, users = FakeTelegram(), FakeUsers()
    asyncio.run(Router(users, tg).handle({"message": _private("/help")}))
    assert tg.sent == [(42, HELP)]


def test_group_chat_is_silent():
    tg, users = FakeTelegram(), FakeUsers()
    msg = _private("/start")
    msg["chat"]["type"] = "group"
    asyncio.run(Router(users, tg).handle({"message": msg}))
    assert tg.sent == []
    assert users.seen == []


def test_unknown_command_ignored():
    tg, users = FakeTelegram(), FakeUsers()
    asyncio.run(Router(users, tg).handle({"message": _private("/nope")}))
    assert tg.sent == []
    assert users.seen  # user still recorded


def test_callback_is_answered():
    tg, users = FakeTelegram(), FakeUsers()
    update = {"callback_query": {"id": "cb-1", "data": "x"}}
    asyncio.run(Router(users, tg).handle(update))
    assert tg.answers == ["cb-1"]


def test_settings_from_env():
    env = SimpleNamespace(TELEGRAM_BOT_TOKEN="t", TELEGRAM_WEBHOOK_SECRET="s")
    settings = config.Settings.from_env(env)
    assert settings.bot_token == "t"
    assert settings.webhook_secret == "s"


class _FakeStmt:
    def __init__(self, db, sql):
        self.db = db
        self.sql = sql
        self.args = None

    def bind(self, *args):
        self.args = args
        return self

    async def run(self):
        self.db.runs.append((self.sql, self.args))
        return {"ok": True}

    async def first(self):
        self.db.queries.append(self.sql)
        return {"n": 3}


class _FakeDB:
    def __init__(self):
        self.runs = []
        self.queries = []

    def prepare(self, sql):
        self.queries.append(sql)
        return _FakeStmt(self, sql)


def test_user_repository_upsert_and_count():
    db = _FakeDB()
    repo = UserRepository(db)
    asyncio.run(repo.upsert({"id": 7, "username": "u", "first_name": "F"}))
    assert db.runs
    assert db.runs[0][1] == (7, "u", "F", None)
    assert asyncio.run(repo.count()) == 3
