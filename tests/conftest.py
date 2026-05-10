"""Test-wide defaults.

Most of the legacy E2E suites were written before the per-service bearer
token gating landed and do not stand up the FastAPI lifespan, so they have
no derived ``_asr_token`` / ``_inspector_token`` to send. Rather than retrofit
every test, we default the entire test process to the documented
``LOCAL_SCRIBE_DISABLE_AUTH=1`` bypass.

The new auth integration suites (``AsrServerAuthIntegrationTests`` /
``AuthTests``) explicitly remove this env var in their ``setUpClass`` so
they exercise the real bearer-token path. That contract is what keeps the
auth tests honest even with this conftest in place.

If you want to run the suite against the *enforced* code path manually:

    unset LOCAL_SCRIBE_DISABLE_AUTH
    ./venv/bin/python -m pytest tests/test_service_auth.py \
                                tests/test_asr_server.py::AsrServerAuthIntegrationTests \
                                tests/test_inspector_server.py::AuthTests
"""

from __future__ import annotations

import os


def pytest_configure(config):  # noqa: ARG001  (pytest hook signature)
    os.environ.setdefault("LOCAL_SCRIBE_DISABLE_AUTH", "1")
