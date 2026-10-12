"""Runtime configuration read from Worker bindings / secrets."""


class Settings:
    def __init__(self, bot_token: str, webhook_secret: str) -> None:
        self.bot_token = bot_token
        self.webhook_secret = webhook_secret

    @classmethod
    def from_env(cls, env) -> "Settings":
        return cls(
            bot_token=str(env.TELEGRAM_BOT_TOKEN),
            webhook_secret=str(env.TELEGRAM_WEBHOOK_SECRET),
        )
