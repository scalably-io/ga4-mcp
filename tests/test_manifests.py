import ast, json, re, tomllib
from conftest import REPO

def _tool_names():
    src = (REPO / "src").glob("*/server.py")
    tree = ast.parse(next(src).read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                target = d.func if isinstance(d, ast.Call) else d
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.append(node.name)
    return names

def test_manifest_tools_match_server():
    manifest = json.loads((REPO / "manifest.json").read_text())
    assert [t["name"] for t in manifest["tools"]] == _tool_names()

def test_versions_and_names_agree():
    py = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    manifest = json.loads((REPO / "manifest.json").read_text())
    server = json.loads((REPO / "server.json").read_text())
    assert py["version"] == manifest["version"] == server["version"]
    pypi = [p for p in server["packages"] if p["registryType"] == "pypi"][0]
    assert pypi["identifier"] == py["name"] and pypi["version"] == py["version"]
    assert py["name"] in py["scripts"], "console script must be named after the distribution so `uvx <name>` runs"
    assert manifest["manifest_version"] == "0.4"
    assert server["$schema"] == "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
    assert server["name"].startswith("io.scalably/")
    assert len(server["description"]) <= 100, "registry rejects description > 100 chars (expected length <= 100)"

def test_readme_carries_registry_marker_and_no_em_dash():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    server = json.loads((REPO / "server.json").read_text())
    assert re.search(rf"^<!-- mcp-name: {re.escape(server['name'])} -->$", readme, re.M)
    assert "—" not in readme

def test_secrets_marked_sensitive():
    """Every user_config key wired to a credential-looking env var is sensitive, and nothing else is."""
    manifest = json.loads((REPO / "manifest.json").read_text())
    env = manifest["server"]["mcp_config"]["env"]
    words = ("CREDENTIAL", "KEY", "TOKEN", "SECRET", "PASSWORD")
    expected = set()
    for var, value in env.items():
        m = re.fullmatch(r"\$\{user_config\.([a-z0-9_]+)\}", value)
        assert m, f"{var} must map to a user_config key, got {value!r}"
        assert m.group(1) in manifest["user_config"], m.group(1)
        if any(w in var.upper() for w in words):
            expected.add(m.group(1))
    sensitive = {k for k, cfg in manifest["user_config"].items() if cfg.get("sensitive") is True}
    assert sensitive == expected, f"sensitive={sorted(sensitive)} expected={sorted(expected)}"
