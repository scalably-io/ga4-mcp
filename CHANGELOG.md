# Changelog

## 1.0.0

- First public release. Derived from `container/tools/ga4-mcp/server.py` at `3122d1c8` (2026-08-31) in the private ScalablyAI repository. Changes from production: the private tool-outcome envelope is replaced by a plain JSON reply, tool annotations added (title, readOnlyHint, openWorldHint) on all 17 tools, no functional change.
- Tool titles derived from each tool's first docstring sentence.
- README Setup section rewritten as the public gsc-mcp pattern adapted for GA4 (enable the Data and Admin APIs, create a service account, add it as a Viewer on each property); the production readme's service-account-email example and server alias are not carried over.
- One long-identifier docstring example (a GA4 access-report timestamp dimension) dropped from `ga4_run_access_report`'s docstring; the remaining two examples (`userEmail`, `accessedPropertyName`) are unaffected and the full dimension list is discoverable via `ga4_get_metadata`.
