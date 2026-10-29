"""Extended Events capture around the Visual Studio deploy: `capture.py start`, then `capture.py stop`,
which prints what reached the server and how /SSISDB/PKGCMP/Corpus changed."""
import json, re, sys, time
import xml.etree.ElementTree as ET
from pathlib import Path
from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.dtsx import package_sha256
from ssisbliss.ssis.ispac import Ispac

XE, APP = "onprem_vsui", "onprem-compare"
c = SqlCatalog(connection_string("localhost,14331", trust_server_certificate=True, app=APP))
cur = c.conn.cursor()
if sys.argv[1] == "start":
    cur.execute(f"""
        IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = N'{XE}') DROP EVENT SESSION {XE} ON SERVER;
        CREATE EVENT SESSION {XE} ON SERVER
        ADD EVENT sqlserver.rpc_completed (ACTION (sqlserver.client_app_name) WHERE sqlserver.client_app_name <> N'{APP}'),
        ADD EVENT sqlserver.sql_batch_completed (ACTION (sqlserver.client_app_name) WHERE sqlserver.client_app_name <> N'{APP}')
        ADD TARGET package0.ring_buffer (SET max_memory = 102400) WITH (MAX_DISPATCH_LATENCY = 1 SECONDS);
        ALTER EVENT SESSION {XE} ON SERVER STATE = START;""")
    print("capture started")
    sys.exit(0)
time.sleep(3)
cur.execute(f"""SELECT CAST(t.target_data AS nvarchar(max)) FROM sys.dm_xe_session_targets t
                JOIN sys.dm_xe_sessions s ON s.address = t.event_session_address WHERE s.name = N'{XE}'""")
row = cur.fetchone()
cur.execute(f"ALTER EVENT SESSION {XE} ON SERVER STATE = STOP; DROP EVENT SESSION {XE} ON SERVER;")
proj = Path(r"C:\lab\work\vsui\Corpus")
source = package_sha256((proj / "Load_B.dtsx").read_bytes())
apps, procs, sent = set(), set(), []
for ev in ET.fromstring(row[0]).iter("event") if row else []:
    d = {n.get("name", ""): n.findtext("value") for n in ev}
    text = d.get("statement") or d.get("batch_text") or ""
    found = {f"{a.lower()}.{b}" for a, b in re.findall(r"\[?\b(catalog|internal)\]?\.\[?(\w+)\]?", text, re.I)}
    if not found:
        continue
    apps.add(d.get("client_app_name")); procs |= {p for p in found if p.startswith("catalog.")}
    for m in re.finditer(r"0x([0-9A-Fa-f]{200,})", text):
        data = bytes.fromhex(m.group(1))
        if data.lstrip(b"\xef\xbb\xbf").lstrip()[:1] == b"<":
            sent.append("source Load_B.dtsx" if package_sha256(data) == source else "other")
now = Ispac(c.get_project("PKGCMP", "Corpus")).package_hashes()
base = json.loads((proj.parent / "baseline.json").read_text())
print(json.dumps({"apps": sorted(a for a in apps if a), "procedures": sorted(procs), "packages_sent": sent,
                  "changed_on_server": sorted(n for n in now if now[n] != base["hashes"].get(n)),
                  "server_Load_B_is_source": now.get("Load_B.dtsx") == source,
                  "new_versions": c.project("PKGCMP", "Corpus").object_version_lsn - base["baseline_lsn"]}))
