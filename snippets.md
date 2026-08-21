# Extractable Snippets

## `CTraderClient`

`src/crackedalert/ctrader/client.py`

Generic async WebSocket client for the cTrader Open API. Handles connection lifecycle, app-level authentication, heartbeats (required every 10s), request/response correlation via `clientMsgId` (pending futures in a dict, resolved by message ID), exponential backoff reconnection (1s to 60s), and immediate rejection of in-flight requests on disconnect.

The cTrader API has no official Python client. The request/response correlation pattern is also applicable to any other WebSocket protocol that correlates requests and responses by ID.

---

## `Settings` config dataclass

`src/crackedalert/config.py`

Frozen dataclass loaded from environment variables. Each required variable has a `_require()` helper that raises a named `ConfigError` if missing. JSON fields are parsed and validated at load time, not at first use.

Good template for any Python service that needs clear startup errors on missing or malformed config.

---

## `systemd/cracked-bot.service`

Minimal unit file: starts a Python script via a wrapper, restarts on failure with a 5s delay, logs to journald. Starting point for deploying any Python background process on Linux.

---

## `deploy.sh`

Creates the data directory, sets permissions, installs scripts and systemd unit files, reloads the daemon, and starts the services. Warns if the `.env` file is missing before proceeding. Template for deploying a Python service to a single Linux server without containers.
