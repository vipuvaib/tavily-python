"""Configuration, evidence, and ownership checks for the Oracle E2E harness."""
import hashlib
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import dotenv_values
import oracledb
from oracle_config import OPTIONAL, initialize, options, server_major, server_release

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REQUIRED = {
    "package": ["P01", "P02", "P03"],
    "smoke": ["E01", "E02", "E03", "E04", "E05"],
    "live": ["E01", "E02", "E03", "E04", "E05"],
    "preflight": ["F01"],
    "cleanup": ["C01"],
}
from catalog import PROFILES
REQUIRED.update({profile: list(cases) for profile, cases in PROFILES.items()})


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def settings(path, live=False):
    path = Path(path).resolve()
    check(path.is_file(), "Env file does not exist: " + str(path))
    check(path.stat().st_mode & 0o077 == 0, "Env file must have owner-only permissions")
    values = dotenv_values(path, interpolate=False)
    keys = ["ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN"]
    if live:
        keys.append("TAVILY_API_KEY")
    result = {key: os.environ.get(key, values.get(key)) for key in keys}
    for key, value in result.items():
        check(isinstance(value, str) and bool(value), "Missing setting: " + key)
    for key in OPTIONAL:
        value = os.environ.get(key, values.get(key))
        if value is not None:
            result[key] = value
    check(result["ORACLE_USER"].upper() not in {"SYS", "SYSTEM", "PDBADMIN"},
          "Use an application test schema")
    return result


def redact(text, secrets=()):
    for value in sorted((v for v in secrets if v), key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    text = re.sub(r"tvly-[A-Za-z0-9_-]+", "<redacted-tavily-key>", text)
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1<redacted>@", text)


def private_values(values):
    result = [values.get(key) for key in ("ORACLE_PASSWORD", "ORACLE_DSN", "TAVILY_API_KEY", "CO_API_KEY")]
    result.append(options(values).get("wallet_password"))
    return [value for value in result if value]


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def sanitize(value, secrets):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item, secrets) for key, item in value.items()}
    return value


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def harness_fingerprint():
    paths = [path for path in HERE.iterdir() if path.is_file() and
             (path.suffix in {".py", ".ini", ".txt"} or path.name == ".gitignore")]
    paths.extend((HERE / "tests").rglob("*.py"))
    files = {str(path.relative_to(HERE)): sha256(path) for path in sorted(paths)}
    return {"sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), "files": files}


def source_files():
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "tavily", "setup.py", "setup.cfg",
         "pyproject.toml", "MANIFEST.in", "README.md", "LICENSE"], cwd=ROOT,
    ).decode().split("\0")
    return sorted(name for name in names if name and (ROOT / name).is_file())


def fingerprint():
    files = {name: sha256(ROOT / name) for name in source_files()}
    return {"sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            "files": files}


def connect(values):
    initialize(values)
    connection = oracledb.connect(user=values["ORACLE_USER"],
                                  password=values["ORACLE_PASSWORD"],
                                  dsn=values["ORACLE_DSN"], tcp_connect_timeout=10, **options(values))
    connection.call_timeout = 15000
    return connection


def database_identity(connection):
    with connection.cursor() as cursor:
        cursor.execute("""SELECT SYS_CONTEXT('USERENV','SESSION_USER'),
            SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), SYS_CONTEXT('USERENV','CON_NAME'),
            SYS_CONTEXT('USERENV','CON_ID'), SYS_CONTEXT('USERENV','SERVER_HOST'),
            SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual""")
        return dict(zip(["owner", "database", "container", "container_id", "host", "service"],
                        cursor.fetchone()))


def preflight(values):
    with connect(values) as connection:
        major = server_major(connection.version)
        identity = database_identity(connection)
        check(identity["owner"].upper() == values["ORACLE_USER"].upper(),
              "Connected owner differs from configured owner")
        with connection.cursor() as cursor:
            cursor.execute("SELECT BANNER_FULL FROM V$VERSION")
            banner = cursor.fetchone()[0]
            release = server_release(connection.version, banner)
            cursor.execute("SELECT VECTOR_DISTANCE(TO_VECTOR('[1,0,0]'), "
                           "TO_VECTOR('[1,0,0]'), COSINE) FROM dual")
            check(abs(cursor.fetchone()[0]) < 1e-6, "Vector capability check failed")
            cursor.execute("SELECT COUNT(*) FROM session_privs WHERE privilege='CREATE TABLE'")
            check(cursor.fetchone()[0] == 1, "Test schema needs CREATE TABLE")
            cursor.execute("SELECT object_name, object_type, object_id FROM user_objects")
            objects = [list(row) for row in cursor.fetchall()]
            cursor.execute("SELECT index_name,table_name,status FROM user_indexes WHERE index_type='VECTOR'")
            vector_indexes = [list(row) for row in cursor.fetchall()]
            prefixes = ["VECTOR$" + row[0] + "$" for row in vector_indexes]
            transient = [row for row in objects if transient_vector_auxiliary(row[0], prefixes)]
            objects = [row for row in objects if not transient_vector_auxiliary(row[0], prefixes)]
        return {"identity": identity, "database_version": connection.version,
                "database_banner": banner, "database_release": release,
                "database_major": major, "oracle_mode": "thin" if oracledb.is_thin_mode() else "thick",
                "client_version": None if oracledb.is_thin_mode() else list(oracledb.clientversion()),
                "transient_vector_objects": transient, "existing_vector_indexes": vector_indexes,
                "driver_version": oracledb.__version__, "existing_objects": objects}


def transient_vector_auxiliary(name, prefixes):
    # Oracle can roll its HNSW ROWID/VID map generation while the base index stays valid.
    # Prefixes come only from catalog-confirmed vector indexes. Base objects stay protected.
    return any(name.startswith(prefix) and re.fullmatch(r"\d+_\d+_\d+\$HNSW_ROWID_VID_(?:MAP|IDX)",
                                                      name[len(prefix):]) for prefix in prefixes)


def verify_schema_preserved(before, after):
    check({tuple(row) for row in before["existing_objects"]}.issubset(
          {tuple(row) for row in after["existing_objects"]}), "Pre-existing schema objects changed")
    valid_before = {row[0] for row in before["existing_vector_indexes"] if row[2] == "VALID"}
    valid_after = {row[0] for row in after["existing_vector_indexes"] if row[2] == "VALID"}
    check(valid_before.issubset(valid_after), "Pre-existing vector index lost VALID status")


def table_identifier(name):
    check(bool(re.fullmatch(r"TVE_[A-F0-9]{20}", name)), "Invalid owned test-table name")
    return name


def verify_owner(connection, manifest):
    table_identifier(manifest["table"])
    check(database_identity(connection) == manifest["identity"], "Database identity mismatch")
    with connection.cursor() as cursor:
        cursor.execute("SELECT object_id FROM user_objects WHERE object_name=:name "
                       "AND object_type='TABLE'", name=manifest["table"])
        row = cursor.fetchone()
        check(row is not None and row[0] == manifest["object_id"], "Owned table identity changed")
        cursor.execute("SELECT comments FROM user_tab_comments WHERE table_name=:name",
                       name=manifest["table"])
        row = cursor.fetchone()
        check(row is not None and row[0] == "tavily-e2e:" + manifest["run_id"],
              "Owned table marker mismatch")


def retire_hnsw_index(connection, manifest, suffix="_ANN"):
    """Release only the fixed-name HNSW index of this verified test table."""
    check(suffix in ("_ANN", "_VEC"), "Invalid test-index suffix")
    verify_owner(connection, manifest)
    index = manifest["table"] + suffix
    with connection.cursor() as cursor:
        cursor.execute("SELECT I.TABLE_NAME,I.INDEX_TYPE,I.INDEX_SUBTYPE,O.OBJECT_ID "
                       "FROM USER_INDEXES I LEFT JOIN USER_OBJECTS O ON O.OBJECT_NAME=I.INDEX_NAME "
                       "AND O.OBJECT_TYPE='INDEX' WHERE I.INDEX_NAME=:name", name=index)
        row = cursor.fetchone()
        if row is None:
            return {"status": "not_present", "table": manifest["table"], "index": index}
        check(row[0] == manifest["table"] and row[1] == "VECTOR" and "HNSW" in str(row[2]),
              "Refusing to retire an unrelated or non-HNSW index")
        cursor.execute('DROP INDEX "' + index + '"')
    verify_owner(connection, manifest)
    return {"status": "retired", "table": manifest["table"], "index": index,
            "index_object_id": row[3], "table_preserved": True}


class Evidence:
    def __init__(self, profile, directory, secrets=()):
        self.profile = profile
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700)
        self.secrets = secrets
        self.report = {"profile": profile, "status": "running", "cases": [],
                       "started_at": time.time(), "artifacts": str(self.directory)}

    def passed(self, case, name, **details):
        self.report["cases"].append({"id": case, "name": name, "status": "passed", **details})
        print(case + " PASS " + name, flush=True)

    def finish(self, error=None):
        expected = self.report.get("required_cases", REQUIRED[self.profile])
        actual = [item["id"] for item in self.report["cases"]]
        if error is None and (sorted(actual) != sorted(expected) or
                              any(item["status"] != "passed" for item in self.report["cases"])):
            error = "Required case inventory did not pass exactly once"
        self.report["status"] = "failed" if error else "passed"
        self.report["finished_at"] = time.time()
        if error:
            self.report["error"] = redact(str(error), self.secrets)
            print("FAIL " + self.report["error"], flush=True)
        self.report = sanitize(self.report, self.secrets)
        write_json(self.directory / "report.json", self.report)
        failed_cases = [item for item in self.report["cases"] if item["status"] != "passed"]
        summary_failure = bool(error) and not failed_cases
        suite = ET.Element("testsuite", name=self.profile,
                           tests=str(len(self.report["cases"]) + summary_failure),
                           failures=str(len(failed_cases) + summary_failure))
        for item in self.report["cases"]:
            case = ET.SubElement(suite, "testcase", name=item["id"] + " " + item["name"])
            if item["status"] != "passed":
                ET.SubElement(case, "failure", message=redact(item.get("error", "Case did not pass"), self.secrets))
        if summary_failure:
            case = ET.SubElement(suite, "testcase", name="required-profile-completion")
            ET.SubElement(case, "failure", message=self.report["error"])
        ET.ElementTree(suite).write(self.directory / "junit.xml", encoding="utf-8", xml_declaration=True)
        print("Report: " + str(self.directory / "report.json"), flush=True)
        return 1 if error else 0
