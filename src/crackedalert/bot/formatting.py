"""Telegram reply templates.

Kept text-compatible with the bash bot so nothing changes for the user.
One deliberate fix: the bash /cancel usage said "/cancel <ID>" which is
invalid under parse_mode=HTML (Telegram rejects unknown tags -- that
message silently never arrived). We say [ID] instead.
"""

from typing import Iterable, List, Optional

from ..alerts import (CANDLE_ABOVE, CANDLE_BELOW, CROSSING_UP, Alert,
                      CandleAlert)
from .. import version as bot_version


def alert_set(alert: Alert, live_price: float) -> str:
    relation = "lower" if alert.direction == CROSSING_UP else "higher"
    return ("cracked alert set (id: %s).\n"
            "%s\n"
            "%s\n\n"
            "Notes: %s\n"
            "current price (%s) is now %s than target."
            % (alert.id, alert.symbol, _trim(alert.target), alert.message,
               _trim(live_price), relation))


def alert_fired(alert: Alert) -> str:
    return ("cracked alert hit! (id:%s)\n"
            "price hits %s.\n"
            "notes: %s" % (alert.id, _trim(alert.target), alert.message))


def alert_usage() -> str:
    return "⚠️ <b>Usage:</b> /alert 2450.00 XAUUSD Approaching support"


def cancel_usage() -> str:
    return "⚠️ Usage: /cancel [ID]"


def cancelled(alert_id: str) -> str:
    return "alert %s cancelled." % alert_id


def cancel_not_found(alert_id: str) -> str:
    return "id %s not found or doesn't belong to you." % alert_id


def no_alerts() -> str:
    return "no active alerts."


def alert_list(alerts: List[Alert]) -> str:
    lines = ["active alerts:"]
    for a in alerts:
        lines.append("(%s)  %s @ %s - %s"
                     % (a.id, a.symbol, _trim(a.target), a.message))
    return "\n".join(lines)


def price_fetch_error(symbol: str) -> str:
    return "❌ API Error: Could not fetch live price for %s." % symbol


def account_not_found(account: str) -> str:
    return "error: account '%s' not found." % account


def help_text(account_codes: Iterable[str],
              balances: Optional[dict] = None) -> str:
    lines = [
        "cracked alert commands:",
        "",
        "market execution:",
        "/m [sl] [widen:y/n] [rr] [risk%] [account]",
        "example: /m 2440.00 y 2 0.5 10k",
        "",
        "pending execution:",
        "/p [entry] [sl] [widen:y/n] [rr] [risk%] [account]",
        "example: /p 2450.00 2455.00 n 3 1 5k",
        "",
        "position management:",
        "/close_all [account] — closes all positions",
        "/close [id] [account] — closes one position",
        "/cancel_order [id] [account] — cancels one pending order",
        "/be [account] — move SL to breakeven + spread",
        "",
        "set alert:",
        "/alert [target] [notes]",
        "example: /alert 2450.00 approaching demand",
        "",
        "candle close alerts:",
        "/ccalert [tf] [price] [above|below] [symbol] [notes]",
        "example: /ccalert M15 2450 above XAUUSD breakout",
        "/cclist — shows active candle alerts",
        "/cccancel [id] — deletes a candle alert",
        "",
        "utilities:",
        "/list — shows active alerts",
        "/cancel [id] — deletes alert",
        "/orders [account] — shows working orders",
        "/positions [account] — shows open positions",
        "/help — shows this message",
        "",
        "accounts: %s" % " | ".join(account_codes),
    ]
    if balances:
        parts = []
        for code in account_codes:
            bal = balances.get(code)
            parts.append("%s: %s" % (code, "?" if bal is None
                                     else _trim(bal)))
        lines.append("balances: %s" % " | ".join(parts))
    lines.append("cracked alert %s" % bot_version())
    return "\n".join(lines)


def order_success(ticket, symbol: str, direction: str, kind_label: str,
                  account: str, lots: float, risk_pct: float, risk_usd: float,
                  entry_label: str, sl: float, tp: float, rr: float,
                  widen_label: str, digits: int = 2,
                  dollar_risk: bool = False) -> str:
    if dollar_risk:
        risk_line = "lots: %.2f ($%.2f risk = %s%%)" % (
            lots, risk_usd, _trim(risk_pct))
    else:
        risk_line = "lots: %.2f (%s%% risk = $%.2f)" % (
            lots, _trim(risk_pct), risk_usd)
    return ("order placed (ticket: #%s)\n"
            "%s - %s %s (%s)\n"
            "%s\n\n"
            "entry: %s\n"
            "sl: %.*f%s\n"
            "tp: %.*f (1:%s RR)"
            % (ticket if ticket is not None else "?",
               symbol, direction, kind_label, account,
               risk_line,
               entry_label,
               digits, sl, widen_label,
               digits, tp, _trim(rr)))


def order_failed(symbol: str, direction: str, kind_label: str, account: str,
                 reason: str, retcode: str) -> str:
    return ("order failed (%s)\n"
            "%s - %s %s\n"
            "reason: %s (retcode: %s)"
            % (account, symbol, direction, kind_label, reason, retcode))


def entry_label_market(entry_ref: float, digits: int = 2) -> str:
    return "%.*f" % (digits, entry_ref)


def entry_label_pending(entry: float, placement: float,
                        digits: int = 2) -> str:
    return "%.*f (placed at %.*f)" % (digits, entry, digits, placement)


def trade_usage(is_market: bool) -> str:
    if is_market:
        return "⚠️ Usage: /m [sl] [widen:y/n] [rr] [risk%] [account]"
    return "⚠️ Usage: /p [entry] [sl] [widen:y/n] [rr] [risk%] [account]"


def positions_usage(is_positions: bool) -> str:
    cmd = "/positions" if is_positions else "/orders"
    return "⚠️ Usage: %s [account]" % cmd


def positions_error(account: str, reason: str) -> str:
    return "error: could not fetch for %s: %s" % (account, reason)


def positions_list(rows: List[dict], is_positions: bool) -> str:
    if not rows:
        return "no open positions." if is_positions else "no working orders."
    label = "open positions:" if is_positions else "working orders:"
    lines = [label]
    for r in rows:
        side = r.get("side") or "?"
        vol = r.get("volume")
        vol_text = "%.2f" % vol if vol is not None else "?"
        price = r.get("price")
        price_text = _trim(price) if price is not None else "?"
        sl = r.get("sl")
        tp = r.get("tp")
        sl_text = _trim(sl) if sl is not None else "-"
        tp_text = _trim(tp) if tp is not None else "-"
        extra = r.get("extra") or ""
        extra_text = " [%s]" % extra if extra else ""
        lines.append("(%s)  %s %s %s @ %s  sl:%s tp:%s%s"
                     % (r.get("id"), side, r.get("symbol"), vol_text,
                        price_text, sl_text, tp_text, extra_text))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# close_all / breakeven
# ----------------------------------------------------------------------
def close_all_usage() -> str:
    return "⚠️ Usage: /close_all [account]"


def close_all_result(account: str, results: List[dict]) -> str:
    if not results:
        return "no open positions."
    ok = sum(1 for r in results if r.get("ok"))
    lines = ["closed %d/%d positions (%s):"
             % (ok, len(results), account)]
    for r in results:
        vol = r.get("volume")
        vol_text = "%.2f" % vol if vol is not None else "?"
        status = "closed" if r.get("ok") else "failed: %s" % r.get("message")
        lines.append("(%s)  %s %s %s → %s"
                     % (r.get("id"), r.get("side"), r.get("symbol"),
                        vol_text, status))
    return "\n".join(lines)


def breakeven_usage() -> str:
    return "⚠️ Usage: /be [account]"


def close_usage() -> str:
    return "⚠️ Usage: /close [id] [account]"


def close_success(account: str, position_id: int) -> str:
    return "position %s closed (%s)." % (position_id, account)


def close_error(account: str, position_id: int, reason: str) -> str:
    return "close failed (%s): %s" % (account, reason)


def cancel_order_usage() -> str:
    return "⚠️ Usage: /cancel_order [id] [account]"


def cancel_order_success(account: str, order_id: int) -> str:
    return "order %s cancelled (%s)." % (order_id, account)


def cancel_order_error(account: str, order_id: int, reason: str) -> str:
    return "cancel failed (%s): %s" % (account, reason)


def breakeven_result(account: str, results: List[dict]) -> str:
    if not results:
        return "no open positions."
    ok = sum(1 for r in results if r.get("ok"))
    lines = ["breakeven set on %d/%d positions (%s):"
             % (ok, len(results), account)]
    for r in results:
        vol = r.get("volume")
        vol_text = "%.2f" % vol if vol is not None else "?"
        be = r.get("be_sl")
        if r.get("ok"):
            detail = "BE at %s" % _trim(be) if be is not None else "BE set"
        else:
            detail = "skipped — %s" % r.get("message")
            if be is not None:
                detail += " (needs %s)" % _trim(be)
        lines.append("(%s)  %s %s %s → %s"
                     % (r.get("id"), r.get("side"), r.get("symbol"),
                        vol_text, detail))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# candle close alerts
# ----------------------------------------------------------------------
def candle_alert_usage() -> str:
    return ("⚠️ <b>Usage:</b> /ccalert [tf] [price] [above|below] "
            "[symbol] [notes]\n"
            "timeframes: M1 M5 M15 M30 H1 H4 D1 W1 MN1")


def candle_alert_set(alert: CandleAlert, last_close: Optional[float]) -> str:
    direction = "above" if alert.direction == CANDLE_ABOVE else "below"
    line = ("cracked candle alert set (id: %s).\n"
            "%s %s\n"
            "close %s %s\n\n"
            "Notes: %s"
            % (alert.id, alert.symbol, alert.timeframe, direction,
               _trim(alert.target), alert.message))
    if last_close is not None:
        line += "\nlast closed close: %s" % _trim(last_close)
    return line


def candle_alert_fired(alert: CandleAlert) -> str:
    direction = "above" if alert.direction == CANDLE_ABOVE else "below"
    return ("cracked candle alert hit! (id:%s)\n"
            "%s %s closed %s %s.\n"
            "notes: %s"
            % (alert.id, alert.symbol, alert.timeframe, direction,
               _trim(alert.target), alert.message))


def candle_alert_list(alerts: List[CandleAlert]) -> str:
    if not alerts:
        return "no active candle alerts."
    lines = ["active candle alerts:"]
    for a in alerts:
        direction = "above" if a.direction == CANDLE_ABOVE else "below"
        lines.append("(%s)  %s %s close %s %s - %s"
                     % (a.id, a.symbol, a.timeframe, direction,
                        _trim(a.target), a.message))
    return "\n".join(lines)


def candle_cancel_usage() -> str:
    return "⚠️ Usage: /cccancel [ID]"


def candle_cancelled(alert_id: str) -> str:
    return "candle alert %s cancelled." % alert_id


def candle_cancel_not_found(alert_id: str) -> str:
    return "id %s not found or doesn't belong to you." % alert_id


def _trim(value: float) -> str:
    """Render 2450.0 as the user typed it: no trailing zeros beyond 2dp."""
    text = ("%.2f" % value).rstrip("0").rstrip(".")
    return text if text else "0"
