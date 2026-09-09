# Google Analytics 4 MCP (read-only). 17 tools covering the GA4 Admin API and Data API read surface.
#
# References:
#   - https://developers.google.com/analytics/devguides/reporting/data/v1/rest
#   - https://developers.google.com/analytics/devguides/config/admin/v1/rest
#   - https://developers.google.com/analytics/devguides/reporting/data/v1/quotas

from __future__ import annotations

import json
import logging
import os
import re
import time
import functools
import inspect
from typing import Any

import google.auth
import google.auth.credentials
import proto
from google.analytics.admin_v1alpha import AnalyticsAdminServiceClient as AdminAlphaClient
from google.analytics.admin_v1beta import AnalyticsAdminServiceClient as AdminBetaClient
from google.analytics.admin_v1beta.types import RunAccessReportRequest
from google.analytics.data_v1alpha import AlphaAnalyticsDataClient
from google.analytics.data_v1alpha.types import (
    Funnel,
    FunnelBreakdown,
    FunnelNextAction,
    RunFunnelReportRequest,
)
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    BatchRunPivotReportsRequest,
    BatchRunReportsRequest,
    CheckCompatibilityRequest,
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    MetricAggregation,
    MinuteRange,
    OrderBy,
    Pivot,
    RunPivotReportRequest,
    RunRealtimeReportRequest,
    RunReportRequest,
)
from google.oauth2 import service_account
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# --- Constants ---

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
PROPERTY_ID_PATTERN = re.compile(r"^(?:properties/)?(\d+)$")
ACCOUNT_ID_PATTERN = re.compile(r"^(?:accounts/)?(\d+)$")

# Hard ceilings from Google (April 2026). We cap client-supplied limits to these
# values instead of hoping the LLM reads the docs.
MAX_ROW_LIMIT = 250000  # Data API hard cap per response
MAX_BATCH_REPORTS = 5
MAX_FUNNEL_STEPS = 10

REDACTION_PATTERNS = (
    re.compile(r"(?i)(\"private_key\"\s*:\s*\")([^\"]+)(\")"),
    re.compile(r"(?i)(\"client_secret\"\s*:\s*\")([^\"]+)(\")"),
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)(\S+)"),
)

mcp = FastMCP("ga4")
LOGGER = logging.getLogger(__name__)


def _reply(status: str, operation: str, summary: str, *, result=None, target=None, proof=None, warnings=None, recovery=None) -> str:
    return json.dumps({"status": status, "operation": operation, "summary": summary, "target": target, "result": result, "proof": proof, "warnings": warnings or [], "recovery": recovery}, indent=2)


def _fail(operation: str, message: str, retryable: bool = False) -> None:
    """Plain error: <code>: <message> <hint>. The operation name is already in the tool error the client shows."""
    hint = "Retry once after a delay." if retryable else "Correct credentials, permissions, identifiers, or parameters before retrying."
    raise RuntimeError(f"ga4_request_failed: {message} {hint}")


_raw_tool = mcp.tool
def _canonical_tool(*tool_args, **tool_kwargs):
    register = _raw_tool(*tool_args, **tool_kwargs)
    def decorator(fn):
        sig=inspect.signature(fn)
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                result=fn(*args, **kwargs)
            except Exception as exc:
                _fail(fn.__name__, _redact(str(exc)), any(token in str(exc).lower() for token in ("quota","timeout","unavailable","429","500","503")))
            bound=sig.bind_partial(*args, **kwargs); bound.apply_defaults(); values=bound.arguments
            rows=result.get("rows",[]) if isinstance(result,dict) else []
            total=result.get("row_count") if isinstance(result,dict) else None
            limit=values.get("limit"); offset=int(values.get("offset",0) or 0)
            complete=True; next_offset=None
            if isinstance(total,int) and isinstance(limit,int):
                complete=offset+len(rows)>=total
                if not complete: next_offset=offset+len(rows)
            metadata=result.get("metadata",{}) if isinstance(result,dict) else {}
            quality=bool(metadata.get("data_loss_from_other_row") or metadata.get("dataLossFromOtherRow") or metadata.get("subject_to_thresholding") or metadata.get("subjectToThresholding") or metadata.get("sampling_metadatas") or metadata.get("samplingMetadatas"))
            no_op=(isinstance(result,dict) and result.get("count")==0) or (isinstance(total,int) and total==0)
            partial=not complete or quality
            property_id=values.get("property_id")
            proof={"complete":not partial,"nextOffset":next_offset,"rowCount":total,"propertyQuota":result.get("property_quota") if isinstance(result,dict) else None,"dataQualityLimited":quality}
            return _reply("partial" if partial else "no_op" if no_op else "succeeded",fn.__name__,f"{fn.__name__} returned {len(rows) if rows else (result.get('count') if isinstance(result,dict) and 'count' in result else 'a')} result(s){' with remaining rows or data-quality limits' if partial else ''}.",result=result,target={"type":"ga4_property","property":str(property_id) if property_id is not None else None},proof=proof,warnings=["Do not treat this result as complete or unsampled/unthresholded."] if partial else [],recovery={"nextAction":"Continue from nextOffset or narrow the query to reduce sampling/thresholding."} if partial else None)
        wrapped.__signature__ = sig.replace(return_annotation=str)
        return register(wrapped)
    return decorator
mcp.tool = _canonical_tool

# Lazily-instantiated clients. All read-only.
_CLIENTS: dict[str, Any] = {}


# --- Security helpers ---


def _redact(text: str) -> str:
    def _replace(m: re.Match[str]) -> str:
        if (m.lastindex or 0) >= 3:
            return f"{m.group(1)}***REDACTED***{m.group(3)}"
        if (m.lastindex or 0) >= 2:
            return f"{m.group(1)}***REDACTED***"
        return "***REDACTED***"

    redacted = text
    for pattern in REDACTION_PATTERNS:
        redacted = pattern.sub(_replace, redacted)
    return redacted


# --- Credentials + clients ---


def _get_credentials() -> google.auth.credentials.Credentials:
    """Load a credential for GA4 APIs.

    Priority:
      1. If GOOGLE_APPLICATION_CREDENTIALS points to a readable JSON file,
         load it as a service account with the analytics.readonly scope.
      2. Otherwise fall back to Application Default Credentials.

    The SA's email must be added as a user (Viewer or higher) on each GA4
    property / account the agent needs to read.
    """
    sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if sa_path and os.path.isfile(sa_path):
        try:
            return service_account.Credentials.from_service_account_file(
                sa_path, scopes=DEFAULT_SCOPES
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to load SA from %s (%s); falling back to ADC",
                sa_path,
                type(exc).__name__,
            )
    creds, _ = google.auth.default(scopes=DEFAULT_SCOPES)
    return creds


def _data_client() -> BetaAnalyticsDataClient:
    if "data" not in _CLIENTS:
        _CLIENTS["data"] = BetaAnalyticsDataClient(credentials=_get_credentials())
    return _CLIENTS["data"]


def _data_alpha_client() -> AlphaAnalyticsDataClient:
    if "data_alpha" not in _CLIENTS:
        _CLIENTS["data_alpha"] = AlphaAnalyticsDataClient(
            credentials=_get_credentials()
        )
    return _CLIENTS["data_alpha"]


def _admin_client() -> AdminBetaClient:
    if "admin" not in _CLIENTS:
        _CLIENTS["admin"] = AdminBetaClient(credentials=_get_credentials())
    return _CLIENTS["admin"]


def _admin_alpha_client() -> AdminAlphaClient:
    if "admin_alpha" not in _CLIENTS:
        _CLIENTS["admin_alpha"] = AdminAlphaClient(credentials=_get_credentials())
    return _CLIENTS["admin_alpha"]


# --- Normalization helpers ---


def _normalize_property_rn(value: int | str) -> str:
    s = str(value).strip()
    m = PROPERTY_ID_PATTERN.match(s)
    if not m:
        raise ValueError(
            f"Invalid property_id {s!r}. Expected numeric ID (e.g. '123456789') "
            f"or resource name (e.g. 'properties/123456789')."
        )
    return f"properties/{m.group(1)}"


def _normalize_account_rn(value: int | str) -> str:
    s = str(value).strip()
    m = ACCOUNT_ID_PATTERN.match(s)
    if not m:
        raise ValueError(
            f"Invalid account_id {s!r}. Expected numeric ID or 'accounts/NNN'."
        )
    return f"accounts/{m.group(1)}"


def _proto_to_dict(obj: Any) -> dict[str, Any]:
    """Serialize a proto-plus message (or list) into a plain dict."""
    if isinstance(obj, proto.Message):
        return type(obj).to_dict(obj, preserving_proto_field_name=True)
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes, dict)):
        return [_proto_to_dict(x) for x in obj]  # type: ignore[return-value]
    return obj


def _build_filter_expression(spec: dict[str, Any] | None) -> FilterExpression | None:
    """Convert a loose dict spec into a Data API FilterExpression.

    Accepted shapes (any depth):
      {"and_group":   {"expressions": [<spec>, ...]}}
      {"or_group":    {"expressions": [<spec>, ...]}}
      {"not_expression": <spec>}
      {"filter": {"field_name": "...", "string_filter": {...}}}  # passthrough
      {"filter": {"field_name": "...", "in_list_filter": {...}}}
      {"filter": {"field_name": "...", "numeric_filter": {...}}}
      {"filter": {"field_name": "...", "between_filter": {...}}}

    Shortcuts:
      {"field_name": "country", "string_filter": {"value": "US"}}
        → wrapped into {"filter": {...}} automatically.
    """
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError(f"filter spec must be a dict, got {type(spec).__name__}")

    if "and_group" in spec:
        group = spec["and_group"]
        return FilterExpression(
            and_group=FilterExpressionList(
                expressions=[
                    _build_filter_expression(e) for e in group.get("expressions", [])
                ]
            )
        )
    if "or_group" in spec:
        group = spec["or_group"]
        return FilterExpression(
            or_group=FilterExpressionList(
                expressions=[
                    _build_filter_expression(e) for e in group.get("expressions", [])
                ]
            )
        )
    if "not_expression" in spec:
        return FilterExpression(
            not_expression=_build_filter_expression(spec["not_expression"])
        )
    if "filter" in spec:
        return FilterExpression(filter=Filter(**spec["filter"]))
    if "field_name" in spec:
        return FilterExpression(filter=Filter(**spec))
    raise ValueError(
        f"Unrecognized filter spec keys: {sorted(spec.keys())}. "
        "Expected one of: and_group, or_group, not_expression, filter, field_name."
    )


def _build_order_bys(specs: list[dict[str, Any]] | None) -> list[OrderBy]:
    if not specs:
        return []
    result = []
    for s in specs:
        order = OrderBy(desc=bool(s.get("desc", False)))
        if "dimension" in s:
            ob = OrderBy.DimensionOrderBy(dimension_name=s["dimension"]["dimension_name"])
            if "order_type" in s["dimension"]:
                ob.order_type = s["dimension"]["order_type"]
            order.dimension = ob
        elif "metric" in s:
            order.metric = OrderBy.MetricOrderBy(metric_name=s["metric"]["metric_name"])
        elif "pivot" in s:
            order.pivot = OrderBy.PivotOrderBy(**s["pivot"])
        else:
            raise ValueError(
                f"OrderBy entry must include 'dimension', 'metric', or 'pivot': {s}"
            )
        result.append(order)
    return result


def _build_metric_aggregations(values: list[str] | None) -> list[int]:
    if not values:
        return []
    mapping = {
        "TOTAL": MetricAggregation.TOTAL,
        "MINIMUM": MetricAggregation.MINIMUM,
        "MAXIMUM": MetricAggregation.MAXIMUM,
        "COUNT": MetricAggregation.COUNT,
    }
    out = []
    for v in values:
        key = v.upper()
        if key not in mapping:
            raise ValueError(
                f"Unknown metric_aggregation {v!r}. "
                f"Expected one of: {sorted(mapping)}"
            )
        out.append(mapping[key])
    return out


def _quota_summary(resp: Any) -> dict[str, Any] | None:
    """Extract propertyQuota fields if the response carries them."""
    if not hasattr(resp, "property_quota"):
        return None
    q = getattr(resp, "property_quota", None)
    if q is None:
        return None
    return _proto_to_dict(q)


# --- Admin API tools (10) ---


@mcp.tool(annotations=ToolAnnotations(title="List GA4 account summaries", readOnlyHint=True, openWorldHint=True))
def ga4_list_account_summaries() -> dict[str, Any]:
    """List every GA4 account and its child properties accessible to this service account.

    CALL THIS FIRST to discover which property_id to pass into other tools. The SA
    only sees accounts/properties where its email has been explicitly added as a user.

    Response: {"accounts": [{account, display_name, property_summaries: [...], ...}]}.
    Each property_summary has property, display_name, property_type, parent.
    """
    client = _admin_client()
    pager = client.list_account_summaries()
    out = [_proto_to_dict(s) for s in pager]
    return {"accounts": out, "count": len(out)}


@mcp.tool(annotations=ToolAnnotations(title="Get GA4 property details", readOnlyHint=True, openWorldHint=True))
def ga4_get_property_details(property_id: str) -> dict[str, Any]:
    """Full metadata for a single GA4 property.

    Returns display_name, property_type, parent account, time_zone, currency_code,
    industry_category, service_level, delete_time, expire_time, account reference,
    create_time, update_time.

    property_id: numeric (e.g. "123456789") or resource (e.g. "properties/123456789").
    """
    name = _normalize_property_rn(property_id)
    prop = _admin_client().get_property(name=name)
    return _proto_to_dict(prop)


@mcp.tool(annotations=ToolAnnotations(title="List GA4 data streams", readOnlyHint=True, openWorldHint=True))
def ga4_list_data_streams(property_id: str) -> dict[str, Any]:
    """List all data streams (web, iOS, Android) on a GA4 property.

    Useful for finding the Measurement ID (web), firebase_app_id (mobile),
    or the hostname of a web stream. Each stream has its creation/update timestamps.
    """
    parent = _normalize_property_rn(property_id)
    pager = _admin_client().list_data_streams(parent=parent)
    streams = [_proto_to_dict(s) for s in pager]
    return {"property": parent, "streams": streams, "count": len(streams)}


@mcp.tool(annotations=ToolAnnotations(title="List custom dimensions", readOnlyHint=True, openWorldHint=True))
def ga4_list_custom_dimensions(property_id: str) -> dict[str, Any]:
    """List custom dimensions configured on a GA4 property.

    Each entry contains the parameter_name (how you query it, use prefix
    'customEvent:' or 'customUser:' when passing to get_metadata / run_report),
    display_name, scope (EVENT|USER|ITEM), and description. Essential grounding
    before running reports that reference custom fields.
    """
    parent = _normalize_property_rn(property_id)
    pager = _admin_client().list_custom_dimensions(parent=parent)
    dims = [_proto_to_dict(d) for d in pager]
    return {"property": parent, "custom_dimensions": dims, "count": len(dims)}


@mcp.tool(annotations=ToolAnnotations(title="List custom metrics", readOnlyHint=True, openWorldHint=True))
def ga4_list_custom_metrics(property_id: str) -> dict[str, Any]:
    """List custom metrics configured on a GA4 property.

    Each entry: parameter_name, display_name, measurement_unit, scope, restricted_metric_type.
    """
    parent = _normalize_property_rn(property_id)
    pager = _admin_client().list_custom_metrics(parent=parent)
    metrics = [_proto_to_dict(m) for m in pager]
    return {"property": parent, "custom_metrics": metrics, "count": len(metrics)}


@mcp.tool(annotations=ToolAnnotations(title="List GA4 key events", readOnlyHint=True, openWorldHint=True))
def ga4_list_key_events(property_id: str) -> dict[str, Any]:
    """List key events (formerly conversions) on a GA4 property.

    These are the events the client has marked as conversion-worthy. Query
    conversion metrics via `conversions` dimension or direct metric names.
    """
    parent = _normalize_property_rn(property_id)
    pager = _admin_client().list_key_events(parent=parent)
    events = [_proto_to_dict(e) for e in pager]
    return {"property": parent, "key_events": events, "count": len(events)}


@mcp.tool(annotations=ToolAnnotations(title="List GA4 audiences", readOnlyHint=True, openWorldHint=True))
def ga4_list_audiences(property_id: str) -> dict[str, Any]:
    """List audiences defined on a GA4 property.

    Each audience has name, display_name, description, membership_duration_days,
    ads_personalization_enabled, event_trigger, exclusion_duration_mode, filter_clauses.
    Reference audienceId as a dimension in run_report for audience-based breakdowns.

    NOTE: Uses Admin v1alpha since audiences are still alpha in April 2026.
    """
    parent = _normalize_property_rn(property_id)
    pager = _admin_alpha_client().list_audiences(parent=parent)
    audiences = [_proto_to_dict(a) for a in pager]
    return {"property": parent, "audiences": audiences, "count": len(audiences)}


@mcp.tool(annotations=ToolAnnotations(title="List Google Ads links", readOnlyHint=True, openWorldHint=True))
def ga4_list_google_ads_links(property_id: str) -> dict[str, Any]:
    """List Google Ads links attached to a GA4 property.

    Returns customer_id (the Ads account), can_manage_clients, ads_personalization_enabled,
    and creator_email. Useful to confirm Ads ↔ GA4 cross-reporting availability before
    querying Ads-related dimensions like sessionGoogleAdsCampaignId.
    """
    parent = _normalize_property_rn(property_id)
    pager = _admin_client().list_google_ads_links(parent=parent)
    links = [_proto_to_dict(l) for l in pager]
    return {"property": parent, "google_ads_links": links, "count": len(links)}


@mcp.tool(annotations=ToolAnnotations(title="List property annotations", readOnlyHint=True, openWorldHint=True))
def ga4_list_property_annotations(property_id: str) -> dict[str, Any]:
    """List reporting annotations on a GA4 property.

    Annotations are markers (user-added or Google-system) flagging important
    events on a timeline: product launches, outages, marketing pushes, so
    downstream analysis can correlate metric swings with known events.

    Each annotation has: name, title, description, annotation_date,
    annotation_date_range, color, creator_email, system_generated.

    NOTE: Uses Admin v1alpha since annotations are still alpha in April 2026.
    """
    parent = _normalize_property_rn(property_id)
    pager = _admin_alpha_client().list_property_annotations(parent=parent)
    annots = [_proto_to_dict(a) for a in pager]
    return {"property": parent, "annotations": annots, "count": len(annots)}


@mcp.tool(annotations=ToolAnnotations(title="Run GA4 access report", readOnlyHint=True, openWorldHint=True))
def ga4_run_access_report(
    property_id: str,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    date_ranges: list[dict[str, str]] | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Audit log of who-read-what on a GA4 property (last 12 months).

    dimensions (common): userEmail, accessedPropertyName.
    metrics: accessCount.
    date_ranges: list of {start_date, end_date} (YYYY-MM-DD or relative). Defaults to last 7d.

    Use cases: identify usage of GA4 data by staff, confirm SA activity, compliance audits.
    """
    name = _normalize_property_rn(property_id)
    dims = [{"dimension_name": d} for d in (dimensions or ["userEmail"])]
    mets = [{"metric_name": m} for m in (metrics or ["accessCount"])]
    ranges = date_ranges or [{"start_date": "7daysAgo", "end_date": "today"}]
    req = RunAccessReportRequest(
        entity=name,
        dimensions=dims,
        metrics=mets,
        date_ranges=[{"start_date": r["start_date"], "end_date": r["end_date"]} for r in ranges],
        limit=min(limit, 100000),
    )
    resp = _admin_client().run_access_report(request=req)
    return _proto_to_dict(resp)


# --- Data API tools (6) ---


@mcp.tool(annotations=ToolAnnotations(title="Get GA4 dimension metric catalog", readOnlyHint=True, openWorldHint=True))
def ga4_get_metadata(property_id: str | None = None) -> dict[str, Any]:
    """Fetch the full GA4 dimension + metric catalog for a property.

    If property_id is omitted, returns the universal catalog (excludes
    property-specific custom fields). Pass a property_id to include custom
    dimensions and metrics like 'customEvent:foo' and 'customUser:signup_plan'.

    Response: {"dimensions": [...], "metrics": [...]}. Each entry has api_name,
    ui_name, description, category, custom_definition.

    Call this before run_report when the agent is uncertain about field names:
    GA4's catalog is large and custom fields require the property to resolve.
    """
    if property_id:
        name = f"{_normalize_property_rn(property_id)}/metadata"
    else:
        name = "properties/0/metadata"
    md = _data_client().get_metadata(name=name)
    return _proto_to_dict(md)


def _build_report_request_kwargs(
    *,
    property_id: str,
    dimensions: list[str] | None,
    metrics: list[str] | None,
    date_ranges: list[dict[str, str]] | None,
    dimension_filter: dict[str, Any] | None,
    metric_filter: dict[str, Any] | None,
    order_bys: list[dict[str, Any]] | None,
    metric_aggregations: list[str] | None,
    limit: int | None,
    offset: int | None,
    keep_empty_rows: bool | None,
    currency_code: str | None,
    cohort_spec: dict[str, Any] | None,
    comparisons: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"property": _normalize_property_rn(property_id)}
    if dimensions:
        kwargs["dimensions"] = [Dimension(name=d) for d in dimensions]
    if metrics:
        kwargs["metrics"] = [Metric(name=m) for m in metrics]
    if date_ranges:
        kwargs["date_ranges"] = [
            DateRange(
                start_date=r["start_date"],
                end_date=r["end_date"],
                name=r.get("name", ""),
            )
            for r in date_ranges
        ]
    if dimension_filter:
        kwargs["dimension_filter"] = _build_filter_expression(dimension_filter)
    if metric_filter:
        kwargs["metric_filter"] = _build_filter_expression(metric_filter)
    if order_bys:
        kwargs["order_bys"] = _build_order_bys(order_bys)
    if metric_aggregations:
        kwargs["metric_aggregations"] = _build_metric_aggregations(metric_aggregations)
    if limit is not None:
        kwargs["limit"] = min(int(limit), MAX_ROW_LIMIT)
    if offset is not None:
        kwargs["offset"] = int(offset)
    if keep_empty_rows is not None:
        kwargs["keep_empty_rows"] = bool(keep_empty_rows)
    if currency_code:
        kwargs["currency_code"] = currency_code
    if cohort_spec:
        kwargs["cohort_spec"] = cohort_spec
    if comparisons:
        kwargs["comparisons"] = comparisons
    return kwargs


@mcp.tool(annotations=ToolAnnotations(title="Run GA4 standard report", readOnlyHint=True, openWorldHint=True))
def ga4_run_report(
    property_id: str,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    date_ranges: list[dict[str, str]] | None = None,
    dimension_filter: dict[str, Any] | None = None,
    metric_filter: dict[str, Any] | None = None,
    order_bys: list[dict[str, Any]] | None = None,
    metric_aggregations: list[str] | None = None,
    limit: int = 10000,
    offset: int = 0,
    keep_empty_rows: bool = False,
    currency_code: str | None = None,
    cohort_spec: dict[str, Any] | None = None,
    comparisons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run a GA4 standard report. The workhorse tool.

    Args:
      property_id: numeric or 'properties/NNN'.
      dimensions: list of dimension api_names (e.g. ["country", "deviceCategory", "date"]).
        Custom dims require 'customEvent:' or 'customUser:' prefixes.
      metrics: list of metric api_names (e.g. ["activeUsers", "sessions", "totalRevenue"]).
      date_ranges: list of {start_date, end_date, name?}. Accepts 'YYYY-MM-DD',
        'NdaysAgo', 'today', 'yesterday'. Up to 4 ranges.
      dimension_filter / metric_filter: filter expression dicts. Shapes:
        {"filter": {"field_name": "country", "string_filter": {"value": "US"}}}
        {"and_group": {"expressions": [...]}}
        {"or_group": {"expressions": [...]}}
        {"not_expression": {...}}
      order_bys: list of {metric: {metric_name}, desc} or {dimension: {...}}.
      metric_aggregations: list of TOTAL|MINIMUM|MAXIMUM|COUNT.
      limit: max rows (hard cap 250000 per response).
      offset: pagination offset.
      keep_empty_rows: include rows where all metrics are zero.
      currency_code: override property default for revenue metrics.
      cohort_spec / comparisons: advanced specs (see REST docs).

    Gotchas surfaced in response.metadata:
      - samplingMetadatas: present if query was sampled (&gt; 10M events scanned)
      - dataLossFromOtherRow: true if high-cardinality dims collapsed into "(other)"
      - schemaRestrictionResponse: active thresholding rules
      - subjectToThresholding: true if user-privacy thresholding dropped rows

    propertyQuota always included so the agent can self-throttle.
    """
    kwargs = _build_report_request_kwargs(
        property_id=property_id,
        dimensions=dimensions,
        metrics=metrics,
        date_ranges=date_ranges,
        dimension_filter=dimension_filter,
        metric_filter=metric_filter,
        order_bys=order_bys,
        metric_aggregations=metric_aggregations,
        limit=limit,
        offset=offset,
        keep_empty_rows=keep_empty_rows,
        currency_code=currency_code,
        cohort_spec=cohort_spec,
        comparisons=comparisons,
    )
    kwargs["return_property_quota"] = True
    req = RunReportRequest(**kwargs)
    resp = _data_client().run_report(request=req)
    return _proto_to_dict(resp)


@mcp.tool(annotations=ToolAnnotations(title="Run batch GA4 reports", readOnlyHint=True, openWorldHint=True))
def ga4_batch_run_reports(
    property_id: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run up to 5 GA4 reports in a single round-trip.

    requests: list of run_report-shaped dicts. Same top-level parameters as
    ga4_run_report (minus property_id, inferred from the batch). Each entry
    accepts: dimensions, metrics, date_ranges, dimension_filter, metric_filter,
    order_bys, metric_aggregations, limit, offset, keep_empty_rows, currency_code.

    Useful when the agent needs paired views (e.g. landing pages + referrers
    for the same window) and wants them atomically + under one quota call.
    """
    if len(requests) > MAX_BATCH_REPORTS:
        raise ValueError(f"Max {MAX_BATCH_REPORTS} reports per batch; got {len(requests)}.")
    pid = _normalize_property_rn(property_id)
    batch = []
    for r in requests:
        kwargs = _build_report_request_kwargs(
            property_id=property_id,
            dimensions=r.get("dimensions"),
            metrics=r.get("metrics"),
            date_ranges=r.get("date_ranges"),
            dimension_filter=r.get("dimension_filter"),
            metric_filter=r.get("metric_filter"),
            order_bys=r.get("order_bys"),
            metric_aggregations=r.get("metric_aggregations"),
            limit=r.get("limit", 10000),
            offset=r.get("offset", 0),
            keep_empty_rows=r.get("keep_empty_rows"),
            currency_code=r.get("currency_code"),
            cohort_spec=r.get("cohort_spec"),
            comparisons=r.get("comparisons"),
        )
        kwargs["return_property_quota"] = True
        batch.append(RunReportRequest(**kwargs))
    req = BatchRunReportsRequest(property=pid, requests=batch)
    resp = _data_client().batch_run_reports(request=req)
    return _proto_to_dict(resp)


@mcp.tool(annotations=ToolAnnotations(title="Run GA4 pivot report", readOnlyHint=True, openWorldHint=True))
def ga4_run_pivot_report(
    property_id: str,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    pivots: list[dict[str, Any]] | None = None,
    date_ranges: list[dict[str, str]] | None = None,
    dimension_filter: dict[str, Any] | None = None,
    metric_filter: dict[str, Any] | None = None,
    currency_code: str | None = None,
    keep_empty_rows: bool = False,
) -> dict[str, Any]:
    """Run a GA4 pivot report.

    pivots: list of pivot specs, each with:
      - field_names: list of dimension names to pivot on
      - limit: max rows per pivot
      - offset: pagination offset
      - order_bys: list of OrderBy dicts (same shape as run_report's order_bys)
      - metric_aggregations: list of TOTAL|MINIMUM|MAXIMUM|COUNT

    Use when you want a 2D view: e.g. rows=date, columns=device, values=sessions.
    """
    pid = _normalize_property_rn(property_id)
    kwargs: dict[str, Any] = {"property": pid, "return_property_quota": True}
    if dimensions:
        kwargs["dimensions"] = [Dimension(name=d) for d in dimensions]
    if metrics:
        kwargs["metrics"] = [Metric(name=m) for m in metrics]
    if date_ranges:
        kwargs["date_ranges"] = [
            DateRange(start_date=r["start_date"], end_date=r["end_date"], name=r.get("name", ""))
            for r in date_ranges
        ]
    if dimension_filter:
        kwargs["dimension_filter"] = _build_filter_expression(dimension_filter)
    if metric_filter:
        kwargs["metric_filter"] = _build_filter_expression(metric_filter)
    if currency_code:
        kwargs["currency_code"] = currency_code
    kwargs["keep_empty_rows"] = bool(keep_empty_rows)
    if pivots:
        built_pivots = []
        for p in pivots:
            pv = Pivot(
                field_names=p.get("field_names", []),
                limit=min(int(p.get("limit", 10000)), MAX_ROW_LIMIT),
                offset=int(p.get("offset", 0)),
                metric_aggregations=_build_metric_aggregations(
                    p.get("metric_aggregations")
                ),
            )
            if p.get("order_bys"):
                pv.order_bys = _build_order_bys(p["order_bys"])
            built_pivots.append(pv)
        kwargs["pivots"] = built_pivots
    req = RunPivotReportRequest(**kwargs)
    resp = _data_client().run_pivot_report(request=req)
    return _proto_to_dict(resp)


@mcp.tool(annotations=ToolAnnotations(title="Run GA4 realtime report", readOnlyHint=True, openWorldHint=True))
def ga4_run_realtime_report(
    property_id: str,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    dimension_filter: dict[str, Any] | None = None,
    metric_filter: dict[str, Any] | None = None,
    limit: int = 10000,
    minute_ranges: list[dict[str, int]] | None = None,
    order_bys: list[dict[str, Any]] | None = None,
    metric_aggregations: list[str] | None = None,
) -> dict[str, Any]:
    """Run a GA4 real-time report. Covers the last 30 minutes only.

    IMPORTANT: realtime uses a SEPARATE, smaller dimension/metric catalog. Do NOT
    pass 'date', 'totalRevenue', etc: they don't exist in realtime. Common dims:
    'minutesAgo', 'country', 'deviceCategory', 'unifiedScreenName', 'eventName'.
    Common metrics: 'activeUsers', 'screenPageViews', 'eventCount'.

    minute_ranges: list of {start_minutes_ago, end_minutes_ago, name?}. Max 2
    ranges. Values are 0-29 (0 = now, 29 = 30 min ago).

    For historical / batch reports use ga4_run_report instead.
    """
    pid = _normalize_property_rn(property_id)
    kwargs: dict[str, Any] = {"property": pid, "return_property_quota": True}
    if dimensions:
        kwargs["dimensions"] = [Dimension(name=d) for d in dimensions]
    if metrics:
        kwargs["metrics"] = [Metric(name=m) for m in metrics]
    if dimension_filter:
        kwargs["dimension_filter"] = _build_filter_expression(dimension_filter)
    if metric_filter:
        kwargs["metric_filter"] = _build_filter_expression(metric_filter)
    kwargs["limit"] = min(int(limit), MAX_ROW_LIMIT)
    if minute_ranges:
        kwargs["minute_ranges"] = [
            MinuteRange(
                start_minutes_ago=int(r.get("start_minutes_ago", 29)),
                end_minutes_ago=int(r.get("end_minutes_ago", 0)),
                name=r.get("name", ""),
            )
            for r in minute_ranges
        ]
    if order_bys:
        kwargs["order_bys"] = _build_order_bys(order_bys)
    if metric_aggregations:
        kwargs["metric_aggregations"] = _build_metric_aggregations(metric_aggregations)
    req = RunRealtimeReportRequest(**kwargs)
    resp = _data_client().run_realtime_report(request=req)
    return _proto_to_dict(resp)


@mcp.tool(annotations=ToolAnnotations(title="Check dimension metric compatibility", readOnlyHint=True, openWorldHint=True))
def ga4_check_compatibility(
    property_id: str,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    dimension_filter: dict[str, Any] | None = None,
    metric_filter: dict[str, Any] | None = None,
    compatibility_filter: str = "COMPATIBLE",
) -> dict[str, Any]:
    """Validate whether a dim/metric combo can be queried together. Cheap pre-flight.

    compatibility_filter: 'COMPATIBLE' (default, returns only fields that work
    with the provided selection) or 'INCOMPATIBLE' (returns only fields that
    would conflict).

    Use before run_report when composing exploratory queries, cheaper than
    catching a GoogleAdsException after a big failed report.
    """
    pid = _normalize_property_rn(property_id)
    kwargs: dict[str, Any] = {"property": pid}
    if dimensions:
        kwargs["dimensions"] = [Dimension(name=d) for d in dimensions]
    if metrics:
        kwargs["metrics"] = [Metric(name=m) for m in metrics]
    if dimension_filter:
        kwargs["dimension_filter"] = _build_filter_expression(dimension_filter)
    if metric_filter:
        kwargs["metric_filter"] = _build_filter_expression(metric_filter)
    filter_map = {
        "COMPATIBILITY_UNSPECIFIED": 0,
        "COMPATIBLE": 1,
        "INCOMPATIBLE": 2,
    }
    if compatibility_filter.upper() not in filter_map:
        raise ValueError(
            f"compatibility_filter must be one of {sorted(filter_map)}"
        )
    kwargs["compatibility_filter"] = filter_map[compatibility_filter.upper()]
    req = CheckCompatibilityRequest(**kwargs)
    resp = _data_client().check_compatibility(request=req)
    return _proto_to_dict(resp)


@mcp.tool(annotations=ToolAnnotations(title="Run GA4 funnel report", readOnlyHint=True, openWorldHint=True))
def ga4_run_funnel_report(
    property_id: str,
    funnel: dict[str, Any],
    date_ranges: list[dict[str, str]] | None = None,
    dimension_filter: dict[str, Any] | None = None,
    funnel_breakdown: dict[str, Any] | None = None,
    funnel_next_action: dict[str, Any] | None = None,
    limit: int = 10000,
    return_property_quota: bool = True,
) -> dict[str, Any]:
    """Run a GA4 funnel report. v1alpha, API surface may change.

    funnel: {
      is_open_funnel: bool,
      steps: [
        {
          name: str,
          is_directly_followed_by: bool,
          filter_expression: FunnelFilterExpression,
          within_duration_from_prior_step: {seconds: int},
        }, ...
      ]
    }
    funnel_breakdown: {dimension_name, limit}, optional per-step breakdown.
    funnel_next_action: {dimension_name, limit}, optional "what came after".

    Use for drop-off analysis across a sequence of events. Behind an alpha flag
    because Google can change the shape, check the Feb 2026 release notes if
    this fails with a validation error.

    Ref: https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1alpha/properties/runFunnelReport
    """
    pid = _normalize_property_rn(property_id)
    kwargs: dict[str, Any] = {"property": pid}
    if date_ranges:
        kwargs["date_ranges"] = [
            DateRange(start_date=r["start_date"], end_date=r["end_date"], name=r.get("name", ""))
            for r in date_ranges
        ]
    if dimension_filter:
        kwargs["dimension_filter"] = _build_filter_expression(dimension_filter)
    steps = funnel.get("steps") or []
    if len(steps) > MAX_FUNNEL_STEPS:
        raise ValueError(
            f"Max {MAX_FUNNEL_STEPS} funnel steps; got {len(steps)}."
        )
    kwargs["funnel"] = Funnel(**funnel)
    if funnel_breakdown:
        kwargs["funnel_breakdown"] = FunnelBreakdown(**funnel_breakdown)
    if funnel_next_action:
        kwargs["funnel_next_action"] = FunnelNextAction(**funnel_next_action)
    kwargs["limit"] = min(int(limit), MAX_ROW_LIMIT)
    kwargs["return_property_quota"] = return_property_quota
    req = RunFunnelReportRequest(**kwargs)
    resp = _data_alpha_client().run_funnel_report(request=req)
    return _proto_to_dict(resp)


# --- Entry ---


def _configure_logging() -> None:
    level = os.environ.get("GA4_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s ga4-mcp %(message)s",
    )


def main() -> None:
    _configure_logging()
    # Eagerly validate credentials so startup fails loudly if SA is missing.
    try:
        _get_credentials()
    except Exception as exc:
        LOGGER.error("Credential load failed at startup: %s", exc)
    mcp.run()


if __name__ == "__main__":
    main()
