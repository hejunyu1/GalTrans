from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class WindowsPowerShellCompatibilityTests(unittest.TestCase):
    project_root = Path(__file__).resolve().parents[1]

    def test_scripts_use_utf8_bom_for_windows_powershell_51(self) -> None:
        for script_name in (
            "build-desktop-sidecar.ps1",
            "galtrans.ps1",
            "galtrans-gui.ps1",
            "test.ps1",
        ):
            with self.subTest(script=script_name):
                data = (self.project_root / "scripts" / script_name).read_bytes()
                self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
                data.decode("utf-8-sig", errors="strict")

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is unavailable")
    def test_launcher_runs_in_windows_powershell_51(self) -> None:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.project_root / "scripts" / "galtrans.ps1"),
                "doctor",
            ],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Status:   OK", result.stdout)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is unavailable")
    def test_gui_launcher_checks_environment_in_windows_powershell_51(self) -> None:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.project_root / "scripts" / "galtrans-gui.ps1"),
                "--check",
            ],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GalTrans GUI 0.4.2", result.stdout)
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
