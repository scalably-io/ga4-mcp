import re
import subprocess
import pytest
from conftest import REPO

# Literals are split so this file does not match itself or the private scanner.
FORBIDDEN = [
    "/" + "workspace" + "/",
    "/opt/" + "scalably",
    "/opt/" + "tools",
    "TOOLS_" + "ROOT",
    "tool-outcome" + "/v1",
    "mcp__" + "scalably",
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    r"[A-Za-z0-9]{30,}",
    "gserviceaccount" + r"\.com",
]
SUFFIXES = {".md", ".py", ".sh", ".json", ".toml", ".txt", ".yml", ".yaml", ""}
TRACKED = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True).stdout.decode().split("\0")
FILES = [REPO / f for f in TRACKED if f and (REPO / f).is_file() and (REPO / f).suffix in SUFFIXES]


def _exempt(line: str) -> bool:
    # The release runbook writes the bundle digest into server.json; a sha256 is not a secret.
    return "fileSha256" in line


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_no_forbidden_patterns(path):
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if _exempt(line):
            continue
        for pat in FORBIDDEN:
            m = re.search(pat, line)
            if m:
                pytest.fail(f"{path.relative_to(REPO)}:{number} matches {pat!r}: {m.group(0)[:60]}")


def test_tracked_files_enumerated():
    assert any(p.name == "server.json" for p in FILES) and any(p.suffix == ".py" for p in FILES)
