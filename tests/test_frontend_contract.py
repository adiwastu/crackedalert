"""Contract test for frontend/ui.html vs the backend command grammar.

The bot accepts a trailing --all flag on /alert and /ccalert to broadcast
to all subscribed chats (_pop_broadcast in bot/handlers.py). This test
ensures the static UI still emits that flag, so a future UI-only change
can't silently regress the "alert all subscribers" feature.

The html is a static single-file app (no build step), so the easiest
robust check is string-level assertions on the relevant JS.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_PATH = ROOT / "frontend" / "ui.html"
BUILDER_PATH = ROOT / "command_builder.html"


class FrontendContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.ui = UI_PATH.read_text(encoding="utf-8")

    def test_version_bumped(self) -> None:
        # Sanity: the header version is a 3-part semver so bumping is
        # visible on the page itself.
        self.assertRegex(self.ui, r"v2\.0\.\d+")

    def test_broadcast_toggles_present(self) -> None:
        self.assertIn('onclick="setPriceBroadcast(false)"', self.ui)
        self.assertIn('onclick="setPriceBroadcast(true)"', self.ui)
        self.assertIn('onclick="setCandleBroadcast(false)"', self.ui)
        self.assertIn('onclick="setCandleBroadcast(true)"', self.ui)

    def test_state_has_broadcast_flags(self) -> None:
        self.assertIn("priceBroadcast: false", self.ui)
        self.assertIn("candleBroadcast: false", self.ui)

    def test_add_price_alert_appends_all(self) -> None:
        self.assertIn("const all   = S.priceBroadcast ? ' --all' : '';", self.ui)
        self.assertIn("${notes ? ' ' + notes : ''}${all}", self.ui)

    def test_add_candle_alert_appends_all(self) -> None:
        self.assertIn("const all  = S.candleBroadcast ? ' --all' : '';", self.ui)
        self.assertIn("${note ? ' ' + note : ''}${all}", self.ui)

    def test_live_previews_append_all(self) -> None:
        # The "Will copy" previews must reflect the toggle too.
        self.assertIn("${S.priceBroadcast ? ' --all' : ''}", self.ui)
        self.assertIn("${S.candleBroadcast ? ' --all' : ''}", self.ui)

    def test_mult_control_present_in_ui(self) -> None:
        # Bug 3: the UI must expose the "Smart SL in-between" multiplier
        # so traders can emit x2..x4 on /m and /p.
        self.assertIn("Lot multiplier", self.ui)
        self.assertIn("adj('mult'", self.ui)
        self.assertIn("S.mult > 1", self.ui)
        self.assertIn("'x' + S.mult", self.ui)

    def test_mult_control_present_in_builder(self) -> None:
        builder = BUILDER_PATH.read_text(encoding="utf-8")
        self.assertIn("Lot multiplier", builder)
        self.assertIn("adj('mult'", builder)
        self.assertIn("S.mult > 1", builder)
        self.assertIn("parts.push('x' + S.mult)", builder)
        self.assertIn("cmd += ` x${S.mult}`", builder)


if __name__ == "__main__":
    unittest.main()