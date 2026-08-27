"""Telegram reply templates — HTML parse_mode, mobile-first.

P0: Every interpolated value is HTML-escaped at render time (quote=False)
so user notes containing < > & never cause Telegram 400 errors.

P1: Templates rewritten for expandable blockquotes, tap-to-copy <code>
spans, and emoji side-glyphs. Function signatures are unchanged.
"""

from html import escape as _html_escape
from typing import List, Optional

from .. import version as _pkg_version
from ..alerts import (CANDLE_ABOVE, CANDLE_BELOW, CROSSING_UP, Alert,
                      CandleAlert)



# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------

def esc(value) -> str:
    """HTML-escape a value for Telegram's HTML parse_mode.

    quote=False on purpose: Telegram only requires <, > and & to be escaped.
    Escaping quotes would emit &#x27; which Telegram does not decode, so an
    apostrophe in a note would render literally as "&#x27;".

    Escape at RENDER time, never at input time. Escaping on the way into the
    database means you can't reuse the value anywhere else without
    double-escaping it.
    """
    if value is None:
        return ""
    return _html_escape(str(value), quote=False)


def _side_glyph(side) -> str:
    s = (str(side or "")).upper()
    if s.startswith("BUY"):
        return "\U0001F7E2"   # green circle
    if s.startswith("SELL"):
        return "\U0001F534"   # red circle
    return "\u26AA"           # white circle


# --------------------------------------------------------------------------
# alert set
# --------------------------------------------------------------------------

def alert_set(alert: Alert, live_price: float) -> str:
    relation = "lower" if alert.direction == CROSSING_UP else "higher"
    return ("cracked alert set (id: %s).\n"
            "%s\n"
            "%s\n\n"
            "Notes: %s\n"
            "current price (%s) is now %s than target."
            % (esc(alert.id), esc(alert.symbol), _trim(alert.target),
               esc(alert.message), _trim(live_price), esc(relation)))


# --------------------------------------------------------------------------
# alert fired
# --------------------------------------------------------------------------

def alert_fired(alert: Alert) -> str:
    """Fired message: just the notes. No price, id, or symbol.

    Without notes, fall back to the minimal 'price hit <target>'."""
    message = getattr(alert, "message", None)
    if message:
        return esc(message)
    return "price hit %s" % esc(_trim(alert.target))


# --------------------------------------------------------------------------
# usage builder
# --------------------------------------------------------------------------

def usage(command: str, args: str = "", example: str = "") -> str:
    """The <code> wrapper matters: tapping a code span in Telegram copies it.
    On a phone the user taps the example, pastes, edits two numbers, sends.
    """
    lines = [
        "\u26A0\ufe0f <b>Usage</b>",
        "<code>%s %s</code>" % (esc(command), esc(args)) if args
        else "<code>%s</code>" % esc(command),
    ]
    if example:
        lines.append("<i>example</i>  <code>%s</code>" % esc(example))
    return "\n".join(lines)


def alert_usage() -> str:
    return usage("/alert", "[target] [notes] [--all]",
                 "/alert 2450.00 approaching demand --all")


def cancel_usage() -> str:
    return usage("/cancel", "[id]", "/cancel 42O4")


def trade_usage(is_market: bool) -> str:
    if is_market:
        return usage("/m",
                     "[sl] [widen:y/n] [rr] [risk%] [account] [--smart-sl <price>] [tf guard] [--all]",
                     "/m 2440.00 y 2 0.5 10k --smart-sl 2435 M15 4080 --all")
    return usage("/p",
                 "[entry] [sl] [widen:y/n] [rr] [risk%] [account] [--smart-sl <price>] [tf guard] [--all]",
                 "/p 2450.00 2455.00 n 3 1 5k --smart-sl 2452 H1 2445 --all")


def positions_usage(is_positions: bool) -> str:
    cmd = "/positions" if is_positions else "/orders"
    return usage(cmd, "[account]", "%s live100k" % cmd)


def close_usage() -> str:
    return usage("/close", "[id] [account]", "/close 4467051 live100k")


def close_all_usage() -> str:
    return usage("/close_all", "[account]", "/close_all live100k")


def cancel_order_usage() -> str:
    return usage("/cancel_order", "[id] [account]",
                 "/cancel_order 4467051 live100k")


def breakeven_usage() -> str:
    return usage("/be", "[account]", "/be live100k")


def candle_alert_usage() -> str:
    return usage("/ccalert", "[tf] [price] [above|below] [symbol] [notes] [--all]",
                 "/ccalert M15 2450 above XAUUSD --all")


def candle_cancel_usage() -> str:
    return usage("/cccancel", "[id]", "/cccancel 3")


# --------------------------------------------------------------------------
# cancelled / not-found / no-alerts
# --------------------------------------------------------------------------

def cancelled(alert_id: str) -> str:
    return "alert %s cancelled." % esc(alert_id)


def cancel_not_found(alert_id: str) -> str:
    return "id %s not found or doesn't belong to you." % esc(alert_id)


def no_alerts() -> str:
    return "no active alerts."


def alert_list(alerts: List[Alert]) -> str:
    lines = ["active alerts:"]
    for a in alerts:
        line = "<code>%s</code>" % esc(a.id)   # tap-to-copy id
        if getattr(a, "message", None):
            line += " - %s" % esc(a.message)
        lines.append(line)
    return "\n".join(lines)


def price_fetch_error(symbol: str) -> str:
    return "\u274C API Error: Could not fetch live price for %s." % esc(symbol)


def account_not_found(account: str) -> str:
    return "error: account '%s' not found." % esc(account)


# --------------------------------------------------------------------------
# help
# --------------------------------------------------------------------------


def help_text() -> str:
    """/help reply: running version, command-builder UI, alarm-app APK."""
    return "\n".join([
        f"<b>cracked alert {_pkg_version()}</b>",
        "",
        "UI: <a href=\"https://alert.hotland3x3.my.id/ui.html\">"
        "alert.hotland3x3.my.id/ui.html</a>",
        "Android app (APK): <a href=\"https://github.com/adiwastu/"
        "crackedalert/raw/main/android/CrackedAlarm-debug.apk\">"
        "CrackedAlarm-debug.apk</a>",
    ])


# --------------------------------------------------------------------------
# order placed
# --------------------------------------------------------------------------

def order_success(ticket, symbol: str, direction: str, kind_label: str,
                  account: str, lots: float, risk_pct: float, risk_usd: float,
                  entry_label: str, sl: float, tp: float, rr: float,
                  widen_label: str, digits: int = 2,
                  dollar_risk: bool = False,
                  smart_sl: Optional[float] = None,
                  smart_risk_usd: Optional[float] = None,
                  smart_risk_pct: Optional[float] = None) -> str:

    if dollar_risk:
        risk_text = "<code>$%.2f</code> risk" % risk_usd
    else:
        risk_text = "%s%% risk = <code>$%.2f</code> (at original SL)" % (
            esc(_trim(risk_pct)), risk_usd)

    sl_text = "%.*f" % (digits, sl)
    if widen_label:
        sl_text = "%s%s" % (sl_text, esc(widen_label))

    # Smart SL (exact price): the stop is placed at the requested price and
    # the exposure there is risk_usd * smart_dist/dist. Show it in dollars
    # when dollar-risk mode is on, otherwise as a % of balance (pct mode).
    smart_text = ""
    if smart_sl is not None:
        if dollar_risk:
            if smart_risk_usd is not None:
                smart_text = "\u00b7 risk at smart SL <code>%.*f</code> = <code>$%.2f</code>" % (
                    digits, smart_sl, smart_risk_usd)
        elif smart_risk_pct is not None:
            smart_text = "\u00b7 risk at smart SL <code>%.*f</code> = %s%%" % (
                digits, smart_sl, esc(_trim(smart_risk_pct)))

    lines = [
        "\u2705 %s <b>%s %s</b> \u00b7 %s \u00b7 %s" % (
            _side_glyph(direction), esc(direction), esc(symbol),
            esc(kind_label), esc(account)),
        "",
        "entry <code>%s</code>" % esc(entry_label),
        "SL <code>%s</code> \u00b7 TP <code>%.*f</code>" % (
            sl_text, digits, tp),
        "<code>%.2f</code> lots \u00b7 %s \u00b7 RR 1:%s" % (
            lots, risk_text, esc(_trim(rr))),
    ]
    if smart_text:
        lines.append(smart_text)
    lines.extend(["", "<i>ticket #%s</i>" %
                  esc(ticket if ticket is not None else "?")])

    return "\n".join(lines)


def order_failed(symbol: str, direction: str, kind_label: str, account: str,
                 reason: str, retcode: str) -> str:
    return ("order failed (%s)\n"
            "%s - %s %s\n"
            "reason: %s (retcode: %s)"
            % (esc(account), esc(symbol), esc(direction), esc(kind_label),
               esc(reason), esc(retcode)))


def entry_label_market(entry_ref: float, digits: int = 2) -> str:
    return "%.*f" % (digits, entry_ref)


def entry_label_pending(entry: float, placement: float,
                        digits: int = 2) -> str:
    return "%.*f (placed at %.*f)" % (digits, entry, digits, placement)


# --------------------------------------------------------------------------
# positions / working orders
# --------------------------------------------------------------------------

def positions_error(account: str, reason: str) -> str:
    return "error: could not fetch for %s: %s" % (esc(account), esc(reason))


def positions_list(rows: List[dict], is_positions: bool) -> str:
    if not rows:
        return "no open positions." if is_positions else "no working orders."

    head = "<b>open positions</b>" if is_positions else "<b>working orders</b>"
    lines = [head, ""]

    for r in rows:
        side = r.get("side") or "?"
        vol = r.get("volume")
        vol_text = "%.2f" % vol if vol is not None else "?"
        price = r.get("price")
        price_text = _trim(price) if price is not None else "?"
        sl_text = _trim(r.get("sl")) if r.get("sl") is not None \
            else "\u2013"
        tp_text = _trim(r.get("tp")) if r.get("tp") is not None \
            else "\u2013"

        lines.append("%s <b>%s %s</b> <code>%s</code> @ <code>%s</code>" % (
            _side_glyph(side),
            esc(side),
            esc(r.get("symbol") or "?"),
            esc(vol_text),
            esc(price_text),
        ))

        tail = "   SL <code>%s</code> \u00b7 TP <code>%s</code>" % (
            esc(sl_text), esc(tp_text))
        lines.append(tail)

        meta = ["#%s" % esc(r.get("id"))]
        if r.get("extra"):
            meta.append(esc(r["extra"]))
        lines.append("   <i>%s</i>" % " \u00b7 ".join(meta))
        lines.append("")

    return "\n".join(lines).rstrip()


# --------------------------------------------------------------------------
# close_all / close / cancel_order / breakeven
# --------------------------------------------------------------------------

def close_all_result(account: str, results: List[dict]) -> str:
    if not results:
        return "no open positions."
    ok = sum(1 for r in results if r.get("ok"))
    lines = ["closed %d/%d positions (%s):"
             % (ok, len(results), esc(account))]
    for r in results:
        vol = r.get("volume")
        vol_text = "%.2f" % vol if vol is not None else "?"
        status = "closed" if r.get("ok") else "failed: %s" % esc(
            r.get("message", ""))
        lines.append("(%s)  %s %s %s \u2192 %s"
                     % (esc(r.get("id")), esc(r.get("side") or "?"),
                        esc(r.get("symbol") or "?"),
                        esc(vol_text), status))
    return "\n".join(lines)


def close_success(account: str, position_id: int) -> str:
    return "position %s closed (%s)." % (esc(position_id), esc(account))


def close_error(account: str, position_id: int, reason: str) -> str:
    return "close failed (%s): %s" % (esc(account), esc(reason))


def cancel_order_success(account: str, order_id: int) -> str:
    return "order %s cancelled (%s)." % (esc(order_id), esc(account))


def cancel_order_error(account: str, order_id: int, reason: str) -> str:
    return "cancel failed (%s): %s" % (esc(account), esc(reason))


def breakeven_result(account: str, results: List[dict]) -> str:
    if not results:
        return "no open positions."
    ok = sum(1 for r in results if r.get("ok"))
    lines = ["breakeven set on %d/%d positions (%s):"
             % (ok, len(results), esc(account))]
    for r in results:
        vol = r.get("volume")
        vol_text = "%.2f" % vol if vol is not None else "?"
        be = r.get("be_sl")
        if r.get("ok"):
            detail = "BE at %s" % esc(_trim(be)) if be is not None \
                else "BE set"
        else:
            detail = "skipped \u2014 %s" % esc(r.get("message", ""))
            if be is not None:
                detail += " (needs %s)" % esc(_trim(be))
        lines.append("(%s)  %s %s %s \u2192 %s"
                     % (esc(r.get("id")), esc(r.get("side") or "?"),
                        esc(r.get("symbol") or "?"),
                        esc(vol_text), detail))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# candle close alerts
# --------------------------------------------------------------------------

def candle_alert_set(alert: CandleAlert, last_close: Optional[float]) -> str:
    direction = "above" if alert.direction == CANDLE_ABOVE else "below"
    line = ("cracked candle alert set (id: %s).\n"
            "%s %s\n"
            "close %s %s\n\n"
            "Notes: %s"
            % (esc(alert.id), esc(alert.symbol), esc(alert.timeframe),
               esc(direction), _trim(alert.target), esc(alert.message)))
    if last_close is not None:
        line += "\nlast closed close: %s" % _trim(last_close)
    return line


def candle_alert_fired(alert: CandleAlert) -> str:
    """Candle fired message: just the notes (same rule as alert_fired)."""
    message = getattr(alert, "message", None)
    if message:
        return esc(message)
    return "price hit %s" % esc(_trim(alert.target))


def candle_alert_list(alerts: List[CandleAlert]) -> str:
    if not alerts:
        return "no active candle alerts."
    lines = ["active candle alerts:"]
    for a in alerts:
        line = "<code>%s</code>" % esc(a.id)   # tap-to-copy id
        if getattr(a, "message", None):
            line += " - %s" % esc(a.message)
        lines.append(line)
    return "\n".join(lines)


def candle_cancelled(alert_id: str) -> str:
    return "candle alert %s cancelled." % esc(alert_id)


def candle_cancel_not_found(alert_id: str) -> str:
    return "id %s not found or doesn't belong to you." % esc(alert_id)


# --------------------------------------------------------------------------
# CC guards (candle-close position auto-close)
# --------------------------------------------------------------------------

def cc_guard_set(alert: CandleAlert) -> str:
    direction = "below" if alert.direction == CANDLE_BELOW else "above"
    return (
        "\U0001F6E1 CC guard set (id: %s)\n"
        "%s %s \u00b7 close %s %s \u2192 position %d auto-closes."
        % (esc(alert.id), esc(alert.symbol), esc(alert.timeframe),
           esc(direction), _trim(alert.target), alert.position_id))


def cc_guard_pending(timeframe: str, price: float) -> str:
    return (
        "\U0001F6E1 CC guard queued: %s close @ %s.\n"
        "Guard activates when this order fills."
        % (esc(timeframe), _trim(price)))


def cc_guard_fired(alert: CandleAlert) -> str:
    direction = "below" if alert.direction == CANDLE_BELOW else "above"
    return (
        "\U0001F6E1 CC guard triggered! (id: %s)\n"
        "%s %s closed %s %s \u2014 position %d closed."
        % (esc(alert.id), esc(alert.symbol), esc(alert.timeframe),
           esc(direction), _trim(alert.target), alert.position_id))


def cc_guard_position_gone(alert: CandleAlert) -> str:
    return (
        "CC guard %s: position %d already closed (SL/TP hit). Guard removed."
        % (esc(alert.id), alert.position_id))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _trim(value: float) -> str:
    """Render 2450.0 as the user typed it: no trailing zeros beyond 2dp."""
    text = ("%.2f" % value).rstrip("0").rstrip(".")
    return text if text else "0"


# --------------------------------------------------------------------------
# subscription
# --------------------------------------------------------------------------

def subscribed(chat_id: int) -> str:
    return "\u2705 chat %s subscribed." % esc(chat_id)


def unsubscribed(chat_id: int) -> str:
    return "\u2705 chat %s unsubscribed." % esc(chat_id)


def already_subscribed(chat_id: int) -> str:
    return "chat %s was already subscribed." % esc(chat_id)


def not_subscribed(chat_id: int) -> str:
    return "chat %s was not subscribed." % esc(chat_id)


# --------------------------------------------------------------------------
# setMyCommands payload — call once at startup
# --------------------------------------------------------------------------

BOT_COMMANDS = [
    ("m", "market order"),
    ("p", "pending order"),
    ("be", "SL to breakeven"),
    ("close", "close a position"),
    ("close_all", "close everything"),
    ("cancel_order", "cancel a pending order"),
    ("positions", "open positions"),
    ("orders", "working orders"),
    ("alert", "set a price alert"),
    ("ccalert", "candle close alert"),
    ("list", "active alerts"),
    ("help", "full command list"),
    ("subscribe", "allow this chat to use the bot"),
    ("unsubscribe", "remove this chat"),
]
