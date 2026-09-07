"""Windows-only MSI build. Run from repository root after generating guide images."""
from pathlib import Path
from cx_Freeze import Executable, setup

root = Path.cwd()
for name in ("connect", "collect", "jobs", "messages"):
    if not (root / "parser_app" / "static" / "guide" / (name + ".webp")).is_file():
        raise SystemExit("Generate the built-in guide screenshots first: node tests/browser.mjs")

setup(
    name="Parser",
    version="0.1.0",
    description="Parser — Telegram workspace",
    author="ProkStudio",
    executables=[Executable("desktop.py", base="gui", target_name="Parser.exe", icon="tools/parser.ico", shortcut_name="Parser", shortcut_dir="ParserProgramMenu")],
    options={
        "build_exe": {
            "packages": ["parser_app", "telethon"],
            "excludes": ["tkinter", "unittest", "tests", "PIL"],
            "include_files": [("parser_app/static", "lib/parser_app/static")],
            "include_msvcr": True,
            "zip_include_packages": [],
            "zip_exclude_packages": ["*"],
        },
        "bdist_msi": {
            "all_users": False,
            "add_to_path": False,
            "initial_target_dir": r"[LocalAppDataFolder]Programs\ProkStudio\Parser",
            "upgrade_code": "{D4ED0B35-FAB1-4650-88AE-0E813A264E70}",
            "product_name": "Parser",
            "output_name": "Parser-0.1.0-x64.msi",
            "install_icon": "tools/parser.ico",
            "launch_on_finish": True,
            "summary_data": {"author": "ProkStudio", "comments": "Local-first Telegram parser. Personal data is retained when uninstalling."},
            "data": {"Directory": [("ProgramMenuFolder", "TARGETDIR", "."), ("ParserProgramMenu", "ProgramMenuFolder", "Parser")]},
        },
    },
)
