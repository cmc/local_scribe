"""End-to-end tests for Defense layer 7 — the secret-scan pre-commit
hook (``tools/secret_scan.sh`` + ``tools/install_git_hooks.sh``).

Why this file exists
--------------------

``SECURITY.md`` § "Defense layer 7 — secret-scan pre-commit hook"
documents a client-side guardrail against accidentally committing
PEM private keys, age secret keys, vendor API tokens (``sk-``,
``AKIA``, ``ghp_``, …), JWTs, and operator-state directories. Until
this audit, no test exercised the scanner — meaning a future edit
to ``tools/secret_scan.sh`` could silently break detection without
the test suite noticing.

The fix lives in this file. Each test class addresses a slice of
the scanner's contract:

  InstallerTests
        ``tools/install_git_hooks.sh`` writes a valid
        ``.git/hooks/pre-commit`` shim into a fresh working tree and
        is idempotent on re-run.

  PatternDetectionTests
        For every advertised secret class (PEM, age, ``sk-``,
        ``sk-ant-``, ``AKIA…``, ``ghp_…``, ``xox[abprs]-…``, JWT),
        the scanner emits a finding when that material lands in a
        staged file. Exit code 1 ⇒ the pre-commit hook would
        refuse the commit.

  AllowlistTests
        Documented inline exclusions (Sentry public DSN, synthetic
        ``AAAA…``/``00000000…``/``deadbeef…``/``cafebabe…`` test
        fixtures) do NOT trip the scanner. Without these, the hook
        would yell on every commit touching ``CHAR_REVIEW.md`` or
        the privacy-redaction fixtures in ``tests/char``, and
        operators would learn to bypass it with ``--no-verify`` —
        exactly the failure mode this layer is meant to prevent.

  ForbiddenPathTests
        ``.age`` / ``.pem`` / ``.env`` / ``id_rsa`` and friends are
        rejected by **path alone**, even if their contents would
        otherwise pass the regex layer.

All tests run inside a freshly-created throwaway git repo under
``tempfile.TemporaryDirectory`` so they cannot pollute the live
working tree, do not race with the operator's real ``.git/hooks/``,
and survive on CI machines that have no real Char installation.

Threat-model context
--------------------

These tests pin the *content* layer. The *path* layer (``.gitignore``)
is enforced by git itself and is covered separately by the routine
``git status`` check operators run anyway. The two together are the
client-side defense; the server-side complement (GitHub Push
Protection) is explicitly outside this project's scope and is not
asserted here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_SECRET_SCAN = _REPO / "tools" / "secret_scan.sh"
_INSTALLER = _REPO / "tools" / "install_git_hooks.sh"


# ---------------------------------------------------------------------------
# Helpers


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command with a clean environment so we don't inherit
    the operator's user.email / user.name / commit.gpgsign etc."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        # Disable any user-global hook / template directories so
        # ``git init`` produces a clean repo.
        "GIT_TEMPLATE_DIR": "",
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        env=env,
    )


def _init_repo(tmp: Path) -> None:
    """Initialise a fresh git working tree under ``tmp`` with the
    repo's ``tools/`` directory copied in so the hook + scanner are
    on disk at their expected paths.
    """
    _git(tmp, "init", "-q", "--initial-branch=main")
    tools_dst = tmp / "tools"
    tools_dst.mkdir(exist_ok=True)
    shutil.copy2(_SECRET_SCAN, tools_dst / "secret_scan.sh")
    shutil.copy2(_INSTALLER, tools_dst / "install_git_hooks.sh")
    (tools_dst / "secret_scan.sh").chmod(0o755)
    (tools_dst / "install_git_hooks.sh").chmod(0o755)


def _stage(tmp: Path, relpath: str, content: str) -> None:
    """Write ``content`` to ``relpath`` under ``tmp`` and stage it."""
    p = tmp / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(tmp, "add", relpath)


def _run_scanner(tmp: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the scanner from inside ``tmp`` as a git working tree.
    Returns the CompletedProcess so the caller can pin exit code +
    stderr."""
    return subprocess.run(
        [str(tmp / "tools" / "secret_scan.sh"), *args],
        cwd=tmp,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp),
        },
    )


# ---------------------------------------------------------------------------
# Installer


class InstallerTests(unittest.TestCase):
    """``tools/install_git_hooks.sh`` is what makes the secret-scan
    hook active for every contributor. Pin the contract: it writes
    an executable shim into ``.git/hooks/pre-commit`` that delegates
    to the version-controlled scanner, and is idempotent."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ls_secret_scan_install_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _init_repo(self.tmp)

    def _run_installer(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.tmp / "tools" / "install_git_hooks.sh")],
            cwd=self.tmp,
            capture_output=True,
            text=True,
        )

    def test_installer_writes_pre_commit_hook(self) -> None:
        proc = self._run_installer()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        hook = self.tmp / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook.exists(), "pre-commit hook was not created")
        # The hook should be executable.
        self.assertTrue(os.access(hook, os.X_OK),
                        f"hook is not executable: mode={hook.stat().st_mode:o}")

    def test_installed_hook_delegates_to_secret_scan(self) -> None:
        self._run_installer()
        hook = (self.tmp / ".git" / "hooks" / "pre-commit").read_text()
        # The hook is a thin shim. The key invariant is that it execs
        # the version-controlled scanner with --staged so future edits
        # to the scanner take effect without re-installing.
        self.assertIn("secret_scan.sh", hook,
                      "hook does not reference the scanner")
        self.assertIn("--staged", hook,
                      "hook does not run the scanner in --staged mode")

    def test_installer_is_idempotent(self) -> None:
        rc1 = self._run_installer().returncode
        rc2 = self._run_installer().returncode
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        # Hook still works after the second install.
        hook = self.tmp / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook.exists())

    def test_installer_refuses_outside_a_git_tree(self) -> None:
        # Remove .git, then re-run; the installer should exit non-zero.
        shutil.rmtree(self.tmp / ".git")
        proc = self._run_installer()
        self.assertNotEqual(proc.returncode, 0,
                            "installer should refuse non-git tree")


# ---------------------------------------------------------------------------
# Pattern detection — the high-signal regex catalog


class PatternDetectionTests(unittest.TestCase):
    """Every advertised secret class from ``tools/secret_scan.sh``
    must produce a finding when the file lands in the staged set.
    These are the cases the pre-commit hook protects against.

    If you add a new pattern to ``tools/secret_scan.sh``, add a
    corresponding test method here. The failure mode of NOT having
    this test is: a future edit to the regex catalog could
    inadvertently break a real pattern, and the operator wouldn't
    notice until a contributor committed a leaked key.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ls_secret_scan_pat_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _init_repo(self.tmp)

    def _assert_blocks(self, relpath: str, content: str, *, msg: str) -> None:
        _stage(self.tmp, relpath, content)
        proc = _run_scanner(self.tmp, "--staged")
        self.assertEqual(
            proc.returncode, 1,
            f"{msg}: scanner returned {proc.returncode}, stderr={proc.stderr!r}",
        )
        # The finding should mention the file we just staged so the
        # operator can find it. (Forbidden-path findings emit the
        # path; content findings prefix lines with it.)
        self.assertIn(relpath, proc.stderr)

    def test_pem_private_key_block(self) -> None:
        body = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxx" + "y" * 80 + "\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        self._assert_blocks("docs/notes.md", body,
                            msg="PEM private-key block should block")

    def test_openssh_private_key_block(self) -> None:
        body = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU" + "y" * 100 + "\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
        )
        self._assert_blocks("README.md", body,
                            msg="OpenSSH private key should block")

    def test_age_secret_key(self) -> None:
        body = "AGE-SECRET-KEY-1ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789QQQQ\n"
        self._assert_blocks("notes.md", body,
                            msg="age secret key should block")

    def test_age_plugin_yubikey_identity(self) -> None:
        body = "AGE-PLUGIN-YUBIKEY-1ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789Q\n"
        self._assert_blocks("setup.md", body,
                            msg="age-plugin-yubikey identity should block")

    # NOTE: every realistic-shape secret fixture below is assembled at
    # runtime via string concatenation. The literal vendor prefix +
    # body never appears statically in this file, which keeps
    # GitHub's server-side Secret Scanning (and our own L7 hook on
    # this very repo) from refusing the file when it lands. The
    # scanner-under-test still sees the fully-assembled string in
    # the staged content of the throwaway test repo, which is the
    # actual contract we're pinning.

    def test_openai_style_sk_key(self) -> None:
        secret = "sk" + "-" + "ProJ_ABCDEFGHijkLM1234567NOPQrstUVWXYZ"
        body = f"api_key = '{secret}'\n"
        self._assert_blocks("settings.py", body,
                            msg="sk- API key should block")

    def test_anthropic_sk_ant_key(self) -> None:
        secret = "sk" + "-ant-" + "ABCDEFGHijklmnopqrSTUV1234567890XYZ"
        body = f"ANTHROPIC = '{secret}'\n"
        self._assert_blocks("env.py", body,
                            msg="sk-ant- API key should block")

    def test_aws_access_key(self) -> None:
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        body = f"AWS_ACCESS_KEY_ID = '{secret}'\n"
        self._assert_blocks("aws.py", body,
                            msg="AWS access key should block")

    def test_github_personal_access_token(self) -> None:
        secret = "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789AB"
        body = f"GH = '{secret}'\n"
        self._assert_blocks("publish.py", body,
                            msg="GitHub PAT should block")

    def test_slack_token(self) -> None:
        secret = "xox" + "b-" + "1234567890-1234567890123-abcdefghijkLMNOP"
        body = f"SLACK = '{secret}'\n"
        self._assert_blocks("notify.py", body,
                            msg="Slack token should block")

    def test_jwt_three_part(self) -> None:
        # Realistic-shape JWT (header.payload.signature, all base64url).
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9"
            + "." + "eyJzdWIiOiJ0ZXN0IiwibmFtZSI6IkFsaWNlIn0"
            + "." + "abcdefghijklmnopQRSTUVWXYZ012345"
        )
        body = f"TOKEN = '{jwt}'\n"
        self._assert_blocks("auth.py", body,
                            msg="three-part JWT should block")


# ---------------------------------------------------------------------------
# Allowlist — documented public values that must NOT trip the scanner


class AllowlistTests(unittest.TestCase):
    """Inline allowlist exclusions documented in ``tools/secret_scan.sh``
    must not produce findings. If they did, the hook would yell on
    every commit touching ``CHAR_REVIEW.md`` (Sentry DSN) or the
    privacy-redaction fixtures in ``tests/char`` (``AAAA…`` etc),
    and operators would learn to ``--no-verify`` — exactly the
    failure mode this layer is meant to prevent.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ls_secret_scan_allow_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _init_repo(self.tmp)

    def _assert_allows(self, relpath: str, content: str, *, msg: str) -> None:
        _stage(self.tmp, relpath, content)
        proc = _run_scanner(self.tmp, "--staged")
        self.assertEqual(
            proc.returncode, 0,
            f"{msg}: scanner returned {proc.returncode}, stderr={proc.stderr!r}",
        )

    def test_sentry_public_dsn_allowed(self) -> None:
        # Sentry's public DSN is a real ``sk-`` looking string but is
        # by Sentry's own design a public token. The exclusion is
        # anchored on the ``ingest.sentry.io`` host substring.
        body = (
            "# Public DSN for Char's Sentry project\n"
            "https://abcdef0123456789abcdef0123456789@o12345.ingest.sentry.io/12345\n"
        )
        self._assert_allows("docs/CHAR_REVIEW.md", body,
                            msg="public Sentry DSN must be allowlisted")

    def test_synthetic_AAAA_fixture_allowed(self) -> None:
        # ``tests/char/test_char_audit.py`` uses runs of 'A' to fill
        # 32-char positions in fixture data. They look like real
        # secrets to a naive matcher but are visibly synthetic.
        synthetic = "sk" + "-" + ("A" * 36)
        body = f"fake_api_key = '{synthetic}'\n"
        self._assert_allows("tests/fixtures.py", body,
                            msg="long AAAA… synthetic fixture must be allowlisted")

    def test_synthetic_deadbeef_fixture_allowed(self) -> None:
        synthetic = "sk" + "-" + ("deadbeef" * 5)
        body = f"fake_key = '{synthetic}'\n"
        self._assert_allows("tests/fixtures.py", body,
                            msg="deadbeef… synthetic fixture must be allowlisted")

    def test_clean_repo_passes(self) -> None:
        # Sanity: no staged secrets ⇒ scanner exits 0 silently.
        _stage(self.tmp, "README.md", "# hello\n\nNothing secret here.\n")
        proc = _run_scanner(self.tmp, "--staged")
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ---------------------------------------------------------------------------
# Forbidden paths — rejected by name alone


class ForbiddenPathTests(unittest.TestCase):
    """``FORBIDDEN_PATHS`` is the belt-and-braces layer that catches
    files whose contents might pass the regex layer but whose
    *names* unambiguously identify them as secret-bearing
    (``.age`` / ``.pem`` / ``.env`` / ``id_rsa`` / …).

    A ``git add -f`` that bypasses ``.gitignore`` would otherwise
    sneak these in.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ls_secret_scan_fp_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _init_repo(self.tmp)

    def _assert_blocks(self, relpath: str) -> None:
        # Content is intentionally innocuous so we know the *path*
        # is what's triggering the finding.
        _stage(self.tmp, relpath, "innocuous\n")
        proc = _run_scanner(self.tmp, "--staged")
        self.assertEqual(
            proc.returncode, 1,
            f"forbidden path {relpath!r} should have blocked the commit; "
            f"stderr={proc.stderr!r}",
        )
        self.assertIn("forbidden path", proc.stderr,
                      "scanner should label this as a 'forbidden path' finding")

    def test_dot_age_extension_blocks(self) -> None:
        self._assert_blocks("secrets/identity.age")

    def test_dot_pem_extension_blocks(self) -> None:
        self._assert_blocks("certs/private.pem")

    def test_dot_p12_extension_blocks(self) -> None:
        self._assert_blocks("certs/store.p12")

    def test_dot_env_extension_blocks(self) -> None:
        self._assert_blocks(".env")

    def test_id_rsa_blocks(self) -> None:
        self._assert_blocks(".ssh/id_rsa")

    def test_id_ed25519_blocks(self) -> None:
        self._assert_blocks(".ssh/id_ed25519")

    def test_credentials_json_blocks(self) -> None:
        self._assert_blocks("aws/credentials.json")


if __name__ == "__main__":
    unittest.main()
