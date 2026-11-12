"""Offline validation; optional task-owned, nonpersistent Docker test services.

Run from Core root with python -B backend/tests/phase_7h4b_runner.py
--services [pytest paths/options]. Requires cached PostgreSQL/Redis images.
Never reads dotenv or connects to development service instances.
"""

import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError("Disposable Docker command failed: " + result.stderr)
    return result.stdout.strip()


def main():
    services = "--services" in sys.argv
    args = [value for value in sys.argv[1:] if value != "--services"]
    root = Path(__file__).resolve().parents[2]
    if Path.cwd() != root:
        raise RuntimeError("Run validation from the Core repository root")
    sys.path.insert(0, str(root / "backend"))
    os.environ.update(PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      DATABASE_URL="sqlite://", REDIS_URL="redis://127.0.0.1:1/0")
    for key in list(os.environ):
        if key.startswith("ARGUS_") and ("TEST_" in key or key.endswith("TEST_DATABASE_URL")):
            os.environ.pop(key)
    from pydantic_settings.sources import DotEnvSettingsSource
    DotEnvSettingsSource.__call__ = lambda self: {}

    task = "argus-core-7h4b-" + uuid4().hex[:12]
    containers = []
    network = None
    temp_root = Path(tempfile.mkdtemp(prefix=".phase7h4b-", dir=root))
    allowed_ports = set()
    try:
        if services:
            network = docker("network", "create", "--label", "phase=7h4b", task)
            for kind, image, port, extra in [
                ("postgres", "postgres:17-bookworm", 5432,
                 ["--tmpfs", "/var/lib/postgresql/data", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                  "-e", "POSTGRES_DB=argus_core_test"]),
                ("redis", "redis:8-alpine", 6379, ["--tmpfs", "/data"]),
            ]:
                command = ["run", "-d", "--pull=never", "--name", task + "-" + kind,
                           "--label", "phase=7h4b", "--network", network,
                           "-p", f"127.0.0.1::{port}", *extra, image]
                if kind == "redis":
                    command += ["redis-server", "--save", "", "--appendonly", "no"]
                identifier = docker(*command)
                containers.append(identifier)
                state = json.loads(docker("inspect", identifier))[0]
                assert state["Config"]["Labels"]["phase"] == "7h4b"
                assert state["Name"] == "/" + task + "-" + kind
                assert not any(mount["Type"] == "volume" for mount in state["Mounts"])
                binding = state["NetworkSettings"]["Ports"][f"{port}/tcp"]
                assert len(binding) == 1 and binding[0]["HostIp"] == "127.0.0.1"
                published = int(binding[0]["HostPort"])
                allowed_ports.add(published)
                if kind == "postgres":
                    os.environ["ARGUS_CORE_TEST_DATABASE_URL"] = f"postgresql+psycopg://postgres@127.0.0.1:{published}/argus_core_test"
                    os.environ["DATABASE_URL"] = os.environ["ARGUS_CORE_TEST_DATABASE_URL"]
                else:
                    redis_url = f"redis://127.0.0.1:{published}/0"
                    os.environ.update(REDIS_URL=redis_url, ARGUS_AI_TEST_REDIS_URL=redis_url,
                                      ARGUS_GDELT_TEST_REDIS_URL=redis_url, ARGUS_RSS_TEST_REDIS_URL=redis_url,
                                      ARGUS_DISCOVERY_TEST_REDIS_URL=redis_url)

        # Permit Windows' local socketpair construction and only our own ports.
        local = threading.local()
        original_pair, original_connect = socket.socketpair, socket.socket.connect
        original_getaddrinfo = socket.getaddrinfo

        def forbidden(*args, **kwargs):
            raise AssertionError("Offline validation blocked an unexpected network target")

        def permitted(address):
            return address[0] == "127.0.0.1" and address[1] in allowed_ports

        def guarded_connect(sock, address):
            if (getattr(local, "pair", False) and address[0] in {"127.0.0.1", "::1"}) or permitted(address):
                return original_connect(sock, address)
            return forbidden()

        def guarded_dns(host, port, *args, **kwargs):
            if host in {"127.0.0.1", b"127.0.0.1"} and int(port) in allowed_ports:
                return original_getaddrinfo(host, port, *args, **kwargs)
            return forbidden()

        def pair(*args, **kwargs):
            local.pair = True
            try:
                return original_pair(*args, **kwargs)
            finally:
                local.pair = False

        socket.socketpair = pair
        socket.socket.connect = guarded_connect
        socket.socket.connect_ex = forbidden
        socket.getaddrinfo = guarded_dns
        if sys.platform == "win32":
            # Windows async TCP uses overlapped ConnectEx, bypassing socket.connect.
            from asyncio.windows_events import IocpProactor
            original_async_connect = IocpProactor.connect

            def guarded_async_connect(proactor, conn, address):
                if permitted(address):
                    return original_async_connect(proactor, conn, address)
                return forbidden()

            IocpProactor.connect = guarded_async_connect

        if services:
            import redis
            import psycopg
            from sqlalchemy.engine import make_url
            database = make_url(os.environ["ARGUS_CORE_TEST_DATABASE_URL"])
            client = redis.Redis.from_url(redis_url, decode_responses=True)
            for attempt in range(80):
                try:
                    client.ping()
                    with psycopg.connect(host=database.host, port=database.port, user="postgres",
                                         dbname="argus_core_test", connect_timeout=1) as connection:
                        assert connection.execute("select current_database()").fetchone()[0] == "argus_core_test"
                    break
                except (redis.ConnectionError, psycopg.OperationalError):
                    if attempt == 79:
                        raise
                    time.sleep(0.25)
            assert client.dbsize() == 0
            assert client.config_get("save")["save"] == ""
            assert client.config_get("appendonly")["appendonly"] == "no"
            run_id = client.info("server")["run_id"]
            os.environ.update(ARGUS_AI_TEST_REDIS_RUN_ID=run_id, ARGUS_GDELT_TEST_REDIS_RUN_ID=run_id,
                              ARGUS_DISCOVERY_TEST_REDIS_RUN_ID=run_id)
            client.close()
            # Legacy source-list API cases use the global database dependency.
            # Initialize it only in our verified disposable control database.
            from sqlalchemy import create_engine
            from app.core.models import CoreBase
            engine = create_engine(os.environ["DATABASE_URL"])
            CoreBase.metadata.create_all(engine)
            engine.dispose()

        import pytest
        return pytest.main(["-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider",
                            "--basetemp=" + str(temp_root / "pytest"), *args])
    finally:
        for identifier in reversed(containers):
            docker("rm", "-f", identifier)
            assert not docker("ps", "-aq", "--filter", "id=" + identifier)
        if network:
            docker("network", "rm", network)
        if containers:
            print("Task-owned disposable containers and network removed.")
        resolved = temp_root.resolve()
        assert resolved.parent == root and resolved.name.startswith(".phase7h4b-")
        assert not temp_root.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        shutil.rmtree(resolved)
        assert not temp_root.exists()


if __name__ == "__main__":
    raise SystemExit(main())
