import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scalably_ga4_mcp import server

class Admin:
    def list_account_summaries(self): return [{"account":"accounts/1","display_name":"Test","property_summaries":[{"property":"properties/123"}]}]

class Data:
    def run_report(self, request):
        start=int(request.offset or 0); limit=int(request.limit or 10000); total=3
        rows=[{"dimension_values":[{"value":str(i)}],"metric_values":[{"value":str(i+1)}]} for i in range(start,min(start+limit,total))]
        metadata={"data_loss_from_other_row": request.property=="properties/999"}
        return {"rows":rows,"row_count":total,"metadata":metadata,"property_quota":{"tokens_per_day":{"remaining":999}}}

server._CLIENTS.update({"admin":Admin(),"admin_alpha":Admin(),"data":Data(),"data_alpha":Data()})
server.mcp.run()
