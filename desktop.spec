# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import ssl
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
datas = [(str(root / "build-resources" / "browsers"), "browsers"),
         (str(root / "tests" / "fixtures" / "alibaba_1601683518137.html"), "selftest")]
# This Python runtime loads a CA sidecar before any HTTPS connection is created.
ssl_sidecar = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
if ssl_sidecar.is_file():
    datas.append((str(ssl_sidecar), "."))
binaries, hiddenimports = [], []
# These libraries load browser code and fingerprint datasets dynamically.
for package in ("scrapling", "patchright", "playwright", "browserforge", "apify_fingerprint_datapoints"):
    package_data, package_binaries, package_imports = collect_all(package, include_py_files=False)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports

a = Analysis([str(root / "desktop_app.py")], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports + ["openpyxl", "desktop_worker", "login", "run"],
             excludes=["IPython", "pytest", "matplotlib", "scipy", "PyQt5", "PySide6"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ProductDetailsDesktop", debug=False,
          strip=False, upx=False, console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ProductDetailsDesktop")
