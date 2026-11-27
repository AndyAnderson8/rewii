# PyInstaller spec: builds a single-file `rewii` executable for the current OS.
#   uv run pyinstaller packaging/rewii.spec --noconfirm
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = collect_data_files("rewii", includes=["assets/**/*"])
binaries = []
hidden = ["rewii.tui.panes." + m for m in
          ("home", "drive", "games", "channels", "homebrew", "saves", "backups", "system", "settings")]
for pkg in ("textual", "libWiiPy", "rich"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h

a = Analysis(["rewii_entry.py"], pathex=["../src"], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=["tkinter", "pytest"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="rewii", console=True,
          upx=False, strip=False, icon=None)
