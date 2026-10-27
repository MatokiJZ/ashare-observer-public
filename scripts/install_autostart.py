"""Install an opt-in macOS LaunchAgent for this workspace; print before installing."""
import argparse
import os
import plistlib
import subprocess
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--install", action="store_true")
parser.add_argument("--remove", action="store_true")
args = parser.parse_args()
label = "local.ashare.observer"
path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
installed = Path.home() / "Library/Application Support/AshareObserver"
logs = installed / "logs"
payload = {"Label": label, "ProgramArguments": [str(installed / ".venv/bin/python"), "-m", "ashare.cli", "serve"],
           "WorkingDirectory": str(installed), "RunAtLoad": True, "KeepAlive": True,
           "ThrottleInterval": 30, "StandardOutPath": str(logs / "service.log"), "StandardErrorPath": str(logs / "error.log"),
           "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": str(installed / "backend"), "ASHARE_DATA_DIR": str(installed / "data")}}
if args.remove:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False)
    path.unlink(missing_ok=True)
    print("已移除开机登录自动启动")
elif args.install:
    # Standard per-user application location: a LaunchAgent must not depend on
    # interactive access to the protected Documents folder.
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    installed.mkdir(parents=True, exist_ok=True)
    for name in ["backend", "frontend/dist", ".venv"]:
        if name == ".venv" and (installed / name).exists():
            continue
        shutil.copytree(root / name, installed / name, dirs_exist_ok=True, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    for pth in (installed / ".venv/lib").glob("python*/site-packages/__editable__.ashare_observer-*.pth"):
        pth.write_text(str(installed / "backend") + "\n")
    shutil.copy2(root / "config.json", installed / "config.json")
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    (installed / ".revision").write_text(revision.stdout.strip() if revision.returncode == 0 else "workspace")
    if not (installed / "data").exists() and (root / "data").exists():
        shutil.copytree(root / "data", installed / "data", symlinks=True)
    # A runtime marker lets workspace CLI commands use the same account, avoiding two ledgers.
    (root / ".runtime-path").write_text(str(installed))
    path.parent.mkdir(parents=True, exist_ok=True)
    logs.mkdir(exist_ok=True)
    path.write_bytes(plistlib.dumps(payload))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    print(f"已安装：{path}")
else:
    print(plistlib.dumps(payload).decode())
    print("使用 --install 安装，--remove 移除。")
