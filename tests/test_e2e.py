import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from conftest import REPO

# The GA4 SDK clients are gRPC objects, not a plain HTTP layer we can point at a
# fake base URL (unlike gsc-mcp). e2e_server.py imports the real server module and
# swaps its lazily-built Admin/Data clients for fakes before serving over stdio, so
# this launches that harness script directly rather than "-m scalably_ga4_mcp".


def reply(result):
    return json.loads(next(x.text for x in result.content if x.type == "text"))


async def run():
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    params = StdioServerParameters(command=sys.executable, args=["e2e_server.py"], env=env, cwd=str(REPO / "tests"))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            assert len(tools.tools) == 17

            accounts = reply(await s.call_tool("ga4_list_account_summaries", {}))
            assert accounts["status"] == "succeeded" and accounts["result"]["count"] == 1
            assert "schema" not in accounts and "changed" not in accounts

            q = {"property_id": "123", "dimensions": ["country"], "metrics": ["activeUsers"],
                 "date_ranges": [{"start_date": "7daysAgo", "end_date": "today"}], "limit": 2, "offset": 0}
            partial = reply(await s.call_tool("ga4_run_report", q))
            assert partial["status"] == "partial" and partial["proof"]["nextOffset"] == 2 and partial["proof"]["propertyQuota"]

            complete = reply(await s.call_tool("ga4_run_report", {**q, "offset": 2}))
            assert complete["status"] == "succeeded" and complete["proof"]["complete"]

            quality = reply(await s.call_tool("ga4_run_report", {
                "property_id": "999", "dimensions": ["pagePath"], "metrics": ["activeUsers"],
                "date_ranges": [{"start_date": "7daysAgo", "end_date": "today"}], "limit": 10,
            }))
            assert quality["status"] == "partial" and quality["proof"]["dataQualityLimited"]

            invalid = await s.call_tool("ga4_run_report", {"property_id": "bad", "metrics": ["activeUsers"]})
            assert invalid.isError
            error_text = next(x.text for x in invalid.content if x.type == "text")
            assert "tool-outcome" not in error_text and "schema" not in error_text


def test_e2e():
    asyncio.run(run())
