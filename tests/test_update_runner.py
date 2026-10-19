import unittest
from pathlib import Path

from ui import _windows_update_runner_script


class WindowsUpdateRunnerTests(unittest.TestCase):
    def test_runner_waits_for_old_app_and_reopens_installed_copy(self):
        script = _windows_update_runner_script(
            Path("C:/Users/O'Brien/AppData/Local/Temp/AutoSNS-Setup-1.0.8.exe"),
            Path("C:/Users/O'Brien/AppData/Local/Temp/AutoSNS-Setup-1.0.8.install.log"),
            Path("C:/Users/O'Brien/AppData/Local/Programs/AutoSNS/AutoSNS.exe"),
            1234,
        )

        self.assertIn("while (Get-Process -Id $oldProcessId", script)
        self.assertIn("'/SILENT'", script)
        self.assertIn("Get-Process -Name $processName", script)
        self.assertIn("Start-Process -FilePath $targetPath", script)
        self.assertIn("O''Brien", script)


if __name__ == "__main__":
    unittest.main()
