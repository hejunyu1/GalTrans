from __future__ import annotations

import json
import unittest
from pathlib import Path


class DesktopPackagingTests(unittest.TestCase):
    project_root = Path(__file__).resolve().parents[1]

    def test_tauri_packages_only_the_fixed_sidecar_without_an_installer(self) -> None:
        config = json.loads(
            (
                self.project_root
                / "desktop"
                / "src-tauri"
                / "tauri.conf.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(
            config["bundle"],
            {
                "active": False,
                "externalBin": ["binaries/galtrans-backend"],
            },
        )
        self.assertIn("build:sidecar", config["build"]["beforeDevCommand"])
        self.assertIn("build:sidecar", config["build"]["beforeBuildCommand"])

    def test_player_window_keeps_its_minimum_capabilities(self) -> None:
        capabilities = json.loads(
            (
                self.project_root
                / "desktop"
                / "src-tauri"
                / "capabilities"
                / "default.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(
            capabilities["permissions"],
            ["core:default", "dialog:allow-open"],
        )


if __name__ == "__main__":
    unittest.main()
