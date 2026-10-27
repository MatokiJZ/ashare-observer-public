"""Interactive, local-only setup. Never pass the credential in command arguments."""
import getpass
import json
import os
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
repository = json.loads((root / "config.json").read_text())["github_repo"]
print(f"在 GitHub 创建 fine-grained token，仅选择 {repository}，Contents 与 Actions 权限均为 Read-only。")
print("凭据仅保存到本机，不要发送到聊天。输入时屏幕不显示字符。")
value = getpass.getpass("专用只读凭据：").strip()
if not value.startswith("github_pat_"):
    raise SystemExit("需要 GitHub fine-grained token，不接受传统全局凭据。")
with httpx.Client(headers={"Authorization": f"Bearer {value}", "Accept": "application/vnd.github+json"}, timeout=20) as client:
    for suffix in ["", "/actions/artifacts?per_page=1"]:
        response = client.get(f"https://api.github.com/repos/{repository}{suffix}")
        if response.status_code != 200:
            raise SystemExit(f"读取权限验证失败（HTTP {response.status_code}），未保存凭据。")
folder = Path.home() / ".config/ashare-observer"
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
folder.chmod(0o700)
path = folder / "github-token"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as stream:
    os.fchmod(stream.fileno(), 0o600)
    stream.write(value)
print("已保存并验证读取权限，下一次同步自动使用。权限范围仍以 GitHub 创建页面设置为准。")
