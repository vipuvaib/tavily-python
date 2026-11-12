"""Provision only a new, explicitly named test-owned Oracle container."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import uuid

HERE = Path(__file__).resolve().parent


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "configure", "configure-tls"])
    parser.add_argument("--state", type=Path)
    parser.add_argument("--image", default="container-registry.oracle.com/database/free:23.9.0.0")
    args = parser.parse_args()
    if args.action == "start":
        directory = HERE / ".artifacts" / ("isolated-deployment-" + uuid.uuid4().hex[:12])
        directory.mkdir(mode=0o700)
        password = "TvE" + secrets.token_hex(18)
        (directory / "container.env").write_text("ORACLE_PWD=" + password + "\n")
        name = "tavily-e2e-" + directory.name.split("-")[-1]
        tcp, tls = unused_port(), unused_port()
        assert tcp != tls
        command = ["podman", "run", "-d", "--name", name,
                   "--label", "tavily-e2e-owned=" + directory.name,
                   "--env-file", str(directory / "container.env"),
                   "--memory", "6g", "--shm-size", "1g",
                   "-p", "127.0.0.1:" + str(tcp) + ":1521",
                   "-p", "127.0.0.1:" + str(tls) + ":1522", args.image]
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        (directory / "start.log").write_text((result.stdout + result.stderr).replace(password, "<redacted>"))
        if result.returncode:
            raise RuntimeError("Container start failed; see " + str(directory / "start.log"))
        state = {"name": name, "id": result.stdout.strip(), "image": args.image,
                 "directory": str(directory), "tcp_port": tcp, "tls_port": tls}
        args.state = directory / "state.json"
        args.state.write_text(json.dumps(state, indent=2) + "\n")
        print(json.dumps({"state": str(args.state), **state}))
        return
    if args.state is None:
        parser.error("--state is required")
    state = json.loads(args.state.read_text())
    directory = Path(state["directory"])
    actual = json.loads(subprocess.check_output(["podman", "inspect", state["id"]], text=True))[0]
    assert actual["Name"].lstrip("/") == state["name"]
    assert actual["Config"]["Labels"]["tavily-e2e-owned"] == directory.name
    if args.action == "status":
        print(json.dumps({"name": state["name"], "state": actual["State"]["Status"],
                          "health": actual["State"].get("Health", {}).get("Status")}))
        return
    assert actual["State"]["Status"] == "running"
    if args.action == "configure-tls":
        password = "TvE" + secrets.token_hex(18)
        script = ("set -eu\nexport TVE_WALLET_PASSWORD=" + password + "\n" + r'''
wallet=/opt/oracle/tavily-e2e-wallet
badwallet=/opt/oracle/tavily-e2e-untrusted
mkdir -p "$wallet" "$badwallet"
orapki wallet create -wallet "$wallet" -auto_login -pwd "$TVE_WALLET_PASSWORD"
orapki wallet add -wallet "$wallet" -dn CN=localhost -keysize 2048 -self_signed -validity 30 -pwd "$TVE_WALLET_PASSWORD"
orapki wallet export -wallet "$wallet" -dn CN=localhost -cert "$wallet/server.crt" -pwd "$TVE_WALLET_PASSWORD"
openssl pkcs12 -in "$wallet/ewallet.p12" -out "$wallet/ewallet.pem" -nodes -passin env:TVE_WALLET_PASSWORD
orapki wallet create -wallet "$badwallet" -auto_login -pwd "$TVE_WALLET_PASSWORD"
orapki wallet add -wallet "$badwallet" -dn CN=untrusted-e2e -keysize 2048 -self_signed -validity 30 -pwd "$TVE_WALLET_PASSWORD"
openssl pkcs12 -in "$badwallet/ewallet.p12" -out "$badwallet/ewallet.pem" -nodes -passin env:TVE_WALLET_PASSWORD
net="$ORACLE_HOME/network/admin"
cp "$net/listener.ora" "$net/listener.ora.tavily-e2e-before"
cp "$net/sqlnet.ora" "$net/sqlnet.ora.tavily-e2e-before"
cat > "$net/listener.ora" <<'LISTENER'
LISTENER=(DESCRIPTION_LIST=(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=0.0.0.0)(PORT=1521))(ADDRESS=(PROTOCOL=TCPS)(HOST=0.0.0.0)(PORT=1522))(ADDRESS=(PROTOCOL=IPC)(KEY=EXTPROC1521))))
WALLET_LOCATION=(SOURCE=(METHOD=FILE)(METHOD_DATA=(DIRECTORY=/opt/oracle/tavily-e2e-wallet)))
SSL_CLIENT_AUTHENTICATION=FALSE
LISTENER
cat >> "$net/sqlnet.ora" <<'SQLNET'

WALLET_LOCATION=(SOURCE=(METHOD=FILE)(METHOD_DATA=(DIRECTORY=/opt/oracle/tavily-e2e-wallet)))
SSL_CLIENT_AUTHENTICATION=FALSE
SQLNET
lsnrctl stop
lsnrctl start
printf 'alter system register;\nexit\n' | sqlplus -s / as sysdba
''')
        result = subprocess.run(["podman", "exec", "-i", state["id"], "bash"],
                                input=script, capture_output=True, text=True, timeout=90)
        (directory / "tls-setup.log").write_text((result.stdout + result.stderr).replace(password, "<redacted>"))
        if result.returncode:
            raise RuntimeError("Owned TLS configuration failed; see " + str(directory / "tls-setup.log"))
        for name in ("wallet", "untrusted"):
            remote = "/opt/oracle/tavily-e2e-" + name
            destination = directory / name
            subprocess.run(["podman", "cp", state["id"] + ":" + remote, str(destination)], check=True, capture_output=True)
            # podman cp can preserve mapped container ownership; normalize only this new copy.
            subprocess.run(["podman", "unshare", "chown", "-R", "0:0", str(destination)], check=True, capture_output=True)
            destination.chmod(0o700)
            for path in destination.iterdir():
                path.chmod(0o600)
        from dotenv import dotenv_values
        app = dotenv_values(directory / "app.env", interpolate=False)
        wallet = directory / "wallet"
        (wallet / "sqlnet.ora").write_text("WALLET_LOCATION=(SOURCE=(METHOD=FILE)(METHOD_DATA=(DIRECTORY=" +
                                            str(wallet) + ")))\nSSL_CLIENT_AUTHENTICATION=FALSE\nSSL_SERVER_DN_MATCH=YES\n")
        kwargs = {"wallet_location": str(wallet), "config_dir": str(wallet),
                  "ssl_server_dn_match": True, "ssl_server_cert_dn": "CN=localhost"}
        env = "ORACLE_USER=" + app["ORACLE_USER"] + "\nORACLE_PASSWORD=" + app["ORACLE_PASSWORD"]
        env += "\nORACLE_DSN=tcps://127.0.0.1:" + str(state["tls_port"]) + "/FREEPDB1\n"
        env += "ORACLE_CONNECT_KWARGS='" + json.dumps(kwargs) + "'\n"
        (directory / "tls.env").write_text(env)
        print(json.dumps({"tls_env": str(directory / "tls.env"), "wallet": str(wallet),
                          "untrusted_wallet": str(directory / "untrusted")}))
        return
    # All administrative SQL is confined to this newly created owned container.
    password = "TvE" + secrets.token_hex(18)
    sql = ("whenever sqlerror exit failure rollback\n"
           "alter system set vector_memory_size=256M scope=spfile;\nshutdown immediate;\nstartup;\n"
           "alter session set container=FREEPDB1;\n"
           "create user TVE_APP identified by \"" + password + "\" quota unlimited on USERS;\n"
           "grant create session, create table, create procedure, ctxapp to TVE_APP;\n"
           "grant select on sys.v_$session to TVE_APP;\n"
           "grant select on sys.v_$sql to TVE_APP;\n"
           "grant select on sys.v_$sql_plan to TVE_APP;\n"
           "grant select on sys.v_$sql_plan_statistics_all to TVE_APP;\n"
           "grant select on sys.v_$sesstat to TVE_APP;\n"
           "grant select on sys.v_$statname to TVE_APP;\n"
           "select version_full from product_component_version where product like 'Oracle%';\nexit\n")
    result = subprocess.run(["podman", "exec", "-i", state["id"], "bash", "-lc", "sqlplus -s / as sysdba"],
                            input=sql, capture_output=True, text=True, timeout=60)
    (directory / "configure.log").write_text((result.stdout + result.stderr).replace(password, "<redacted>"))
    if result.returncode:
        raise RuntimeError("Owned deployment configuration failed; see " + str(directory / "configure.log"))
    (directory / "app.env").write_text("ORACLE_USER=TVE_APP\nORACLE_PASSWORD=" + password +
                                      "\nORACLE_DSN=127.0.0.1:" + str(state["tcp_port"]) + "/FREEPDB1\n")
    print(json.dumps({"state": str(args.state), "app_env": str(directory / "app.env"),
                      "configuration": "owned application schema with scoped diagnostics"}))


if __name__ == "__main__":
    main()
