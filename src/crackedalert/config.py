"""Environment-driven configuration.

Everything the bot needs comes from the environment (loaded from
/etc/cracked_alert/.env_cracked in production via python-dotenv).
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List

from dotenv import load_dotenv

DEFAULT_DATA_DIR = "/etc/cracked_alert"
ENV_FILE = os.path.join(DEFAULT_DATA_DIR, ".env_cracked")


@dataclass(frozen=True)
class Account:
    shortcode: str
    ctid_account_id: int
    environment: str  # "live" | "demo"


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_chat_ids: List[int]
    ctrader_client_id: str
    ctrader_client_secret: str
    accounts: Dict[str, Account] = field(default_factory=dict)
    price_feed_account: str = "demo"
    data_dir: str = DEFAULT_DATA_DIR
    trade_symbol: str = "XAUUSD"
    # Alert-status HTTP endpoint for the mobile alarm app. Empty token
    # disables the endpoint entirely (safe default).
    alert_status_token: str = ""
    alert_status_port: int = 8190

    @property
    def tokens_file(self) -> str:
        return os.path.join(self.data_dir, "tokens.json")

    @property
    def db_file(self) -> str:
        return os.path.join(self.data_dir, "cracked.db")

    @property
    def legacy_tsv(self) -> str:
        return os.path.join(self.data_dir, "cracked_alerts.tsv")

    def environments_in_use(self) -> List[str]:
        return sorted({a.environment for a in self.accounts.values()})


class ConfigError(Exception):
    pass


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError("missing required env var: %s" % name)
    return val


def load_settings() -> Settings:
    # Production env file first (no-op if absent), process env wins.
    load_dotenv(ENV_FILE)
    load_dotenv()  # local ./.env for development

    raw_accounts = _require("CTRADER_ACCOUNTS")
    try:
        parsed = json.loads(raw_accounts)
    except json.JSONDecodeError as e:
        raise ConfigError("CTRADER_ACCOUNTS is not valid JSON: %s" % e)

    accounts = {}
    for shortcode, spec in parsed.items():
        env = spec.get("env")
        if env not in ("live", "demo"):
            raise ConfigError(
                "account '%s': env must be 'live' or 'demo', got %r"
                % (shortcode, env))
        acc_id = int(spec.get("id") or 0)
        if acc_id <= 0:
            raise ConfigError(
                "account '%s': missing ctid account id (run auth_setup.py)"
                % shortcode)
        accounts[shortcode] = Account(shortcode, acc_id, env)

    if not accounts:
        raise ConfigError("CTRADER_ACCOUNTS is empty")

    chat_ids = []
    for part in _require("ALLOWED_CHAT_IDS").split(","):
        part = part.strip()
        if part:
            chat_ids.append(int(part))
    if not chat_ids:
        raise ConfigError("ALLOWED_CHAT_IDS is empty")

    price_feed = os.environ.get("PRICE_FEED_ACCOUNT", "demo").strip()
    if price_feed not in accounts:
        raise ConfigError(
            "PRICE_FEED_ACCOUNT '%s' is not a configured account" % price_feed)

    return Settings(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=chat_ids,
        ctrader_client_id=_require("CTRADER_CLIENT_ID"),
        ctrader_client_secret=_require("CTRADER_CLIENT_SECRET"),
        accounts=accounts,
        price_feed_account=price_feed,
        data_dir=os.environ.get("CRACKED_DATA_DIR", DEFAULT_DATA_DIR).strip()
        or DEFAULT_DATA_DIR,
        trade_symbol=os.environ.get("TRADE_SYMBOL", "XAUUSD").strip().upper()
        or "XAUUSD",
        alert_status_token=os.environ.get("ALERT_STATUS_TOKEN", "").strip(),
        alert_status_port=int(
            os.environ.get("ALERT_STATUS_PORT", "8190").strip() or "8190"),
    )
