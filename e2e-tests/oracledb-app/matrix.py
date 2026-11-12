"""Probe declared Python support with actual interpreters and the built wheel."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import check
from service import provider_server


def run(package, evidence):
    from run import command, clean_environment
    versions = ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]
    outcomes = []
    uv = shutil.which("uv") or "/home/vipuvaib/.local/bin/uv"
    with provider_server() as (endpoint, state):
        for version in versions:
            result = {"python": version, "status": "passed"}
            label = "python-" + version
            try:
                interpreter = subprocess.check_output([uv, "python", "find", "--no-python-downloads", version],
                                                       text=True, stderr=subprocess.STDOUT, timeout=15).strip()
                workspace = Path(tempfile.mkdtemp(prefix="tavily-python-" + version + "-"))
                command([interpreter, "-m", "venv", str(workspace / "venv")], workspace, evidence, label + "-venv")
                python = workspace / "venv" / "bin" / "python"
                command([str(python), "-m", "pip", "install", "--only-binary=:all:", package["wheel"]],
                        workspace, evidence, label + "-install", clean_environment())
                probe = """
import asyncio, json, pathlib, sys
import tavily
from tavily import TavilyClient, AsyncTavilyClient, TavilyHybridClient
from tavily.databases.oracledb import OracleDB
assert str(pathlib.Path(tavily.__file__).resolve()).startswith(str(pathlib.Path(sys.prefix).resolve()))
endpoint=sys.argv[1]
with TavilyClient('fixture-key',api_base_url=endpoint) as client:
    assert client.search('Oracle',max_results=1)['results']
async def test():
    async with AsyncTavilyClient('fixture-key',api_base_url=endpoint) as client:
        assert (await client.search('Oracle',max_results=1))['results']
asyncio.run(test())
print(json.dumps({'python':sys.version.split()[0],'path':tavily.__file__}))
"""
                result.update(json.loads(command([str(python), "-I", "-c", probe, endpoint], workspace,
                                                   evidence, label + "-imports-and-http", clean_environment())))
            except Exception as error:
                result.update(status="failed", error=str(error))
            outcomes.append(result)
            evidence.report["python_matrix"] = outcomes
            print(label + " " + result["status"].upper(), flush=True)
    failed = [item["python"] for item in outcomes if item["status"] != "passed"]
    check(not failed, "Declared Python compatibility failed for: " + ", ".join(failed))
    evidence.passed("Y01", "Testing-scope Python >=3.9 import and HTTP matrix", versions=outcomes,
                    historical_finding="Python 3.8 remains excluded from this testing scope; SDK metadata was not changed")
