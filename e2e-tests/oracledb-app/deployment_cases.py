"""Exercise available configurations; unavailable deployments are not called covered."""
import json
import os
from pathlib import Path
import subprocess

import oracledb
from worker_support import Cases, Harness, check, connection
from catalog import PROFILES


def run(config):
    cases, available, unavailable = Cases(), [], []
    h = Harness(config, cases)

    def configurations():
        params = oracledb.ConnectParams()
        params.parse_connect_string(os.environ["ORACLE_DSN"])
        descriptor = params.get_connect_string()
        for name, dsn, kwargs in [("easy-connect", os.environ["ORACLE_DSN"], {}),
                                  ("descriptor-no-oob", descriptor, {"disable_oob": True, "stmtcachesize": 0})]:
            raw = oracledb.connect(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"],
                                   dsn=dsn, tcp_connect_timeout=5, **kwargs)
            try:
                owner = h.scope+":"+name
                with h.client(scope=owner, connection=raw) as (client, _):
                    client.database.persist([h.doc("ALPHA "+name, tenant_id=owner)])
                    check(client.search("ALPHA", max_foreign=0)[0]["content"] == "ALPHA "+name, "Connection configuration failed")
                raw.ping()
                available.append({"configuration": name, "database_version": raw.version, "driver": oracledb.__version__, "mode": "thin"})
            finally:
                raw.close()
        pool = oracledb.create_pool(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"],
                                   dsn=os.environ["ORACLE_DSN"], min=1, max=2, increment=1, tcp_connect_timeout=5)
        try:
            with pool.acquire() as raw:
                owner = h.scope+":pool"
                with h.client(scope=owner, connection=raw) as (client, _):
                    client.database.persist([h.doc("ALPHA pooled", tenant_id=owner)])
                    check(client.search("ALPHA", max_foreign=0), "Caller-owned pool connection failed")
                raw.ping()
            check(pool.busy == 0 and pool.opened <= 2, "Pool acquisition leaked")
            available.append({"configuration": "caller-owned-pool", "busy_after_release": pool.busy, "maximum": 2})
        finally:
            pool.close()
        library = Path(config.get("native_client_dir") or "/usr/lib/oracle/21/client64/lib")
        if library.exists():
            script = Path(__file__).with_name("thick-probe.py")
            script.write_text("import sys,json,pathlib,os\nsys.path.insert(0,str(pathlib.Path(__file__).parent))\n"
                "import oracledb\nresult={}\n"
                "try:\n from oracle_config import initialize\n initialize()\n"
                " result['client_version']=oracledb.clientversion()\n"
                " from worker_support import Cases,Harness\n cfg=json.load(sys.stdin)\n c=Cases();c.current='D01thick';h=Harness(cfg,c)\n"
                " with h.client() as (client,transport):\n  result['connected']=True\n"
                "  client.database.persist([h.doc('ALPHA thick client')])\n"
                "  assert client.search('ALPHA',max_foreign=0)\n result['vector_integration']='passed'\n"
                "except Exception as e:\n result['vector_integration']='unavailable'\n result['error']=type(e).__name__+': '+str(e)\n"
                "for key in ('ORACLE_PASSWORD','ORACLE_DSN','TAVILY_API_KEY','CO_API_KEY'):\n"
                " if os.environ.get(key): result={k:(v.replace(os.environ[key],'<redacted>') if isinstance(v,str) else v) for k,v in result.items()}\n"
                "print(json.dumps(result))\n")
            env = dict(os.environ)
            env["LD_LIBRARY_PATH"] = str(library)+":"+env.get("LD_LIBRARY_PATH", "")
            env["ORACLE_E2E_MODE"] = "thick"
            env["ORACLE_CLIENT_LIB_DIR"] = str(library)
            response = subprocess.run([config["worker_python"], "-I", str(script)], input=json.dumps(config),
                                      capture_output=True, text=True, env=env, timeout=45)
            check(response.returncode == 0, "Native-client discovery process failed unexpectedly")
            probe = json.loads(response.stdout)
            (available if probe.get("vector_integration") == "passed" else unavailable).append({"configuration": "native-client-probe", **probe})
        else:
            unavailable.append({"configuration": "thick", "reason": "No native Oracle client found"})
        unavailable.extend([{"configuration": "cross-version deployment matrix and wallet/TLS",
                             "reason": "Not part of this single-target profile; run deployment-coverage with a private target inventory"}])
        return {"covered": available, "unavailable": unavailable,
                "scope": "Available configurations only; unavailable targets are not integration passes"}

    cases.case("D01", PROFILES["deployments"]["D01"], configurations)
    return cases.output()
