# Google Analytics 4 MCP

Read-only MCP server for Google Analytics 4. Seventeen tools cover the read surface of both the Data API and the Admin API: standard, batch, pivot, realtime and funnel reports; the metadata catalog and compatibility check; and admin lists for properties, streams, custom dimensions and metrics, key events, audiences, Google Ads links, annotations and access reports. We run this server in production for every analytics client.

<!-- mcp-name: io.scalably/ga4-mcp -->

## Install

Claude Code:

```bash
claude mcp add ga4 -e GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json -- uvx scalably-ga4-mcp
```

Codex:

```bash
codex mcp add ga4 --env GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json -- uvx scalably-ga4-mcp
```

Claude Desktop: download `ga4-mcp.mcpb` from the latest GitHub release and open it.

## Setup

1. In Google Cloud, create or pick a project and enable the Google Analytics Data API and the Google Analytics Admin API on it (APIs and Services, Library).
2. Create a service account in that project and download its JSON key.
3. Add the service account's email as a Viewer on each GA4 property you want to query (Admin, Property access management).

No OAuth consent screen is needed; the server authenticates as the service account with the `analytics.readonly` scope. One service account can read any property that grants it access, so there is no per-client configuration beyond step 3 above.

Call `ga4_list_account_summaries` first to discover which `property_id` values the service account can see, then pass one into the reporting or admin tools.

## Tools (17)

| Tool | What it does |
|---|---|
| `ga4_list_account_summaries` | List every GA4 account and its child properties accessible to this service account |
| `ga4_get_property_details` | Full metadata for a single GA4 property |
| `ga4_list_data_streams` | List all data streams (web, iOS, Android) on a GA4 property |
| `ga4_list_custom_dimensions` | List custom dimensions configured on a GA4 property |
| `ga4_list_custom_metrics` | List custom metrics configured on a GA4 property |
| `ga4_list_key_events` | List key events (formerly conversions) on a GA4 property |
| `ga4_list_audiences` | List audiences defined on a GA4 property |
| `ga4_list_google_ads_links` | List Google Ads links attached to a GA4 property |
| `ga4_list_property_annotations` | List reporting annotations on a GA4 property |
| `ga4_run_access_report` | Audit log of who read what on a GA4 property, last 12 months |
| `ga4_get_metadata` | Fetch the full GA4 dimension and metric catalog for a property |
| `ga4_run_report` | Run a GA4 standard report, the workhorse tool |
| `ga4_batch_run_reports` | Run up to 5 GA4 reports in a single round trip |
| `ga4_run_pivot_report` | Run a GA4 pivot report |
| `ga4_run_realtime_report` | Run a GA4 real-time report covering the last 30 minutes |
| `ga4_check_compatibility` | Validate whether a dimension and metric combo can be queried together |
| `ga4_run_funnel_report` | Run a GA4 funnel report (v1alpha, drop-off analysis) |

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | yes | Path to a Google service-account JSON file with the `analytics.readonly` scope; share each property with the service account email |
| `GA4_LOG_LEVEL` | no | INFO (default) or DEBUG |

## Filter expression shape

Every tool accepting `dimension_filter` / `metric_filter` takes a dict:

```json
{"filter": {"field_name": "country", "string_filter": {"value": "US"}}}
```

Compose with boolean groups:

```json
{"and_group": {"expressions": [
  {"filter": {"field_name": "country", "string_filter": {"value": "US"}}},
  {"filter": {"field_name": "deviceCategory", "string_filter": {"value": "mobile"}}}
]}}
```

A raw `{"field_name": ..., "string_filter": ...}` is auto-wrapped into `{"filter": {...}}`.

## Reply shape

Every tool returns JSON with `status` (`succeeded`, `partial`, `no_op`), `operation`, `summary`, `target`, `result`, `proof`, `warnings`, `recovery`. Failures surface as a tool error whose text is `ga4_request_failed: <message> <hint>`. `proof.nextOffset` on a `partial` status means: continue from that offset. `proof.propertyQuota` carries the property's remaining Data API quota so the caller can self-throttle. `proof.dataQualityLimited` is true when the response was sampled, thresholded, or collapsed high-cardinality rows into `(other)`; treat such a result as directionally useful, not exact.

## Limits

250,000 rows per Data API response (the server caps any higher client-supplied limit). Batch reports: up to 5 per call. Funnel reports: up to 10 steps. Realtime reports use a separate, smaller dimension and metric catalog covering only the last 30 minutes; non-realtime data typically lags 24 to 48 hours. Funnel reporting is v1alpha and its shape can change upstream. Every quota bucket (core reports, realtime, funnel) allows roughly 14,000 quota tokens per project per property per hour (a complex query can cost more than one token); 360 properties get a 10x multiplier. Re-check Google's current quota page before relying on an exact number.

## Verify

Each release lists the package version, the `.mcpb` sha256 and the production commit it was derived from in CHANGELOG.md. CI runs the tests and a clean install of the built wheel on every push.

## Privacy Policy

This server runs locally, on your machine, under your own credentials. It collects no personal data, contains no telemetry, stores nothing persistently, and talks only to the vendor API it wraps. No third party, including Scalably, receives your data. Contact: hello@scalably.io. Canonical copy: https://scalably.io/connector-privacy.html

## License

MIT. Copyright Scalably.
