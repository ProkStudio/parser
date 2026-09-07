"""Windows MSI build; distribute only after reviewing third-party licenses."""
from pathlib import Path
from cx_Freeze import Executable, setup
from parser_app.domain import VERSION

setup(
    name="Parser", version=VERSION,
    description="Parser — Telegram desktop workspace", author="ProkStudio",
    executables=[Executable("desktop.py", base="gui", target_name="Parser.exe", icon="tools/parser.ico", shortcut_name="Parser", shortcut_dir="ParserProgramMenu")],
    options={
        "build_exe": {
            "packages": ["parser_app", "telethon", "socks", "webview", "pythonnet", "clr_loader", "opentele"],
            "includes": ["clr", "PyQt5.QtCore", "tgcrypto"],
            "excludes": ["tkinter", "unittest", "tests", "PIL", "PyQt5.QtWebEngineWidgets", "PyQt5.QtWidgets", "PyQt5.QtGui"],
            "include_files": [("parser_app/static", "lib/parser_app/static"), ("README.md", "README.md")],
            "include_msvcr": True, "zip_include_packages": [], "zip_exclude_packages": ["*"],
        },
        "bdist_msi": {
            "all_users": False, "add_to_path": False,
            "initial_target_dir": r"[LocalAppDataFolder]Programs\ProkStudio\Parser",
            "upgrade_code": "{D4ED0B35-FAB1-4650-88AE-0E813A264E70}",
            "product_name": "Parser", "output_name": f"Parser-{VERSION}-x64.msi",
            "install_icon": "tools/parser.ico", "launch_on_finish": True,
            "summary_data": {"author": "ProkStudio", "comments": "Local-only Telegram reader. Personal data retained on uninstall."},
            "data": {"Directory": [("ProgramMenuFolder", "TARGETDIR", "."), ("ParserProgramMenu", "ProgramMenuFolder", "Parser")]},
        },
    },
)
