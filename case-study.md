# Case Study: CrackedAlert

## Problem

I trade XAUUSD and needed to track price levels without staying inside the broker's platform. I wanted to set an alert or place a trade from Telegram and put the phone down. The cTrader Open API supports WebSocket connections for price feeds and order execution, but nothing connected it to Telegram out of the box.

## What I Built

A self-hosted Telegram bot that connects to cTrader's live and demo environments simultaneously, fires price alerts when spot prices cross thresholds, fires candle-close alerts when a timeframe closes above or below a threshold, and places market or pending orders from a single command with calculated position size.

The system grew in three phases. Phase 1 was price alerts only. Phase 2 added candle-close alerts, which are more reliable for trade confirmation than tick-level crossings. Phase 3 added full order execution with automatic TP/SL alerts.

## CTraderClient

The core is a custom async WebSocket client in `ctrader/client.py`. The cTrader API uses a request/response pattern over a shared connection. Every outbound frame includes a `clientMsgId`; the matching inbound frame carries the same ID back. The client stores a pending `asyncio.Future` per ID and resolves it when the response arrives.

The tricky part: the recv loop must be running before `on_connected` fires. That callback authenticates trading accounts and re-subscribes to price feeds, both of which require sending requests and waiting for responses. If the recv loop is not running yet, those responses never get delivered and every request times out. The fix is to start the recv loop as a background task first, then await `on_connected` while the loop runs. A helper (`_with_receiver`) cancels the setup immediately if the connection drops mid-callback instead of waiting for timeouts.

On disconnect, all pending futures are rejected immediately with `NotConnected`. Callers get the error right away instead of waiting.

Reconnection uses exponential backoff from 1s to 60s. Account auth and subscriptions are fully redone after each reconnect via `on_connected`.

## Candle Alerts

Tick alerts and candle-close alerts are separate systems with different trigger logic. A tick alert fires every time the price updates past a threshold. A candle-close alert fires only when a period ends and the close price is on the right side of the threshold. You cannot substitute one for the other: a price can cross a level mid-candle and then close back on the other side.

The candle system fetches the last completed bar from the cTrader history API (`ProtoOAGetTrendbarsReq`) and checks only the close price. It polls on a configurable interval per timeframe.

## Result

Runs in production on a Linux VPS. Both live and demo cTrader accounts are connected simultaneously. Alerts fire within a second of the price crossing the threshold. The bot stays up via systemd with automatic restarts. The WebSocket client and Telegram handler layers are fully independent, so either can be tested in isolation.
