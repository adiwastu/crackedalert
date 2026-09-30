"""The Telegram command menu and the handlers behind it must agree."""

import unittest
from unittest import mock

from crackedalert.bot import formatting as fmt
from crackedalert.bot.handlers import Handlers


class FakeApp:
    """Collects handlers the way telegram's Application would."""

    def __init__(self):
        self.commands = set()

    def add_handler(self, handler):
        self.commands |= set(handler.commands)


def registered():
    app = FakeApp()
    Handlers(mock.Mock(), mock.Mock(), mock.Mock(), "XAUUSD").register(app)
    return app.commands


class CommandRegistrationTests(unittest.TestCase):

    def test_the_bulk_cancels_are_registered(self):
        self.assertIn("cancellast", registered())
        self.assertIn("cancelall", registered())

    def test_every_menu_entry_has_a_handler(self):
        # A command in the menu that nothing answers looks broken to the
        # user, and nothing else guards against it.
        missing = [name for name, _ in fmt.BOT_COMMANDS
                   if name not in registered()]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
