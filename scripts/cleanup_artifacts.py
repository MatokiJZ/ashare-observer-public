import os
from ashare.cloud import GitHub

gh = GitHub(os.environ["GITHUB_REPOSITORY"])
items = gh.api("actions/artifacts?per_page=100").get("artifacts", [])
checkpoints = sorted([a for a in items if a["name"].startswith("checkpoint-") and not a.get("expired")], key=lambda a: a["created_at"], reverse=True)
# Delete only this app's named checkpoint artifacts after a new artifact was uploaded.
for artifact in checkpoints[2:]:
    gh.api(f"actions/artifacts/{artifact['id']}", "DELETE")
print(f"保留 {min(2, len(checkpoints))} 份成功检查点")
