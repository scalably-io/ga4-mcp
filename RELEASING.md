# Releasing

1. Bump `version` in `pyproject.toml`, `manifest.json`, `server.json` (all three), and `src/<module>/__init__.py`; add a CHANGELOG entry; keep `server.json` `description` at 100 characters or fewer (the registry rejects longer ones); run `pytest -q` and the scanner; commit.
2. Tag and push: `git tag v<version> && git push origin main --tags`. CI publishes the wheel to PyPI (trusted publishing, environment `pypi`) and attaches `<server>.mcpb` plus `<server>.mcpb.sha256` to the GitHub release.
3. Confirm: `pip index versions scalably-<server>` shows the version; the release page shows both assets.
4. Add or update the `mcpb` entry in `server.json`: `identifier` = the release asset URL, `fileSha256` = the 64 hex characters in the `.sha256` asset (no trailing newline). Commit and push.
5. Registry (local, maintainer's Mac only; on macOS use `/opt/homebrew/opt/openssl@3/bin/openssl`, the system LibreSSL has no Ed25519): `PRIVATE_KEY="$(openssl pkey -in ~/.config/scalably/mcp-registry/key.pem -noout -text | grep -A3 'priv:' | tail -n +2 | tr -d ' :\n')"` then `mcp-publisher login dns --domain scalably.io --private-key "$PRIVATE_KEY"` and `mcp-publisher publish`.
6. Confirm: `curl -s 'https://registry.modelcontextprotocol.io/v0.1/servers?search=io.scalably/<server>'` lists the version.
