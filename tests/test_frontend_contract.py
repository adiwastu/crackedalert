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
TV_PATH = ROOT / "frontend" / "tv.html"


class TradingViewImportContractTest(unittest.TestCase):
    """frontend/tv.html: TradingView clipboard import -> /m /p command.

    The page must keep the clipboard parser, the doc's field extraction
    (entry/stopLevel/profitLevel), and the current --smart-sl <price> <tf>
    syntax so a future UI-only change can't silently regress them.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tv = TV_PATH.read_text(encoding="utf-8")

    def test_version_bumped(self) -> None:
        self.assertRegex(self.tv, r"v2\.0\.\d+")

    def test_clipboard_parser_present(self) -> None:
        self.assertIn("data-tradingview-clip", self.tv)
        self.assertIn("parseTradingViewClip", self.tv)
        self.assertIn("querySelector('[data-tradingview-clip]')", self.tv)

    def test_field_extraction_present(self) -> None:
        self.assertIn("points.0.price", self.tv)
        self.assertIn("stopLevel", self.tv)
        self.assertIn("profitLevel", self.tv)
        self.assertIn("LineToolRiskRewardShort", self.tv)

    def test_smart_sl_syntax_present(self) -> None:
        self.assertIn("--smart-sl", self.tv)
        self.assertIn("smart-tf", self.tv)
        self.assertIn("M30", self.tv)

    def test_builder_ids_present(self) -> None:
        self.assertIn('id="tv-trade-cmd"', self.tv)
        self.assertIn('id="tv-paste"', self.tv)
        self.assertIn('id="tv-symbol"', self.tv)

    def test_pending_only(self) -> None:
        # The drawing page is strictly for pending orders: no /m toggle.
        self.assertNotIn("seg-market", self.tv)
        self.assertNotIn("'/m'", self.tv)
        self.assertIn("'/p'", self.tv)

    def test_no_alert_builder(self) -> None:
        # Strictly a pending-order page: no /alert or /ccalert UI.
        self.assertNotIn("tv-alert", self.tv)
        self.assertNotIn("prefillAlert", self.tv)
        self.assertNotIn("note-presets", self.tv)
        self.assertNotIn("refreshAlert", self.tv)

    def test_prefills_present(self) -> None:
        # RR derived from the drawing, risk% from the clip, candle TF
        # prefilled for the CC guard.
        self.assertIn("setSelect('tv-rr'", self.tv)
        self.assertIn("sources.0.source.state.risk", self.tv)
        self.assertIn("Bot TP at RR", self.tv)
        self.assertIn("tv-cc-tf", self.tv)


class FrontendContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.ui = UI_PATH.read_text(encoding="utf-8")

    def test_version_bumped(self) -> None:
        # Sanity: the header version is a 3-part semver so bumping is
        # visible on the page itself.
        self.assertRegex(self.ui, r"v2\.0\.\d+")

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


if __name__ == "__main__":
    unittest.main()
