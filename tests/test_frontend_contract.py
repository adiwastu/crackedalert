"""Contract test for frontend/ui.html vs the backend command grammar.

The bot accepts a trailing --all flag on /alert and /ccalert to broadcast
to all subscribed chats, and an exact --smart-sl <price> on /m and /p (the
smart-SL price replaced the old x<mult> token). These tests ensure the
static UI still emits those tokens, so a future UI-only change can't
silently regress them.

The html is a static single-file app (no build step), so the easiest
robust check is string-level assertions on the relevant JS/markup.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_PATH = ROOT / "frontend" / "ui.html"


class FrontendContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.ui = UI_PATH.read_text(encoding="utf-8")

    def test_version_token_present(self) -> None:
        # The header carries a __VERSION__ token; deploy_v2.sh stamps the
        # deployed bot's version into the served copy, so the page always
        # shows the real version without manual UI edits.
        self.assertIn("__VERSION__", self.ui)

    def test_broadcast_toggles_present(self) -> None:
        # Alert-all broadcasts are exposed as checkboxes in the UI.
        self.assertIn("pa-all", self.ui)
        self.assertIn("ca-all", self.ui)
        self.assertIn("S.priceBroadcast=this.checked", self.ui)
        self.assertIn("S.candleBroadcast=this.checked", self.ui)

    def test_state_has_broadcast_flags(self) -> None:
        self.assertIn("priceBroadcast:false", self.ui)
        self.assertIn("candleBroadcast:false", self.ui)

    def test_add_price_alert_appends_all(self) -> None:
        self.assertIn("S.priceBroadcast?' --all':''", self.ui)

    def test_add_candle_alert_appends_all(self) -> None:
        self.assertIn("S.candleBroadcast?' --all':''", self.ui)

    def test_live_previews_append_all(self) -> None:
        # Unlike the old page, priceCmd/candleCmd are the single source for
        # both the preview and the copied value, so the --all flag must
        # appear in the same builder used by both.
        self.assertIn("S.priceBroadcast?' --all':''", self.ui)
        self.assertIn("S.candleBroadcast?' --all':''", self.ui)

    def test_smart_sl_field_present(self) -> None:
        # Bug was: the only smart-SL control was a opaque x-multiplier. The
        # minimal UI exposes an exact smart-SL price input instead.
        self.assertIn('id="smart-sl"', self.ui)

    def test_smart_sl_flag_emitted_on_trade(self) -> None:
        self.assertIn("base.push('--smart-sl',ss,$('smart-tf').value)",
                      self.ui)
        self.assertIn("const ss=$('smart-sl').value", self.ui)

    def test_smart_sl_timeframe_select_present(self) -> None:
        # v2.0.32: the soft candle-close stop needs a designated timeframe;
        # M5 is the default selection.
        self.assertIn('id="smart-tf"', self.ui)
        self.assertIn("<option selected>M5</option>", self.ui)

    def test_smart_sl_risk_readout_present(self) -> None:
        # The UI shows the risk at the smart stop (pending orders) or says
        # the bot reports it (market orders where the fill is unknown).
        self.assertIn("Risk at smart SL:", self.ui)
        self.assertIn("bot reports it", self.ui)

    def test_no_multiplier_control_remains(self) -> None:
        # The x-multiplier spelling was removed from the bot and the UI.
        self.assertNotIn("Lot multiplier", self.ui)
        self.assertNotIn("adj('mult'", self.ui)
        self.assertNotIn("S.mult", self.ui)
        self.assertNotIn("x-mult", self.ui)

    def test_notes_presets_datalist_present(self) -> None:
        # Notes fields are type-or-pick: a datalist offers the four presets
        # while still allowing free text.
        self.assertIn('id="note-presets"', self.ui)
        self.assertIn('<option value="ChoCh">', self.ui)
        self.assertIn('<option value="BoS">', self.ui)
        self.assertIn('<option value="LTF ChoCh">', self.ui)
        self.assertIn('<option value="LTF BoS">', self.ui)
        self.assertIn('list="note-presets"', self.ui)

    def test_builders_all_present(self) -> None:
        # All three builders must remain: trade (/m,/p), price alert, candle alert.
        self.assertIn('id="trade-cmd"', self.ui)
        self.assertIn('id="palrt-cmd"', self.ui)
        self.assertIn('id="calrt-cmd"', self.ui)

    def test_orders_panel_present(self) -> None:
        # v2.0.56: the UI lists working orders from the bot's read-only
        # GET /orders endpoint, with a per-row copy-id button for
        # /ocancel + /cancel_order.
        self.assertIn('id="orders-n"', self.ui)
        self.assertIn('id="orders-list"', self.ui)
        self.assertIn("function loadOrders", self.ui)
        self.assertIn("'/orders?token='", self.ui)
        self.assertIn("copy('+r.id+',this)", self.ui)
        self.assertIn("function renderOrders", self.ui)

    def test_token_stamp_placeholder(self) -> None:
        # v2.0.58: deploy_v2.sh stamps ALERT_STATUS_TOKEN over __TOKEN__
        # in the served copy, so the page never prompts for it. The repo
        # file must keep the placeholder so stamping is possible.
        self.assertIn("__TOKEN__", self.ui)
        self.assertIn("function ordersToken", self.ui)

    def test_ocancel_layout_present(self) -> None:
        # v2.0.59: third builder layout -- set/amend the cancel condition
        # on an existing unfilled order via /ocancel.
        self.assertIn('id="form-ocancel"', self.ui)
        self.assertIn('id="oc-btn"', self.ui)
        self.assertIn('id="oc-id"', self.ui)
        self.assertIn('id="oc-price"', self.ui)
        self.assertIn("function ocancelCmd", self.ui)
        self.assertIn("'/ocancel '", self.ui)
        self.assertIn("function amendCancel", self.ui)

    def test_orders_panel_shows_cancel_level(self) -> None:
        # Orders rows tag active cancel-condition watches (cancel_level
        # from GET /orders) and offer an amend shortcut into the /ocancel
        # layout.
        self.assertIn("cancel_level", self.ui)
        self.assertIn("amendCancel(", self.ui)
        self.assertIn("r.cancel_level!=null", self.ui)

    def test_no_tradingview_page_reference(self) -> None:
        # The TradingView import page was deleted (too much upkeep for its
        # use); the UI must not link to /tv.html anymore.
        self.assertNotIn("tv.html", self.ui)

    def test_cancel_condition_field_present(self) -> None:
        # v2.0.57: the pending-order builder exposes the --cancel condition
        # (cancel the unfilled order if price touches the level pre-fill).
        self.assertIn('id="cancel-price"', self.ui)
        self.assertIn("Cancel if hits", self.ui)

    def test_cancel_condition_flag_emitted_on_pending(self) -> None:
        # Only the /p branch emits --cancel (market orders reject it), and
        # only when the user typed a level.
        self.assertIn("base.push('--cancel',cc)", self.ui)
        self.assertIn("const cc=$('cancel-price').value", self.ui)

    def test_cancel_condition_in_readout(self) -> None:
        self.assertIn("cancel if price hits ", self.ui)
        self.assertIn("S.orderType==='pending'&&!isNaN(cc)", self.ui)


if __name__ == "__main__":
    unittest.main()
