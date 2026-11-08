"""Core composition must work when legacy consumer packages cannot be imported."""

import inspect
import os
from pathlib import Path
import subprocess
import sys

from conftest import CoreOnlyImports


def test_core_composes_without_legacy_application_packages():
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    for key in list(environment):
        if key.startswith("GLOBAL_CHESS_"):
            environment.pop(key)
    environment.update(DATABASE_URL="sqlite://", REDIS_URL="redis://127.0.0.1:1/0")
    program = """
import importlib.abc, json, pathlib, socket, sys
sys.path.insert(0, str(pathlib.Path('backend').resolve()))
from pydantic_settings.sources import DotEnvSettingsSource
DotEnvSettingsSource.__call__ = lambda self: {}
def forbidden(*args, **kwargs): raise AssertionError('Composition attempted network access')
socket.socket.connect = forbidden
""" + inspect.getsource(CoreOnlyImports) + """
sys.meta_path.insert(0, CoreOnlyImports())
from app import core_main
from app.core.models import CoreBase
from app.core.database import Base
assert core_main.app.title == 'Argus-Core'
assert set(CoreBase.metadata.tables) == {
 'core_sources', 'core_applications', 'core_api_keys', 'core_audit_logs',
 'core_source_catalogs', 'core_source_admission_events'}
assert not Base.metadata.tables
paths = core_main.app.openapi()['paths']
assert '/api/v1/discovery' in paths and '/api/v1/ai/generate' in paths
assert '/ingest' not in paths and '/topics' not in paths
expected = json.loads(pathlib.Path('backend/tests/fixtures/core_openapi.json').read_text())
assert core_main.app.openapi() == expected
core_main.audit_sessions.kw['bind'].dispose()
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program],
        cwd=Path(__file__).resolve().parents[2], env=environment,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
