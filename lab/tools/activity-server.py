"""The server side of `ssisbliss activity` against the real SQL Server: SQL Agent's run-as accounts, the
jobs that run each package, and catalog executions started by people (labdev, from the developer-login
check) kept apart from SQL Agent's own runs (from the agent-chain check)."""
import json
from ssisbliss.agent.source import SqlAgent
from ssisbliss.ssis.activity import Activity
from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.ispac import Ispac

SERVER = "localhost,14331"
c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="ssisbliss-compare"))
agent = SqlAgent(SERVER, None, True)
act = Activity(None, server=Ispac(c.get_project("SILVER", "SILVER")), catalog=c, folder="SILVER", project="SILVER",
               agent=agent, days=1)
run_as = sorted(agent.run_as_accounts())
jobs = act.jobs()
main_all = c.executions("SILVER", "SILVER", "Main.dtsx", since_hours=24, limit=50)
result = {
    "run_as": run_as,
    "jobs_running_main": jobs.get("main.dtsx", []),
    "main_runs_all": len(main_all),
    "main_runs_by_people": act.runs_by_people("Main.dtsx"),
    "customer_runs_by_people": act.runs_by_people("Stage_Customer.dtsx"),
}
result["ok"] = (any("sqlagent" in a for a in run_as) and any(j.startswith("3 SILVER") for j in result["jobs_running_main"])
                and result["main_runs_all"] > 0 and not result["main_runs_by_people"]
                and any("labdev" in r["by"].lower() for r in result["customer_runs_by_people"]))
print(json.dumps(result, indent=1))
