"""Install the freshly built wheel in an isolated env via uvx and list tools over stdio."""
import asyncio, glob, os, subprocess, sys, tomllib
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]
py = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
script = next(iter(py["scripts"]))
wheel = sorted(glob.glob(str(REPO / "dist" / "*.whl")))[-1]

async def run():
    params = StdioServerParameters(command="uvx", args=["--from", wheel, script], env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return sorted(t.name for t in (await s.list_tools()).tools)

names = asyncio.run(run())
readme = (REPO / "README.md").read_text().split("## Tools")[1].split("\n## ")[0]
expected = sorted(l.split("`")[1] for l in readme.splitlines() if l.startswith("| `"))
assert names == expected, (names, expected)
print(f"OK clean install: {script} from {os.path.basename(wheel)} lists {len(names)} tools")
