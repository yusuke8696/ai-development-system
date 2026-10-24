"""Build a self-contained Windows folder distribution."""
import os
import subprocess
import shutil
from importlib import metadata
import sys
from pathlib import Path
from build_runtime import ssl_support_data

root = Path(__file__).resolve().parents[1]
os.environ["PYINSTALLER_CONFIG_DIR"] = str(root / ".runtime/pyinstaller-cache")
command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "AIWorkspace",
    "--add-data", "ai_system/web;ai_system/web", "--add-data", "ai_system/bundled;ai_system/bundled",
    "--collect-all", "uvicorn"]
for source, destination in ssl_support_data():
    command.extend(["--add-data", source + ";" + destination])
subprocess.run(command + ["launcher.py"], cwd=root, check=True)
for distribution in metadata.distributions():
    for file in distribution.files or []:
        if "license" in file.name.lower() or "copying" in file.name.lower():
            source = Path(distribution.locate_file(file))
            if source.is_file() and source.stat().st_size < 1000000:
                destination = root / "dist/AIWorkspace/licenses" / distribution.metadata["Name"] / file.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
shutil.copyfile(root / "README.md", root / "dist/AIWorkspace/README.md")
if (root / "docs").exists():
    shutil.copytree(root / "docs", root / "dist/AIWorkspace/docs", dirs_exist_ok=True)
