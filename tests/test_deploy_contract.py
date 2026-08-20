"""Contract test for the Caddy-based frontend hosting.

deploy_v2.sh step 6 serves the static UI (/var/www/crackedalert-ui) through
Caddy, which already owns :80 on the VPS (fronting geararea at :8080). This
test guards that hosting setup so a future change can't silently regress it
back to the clashing nginx approach or reintroduce the %REPO_DIR% path bug.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CADDY_UI_PATH = REPO_ROOT / "deploy" / "caddy-ui.caddyfile"
DEPLOY_SCRIPT = REPO_ROOT / "deploy_v2.sh"


class DeployContractTest(unittest.TestCase):

    def test_caddy_ui_block_exists(self) -> None:
        self.assertTrue(
            CADDY_UI_PATH.exists(),
            "deploy/caddy-ui.caddyfile must exist for deploy_v2.sh step 6",
        )
        text = CADDY_UI_PATH.read_text(encoding="utf-8")
        # Collect only top-level site-block hosts so the subdomain prefix
        # (alert.hotland3x3.my.id) can't false-positive/miss a legacy block.
        hosts = [
            line.split()[0]
            for line in text.splitlines()
            if line.strip().endswith("{")
            and not line.startswith(("\t", " "))
        ]
        self.assertEqual(hosts, ["alert.hotland3x3.my.id"])
        self.assertIn("/var/www/crackedalert-ui", text)

    def test_no_repo_dir_placeholder_in_deploy_config(self) -> None:
        # The old nginx config was rendered from %REPO_DIR%, which pointed at
        # the repo checkout under /root and could 404 after re-provisioning.
        # The Caddy config must use a fixed path instead.
        for path in (CADDY_UI_PATH, DEPLOY_SCRIPT):
            if path.exists():
                self.assertNotIn(
                    "%REPO_DIR%",
                    path.read_text(encoding="utf-8"),
                    f"{path} must not contain the %REPO_DIR% placeholder",
                )

    def test_no_nginx_in_deploy_script(self) -> None:
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("apt-get install nginx", text)
        self.assertNotIn("systemctl enable nginx", text)
        self.assertNotIn("sites-enabled/crackedalert-ui", text)

    def test_deploy_script_wires_caddy(self) -> None:
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/var/www/crackedalert-ui", text)
        self.assertIn("deploy/caddy-ui.caddyfile", text)
        self.assertIn("systemctl reload caddy", text)

    def test_deploy_script_uses_subdomain_and_migrates_legacy(self) -> None:
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("site alert.hotland3x3.my.id (crackedalert-ui)", text)
        self.assertIn("alert.hotland3x3.my.id/ui.html", text)
        # The legacy root-domain block must be removed, not left duplicate.
        self.assertIn("Migrating legacy UI site block", text)
        self.assertIn("site hotland3x3.my.id (crackedalert-ui)", text)


if __name__ == "__main__":
    unittest.main()