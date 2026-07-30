"""Telegram reply templates.

Kept text-compatible with the bash bot so nothing changes for the user.
One deliberate fix: the bash /cancel usage said "/cancel <ID>" which is
invalid under parse_mode=HTML (Telegram rejects unknown tags -- that
message silently never arrived). We say [ID] instead.
"""

from typing import Iterable, List

from ..alerts import CROSSING_UP, Alert


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


def help_text(account_codes: Iterable[str]) -> str:
    return ("cracked alert commands:\n\n"
            "market execution:\n"
            "/m [sl] [widen:y/n] [rr] [risk%%] [account]\n"
            "example: /m 2440.00 y 2 0.5 10k\n\n"
            "pending execution:\n"
            "/p [entry] [sl] [widen:y/n] [rr] [risk%%] [account]\n"
            "example: /p 2450.00 2455.00 n 3 1 5k\n\n"
            "set alert:\n"
            "/alert [target] [notes]\n"
            "example: /alert 2450.00 approaching demand\n\n"
            "utilities:\n"
            "/list — shows active alerts\n"
            "/cancel [id] — deletes alert\n"
            "/help — shows this message\n\n"
            "accounts: %s" % " | ".join(account_codes))


def order_success(ticket, symbol: str, direction: str, kind_label: str,
                  account: str, lots: float, risk_pct: float, risk_usd: float,
                  entry_label: str, sl: float, tp: float, rr: float,
                  widen_label: str, digits: int = 2) -> str:
    return ("order placed (ticket: #%s)\n"
            "%s - %s %s (%s)\n"
            "lots: %.2f (%s%% risk = $%.2f)\n\n"
            "entry: %s\n"
            "sl: %.*f%s\n"
            "tp: %.*f (1:%s RR)"
            % (ticket if ticket is not None else "?",
               symbol, direction, kind_label, account,
               lots, _trim(risk_pct), risk_usd,
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


def _trim(value: float) -> str:
    """Render 2450.0 as the user typed it: no trailing zeros beyond 2dp."""
    text = ("%.2f" % value).rstrip("0").rstrip(".")
    return text if text else "0"
