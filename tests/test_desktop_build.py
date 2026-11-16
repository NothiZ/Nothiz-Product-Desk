import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from openpyxl import load_workbook


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows PowerShell is required")
class DesktopBuildTests(unittest.TestCase):
    def test_windows_powershell_preserves_chinese_release_files(self):
        root = Path(__file__).resolve().parent.parent
        raw = (root / "build_desktop.ps1").read_bytes()
        source = raw.decode("utf-8-sig")
        header = source.split("$taskBrowserRoot =", 1)[0]
        header = header.replace("$taskRoot = $PSScriptRoot", "$taskRoot = '" + str(root).replace("'", "''") + "'")
        tail = source[source.index("$taskRelease = Join-Path"):]
        setup = "New-Item -ItemType Directory -Path (Join-Path $taskDistRoot 'ProductDetailsDesktop') -Force | Out-Null\n"
        debug = root / "debug"
        debug.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=debug) as folder:
            folder = Path(folder).resolve()
            self.assertTrue(folder.is_relative_to(root))
            script = folder / "release-tail.ps1"
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            script.write_bytes(bom + (header + setup + tail).encode("utf-8"))
            output = folder / "output"
            script_arg = str(script).replace("'", "''")
            output_arg = str(output.relative_to(root)).replace("'", "''")
            command = f"& ([scriptblock]::Create((Get-Content -LiteralPath '{script_arg}' -Raw))) -SkipZip -OutputRoot '{output_arg}'"
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            release = output / "ProductDetailsDesktop"
            self.assertEqual((release / "使用说明.txt").read_bytes(), (root / "DESKTOP_USAGE.txt").read_bytes())
            workbook = load_workbook(release / "输入模板.xlsx", read_only=True)
            try:
                self.assertEqual(workbook.sheetnames, ["商品链接"])
                self.assertEqual(workbook.active["A1"].value, "链接")
            finally:
                workbook.close()
