"""Build and run Planetarium on localhost with a persistent private SQLite index."""

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "makara-planetarium:local"


def main():
    os.umask(0o077)
    home = Path(os.environ.get("MAKARA_HOME", Path.home() / ".local/share/makara"))
    private = home / "planetarium"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    policy = (
        json.loads((home / "capture.json").read_text())
        if (home / "capture.json").is_file()
        else {}
    )
    users = [("Linux", Path.home())]
    if shutil.which("powershell.exe") and shutil.which("wslpath"):
        win = subprocess.check_output(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "[Environment]::GetFolderPath('UserProfile')",
            ],
            text=True,
        ).strip()
        users.append(
            (
                "Windows",
                Path(
                    subprocess.check_output(["wslpath", "-u", win], text=True).strip()
                ),
            )
        )
    volumes = [{"type": "bind", "source": str(private.resolve()), "target": "/data"}]
    sources = []
    for loc, user in users:
        for agent, rel in [
            ("Codex", ".codex/sessions"),
            ("Codex", ".codex/archived_sessions"),
            ("Claude", ".claude/projects"),
            ("OpenCode", ".local/share/opencode"),
        ]:
            root = user / rel
            if (
                not root.is_dir()
                or agent == "OpenCode"
                and not (root / "opencode.db").is_file()
            ):
                continue
            target = "/inputs/" + loc + "/" + rel
            volumes.append(
                {
                    "type": "bind",
                    "source": str(root.resolve()),
                    "target": target,
                    "read_only": True,
                }
            )
            sources.append(
                {
                    "agent": agent,
                    "location": loc,
                    "path": target + ("/opencode.db" if agent == "OpenCode" else ""),
                }
            )
    (private / "sources.json").write_text(
        json.dumps(
            {"sources": sources, "excluded_paths": policy.get("excluded_paths", [])},
            indent=2,
        )
    )
    compose = {
        "name": "makara-planetarium",
        "services": {
            "observatory": {
                "image": IMAGE,
                "restart": "unless-stopped",
                "user": f"{os.getuid()}:{os.getgid()}",
                "ports": ["127.0.0.1:18768:18768"],
                "volumes": volumes,
                "read_only": True,
                "tmpfs": ["/tmp:rw,nosuid,nodev,size=192m"],
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
            }
        },
    }
    spec = private / "compose.json"
    spec.write_text(json.dumps(compose, indent=2))
    subprocess.run(
        ["docker", "build", "-t", IMAGE, str(ROOT / "planetarium")], check=True
    )
    subprocess.run(
        ["docker", "compose", "-f", str(spec), "up", "-d", "--force-recreate"],
        check=True,
    )
    print("Makara Planetarium → http://localhost:18768")


if __name__ == "__main__":
    main()
