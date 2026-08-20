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

UI_PATH = Path(__file__).resolve().parent.parent / "frontend" / "ui.html"


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


if __name__ == "__main__":
    unittest.main()