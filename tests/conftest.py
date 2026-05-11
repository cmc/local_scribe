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
    # Layer C (launch-session gate) is also disabled by default for
    # the same reason: the legacy E2E suites pre-date it and don't
    # mint a launch.lock. ``LaunchGateIntegrationTests`` (in
    # ``test_launch_session.py``) and the auth integration suites
    # re-enable it explicitly when they need to exercise the gate.
    os.environ.setdefault("LOCAL_SCRIBE_DISABLE_LAUNCH_GATE", "1")
    # Layer 0 — the SIP gate. The FastAPI service lifespans
    # (asr_server, inspector_server) refuse to start when SIP is
    # not fully enabled. CI machines and developer laptops may
    # legitimately have SIP off (e.g. for kernel work), so we fake
    # a "fully enabled" csrutil output by default. The dedicated
    # SIP-check tests in ``test_sip_check.py`` exercise both
    # directions explicitly by patching the env var. Tests that
    # specifically want to see the gate REJECT can override the
    # value in their own setUp.
    os.environ.setdefault(
        "LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT",
        "System Integrity Protection status: enabled.\n",
    )
