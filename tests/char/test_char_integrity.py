"""Unit tests for ``char_integrity.py``.

Real ``codesign`` / ``spctl`` / ``otool`` would require an actual
signed bundle. We instead use a fake-bundle directory + a fake
``_run`` that returns canned tool output. The verify() entry-point
is exercised end-to-end through the same code path the operator
hits — only the shell tool invocations are stubbed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from local_scribe.char import char_integrity


# --- fixtures ---------------------------------------------------------


def _write_fake_macho(path: Path, sha_seed: bytes = b"\x01") -> None:
    """A "Mach-O" file the enumerator will accept (matching magic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 64-bit Mach-O LE magic + padding.
    payload = b"\xcf\xfa\xed\xfe" + sha_seed * 32
    path.write_bytes(payload)
    path.chmod(0o755)


def _make_fake_bundle(root: Path) -> Path:
    """Build the minimum bundle structure: ``Char.app/Contents/MacOS/char``
    with valid Mach-O magic so ``_enumerate_macho`` finds it."""
    bundle = root / "Char.app"
    contents = bundle / "Contents" / "MacOS"
    _write_fake_macho(contents / "char", sha_seed=b"\xaa")
    _write_fake_macho(contents / "char-cli", sha_seed=b"\xbb")
    # Info.plist is irrelevant to our checks (we drive identifier
    # off codesign output, not the plist), but harmless to include.
    (bundle / "Contents" / "Info.plist").write_text(
        "<?xml version='1.0'?><plist></plist>",
    )
    return bundle


# A fake ``_run`` that drives all the shell tools char_integrity
# invokes. It dispatches on argv[0] + sub-commands.
def make_fake_run(*,
                  codesign_verify_rc: int = 0,
                  codesign_display: Optional[str] = None,
                  spctl_rc: int = 0,
                  spctl_output: Optional[str] = None,
                  otool_deps: Optional[list[str]] = None) -> callable:
    """Return a function that mirrors ``char_integrity._run``'s
    ``(rc, stdout, stderr)`` contract, using the canned values
    supplied for each tool."""
    if codesign_display is None:
        codesign_display = (
            "Executable=/x/Char.app/Contents/MacOS/char\n"
            "Identifier=com.hyprnote.stable\n"
            "Format=app bundle\n"
            "CandidateCDHashFull sha256="
            "a85c74f98c43679764d7fd2a80d4cca5b661876325e4536119a01b9ef450ecc8\n"
            "TeamIdentifier=6SLY7V277V\n"
            "Authority=Developer ID Application: Fastrepl, Inc. (6SLY7V277V)\n"
        )
    if spctl_output is None:
        spctl_output = (
            "/x/Char.app: accepted\n"
            "source=Notarized Developer ID\n"
        )
    if otool_deps is None:
        otool_deps = [
            "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
            " (compatibility version 150.0.0, current version 3502.1.255)",
            "/usr/lib/libc++.1.dylib (compatibility version 1.0.0, current version 1900.0.0)",
        ]
    otool_output = "/x/Char.app/Contents/MacOS/char:\n" + "\n".join(
        f"\t{d}" for d in otool_deps
    ) + "\n"

    def fake_run(cmd):
        argv0 = cmd[0]
        if argv0 == "codesign":
            if "--verify" in cmd:
                return (codesign_verify_rc, "", "" if codesign_verify_rc == 0 else "codesign FAILED")
            if "--display" in cmd:
                return (0, "", codesign_display)
        elif argv0 == "spctl":
            return (spctl_rc, "", spctl_output)
        elif argv0 == "otool":
            return (0, otool_output, "")
        return (0, "", "")

    return fake_run


# --- core verification tests ----------------------------------------


class HappyPathTests(unittest.TestCase):
    """A fake bundle with everything signed correctly + baseline
    present → clean."""

    def test_clean_when_baseline_matches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            with patch.object(char_integrity, "_run", make_fake_run()):
                # First, record the baseline.
                fp = char_integrity.collect_fingerprint(bundle)
                self.assertIsNotNone(fp)
                self.assertEqual(fp.team_id, char_integrity.PINNED_TEAM_ID)
                self.assertEqual(fp.bundle_id, char_integrity.PINNED_BUNDLE_ID)
                self.assertEqual(len(fp.mach_os), 2)
                char_integrity.save_baseline(fp, baseline_path)
                # Then verify against it.
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertTrue(rep.clean,
                                msg=f"unexpected drifts: {rep.drifts}")

    def test_first_run_no_baseline_demands_one(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            with patch.object(char_integrity, "_run", make_fake_run()):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)
                kinds = {d.kind for d in rep.drifts}
                self.assertIn("cdhash", kinds)
                msgs = " ".join(d.message for d in rep.drifts)
                self.assertIn("no recorded Char baseline", msgs)

    def test_baseline_not_required_mode(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = _make_fake_bundle(Path(td))
            with patch.object(char_integrity, "_run", make_fake_run()):
                rep = char_integrity.verify(
                    bundle, Path(td) / "no.json",
                    require_baseline=False,
                )
                self.assertTrue(rep.clean,
                                msg=f"unexpected drifts: {rep.drifts}")


class DriftDetectionTests(unittest.TestCase):
    """Each individual drift type must surface in `report.drifts`."""

    def _baseline_for(self, bundle: Path, baseline_path: Path) -> None:
        with patch.object(char_integrity, "_run", make_fake_run()):
            fp = char_integrity.collect_fingerprint(bundle)
            char_integrity.save_baseline(fp, baseline_path)

    def test_codesign_verify_failure_surfaces(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            self._baseline_for(bundle, baseline_path)
            with patch.object(
                char_integrity, "_run",
                make_fake_run(codesign_verify_rc=1),
            ):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)
                kinds = [d.kind for d in rep.drifts]
                self.assertIn("codesign", kinds)

    def test_team_id_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            self._baseline_for(bundle, baseline_path)
            # Re-sign by an attacker with a different Team ID.
            wrong = (
                "Identifier=com.hyprnote.stable\n"
                "CandidateCDHashFull sha256="
                "a85c74f98c43679764d7fd2a80d4cca5b661876325e4536119a01b9ef450ecc8\n"
                "TeamIdentifier=BADBADBAD0\n"
            )
            with patch.object(
                char_integrity, "_run",
                make_fake_run(codesign_display=wrong),
            ):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)
                self.assertIn("team", {d.kind for d in rep.drifts})

    def test_bundle_id_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            self._baseline_for(bundle, baseline_path)
            wrong = (
                "Identifier=com.attacker.masquerade\n"
                "CandidateCDHashFull sha256="
                "a85c74f98c43679764d7fd2a80d4cca5b661876325e4536119a01b9ef450ecc8\n"
                "TeamIdentifier=6SLY7V277V\n"
            )
            with patch.object(
                char_integrity, "_run",
                make_fake_run(codesign_display=wrong),
            ):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)
                self.assertIn("bundle", {d.kind for d in rep.drifts})

    def test_spctl_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            self._baseline_for(bundle, baseline_path)
            with patch.object(
                char_integrity, "_run",
                make_fake_run(spctl_rc=3,
                              spctl_output="/x/Char.app: rejected\nsource=no usable signature\n"),
            ):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)
                self.assertIn("spctl", {d.kind for d in rep.drifts})

    def test_linkage_to_homebrew_is_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            self._baseline_for(bundle, baseline_path)
            with patch.object(
                char_integrity, "_run",
                make_fake_run(otool_deps=[
                    "/opt/homebrew/lib/libcurl.4.dylib "
                    "(compatibility version 7.0.0, current version 9.0.0)",
                ]),
            ):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)
                self.assertIn("linkage", {d.kind for d in rep.drifts})

    def test_cdhash_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            self._baseline_for(bundle, baseline_path)
            new_hash = "ff" * 32
            mutated = (
                "Identifier=com.hyprnote.stable\n"
                f"CandidateCDHashFull sha256={new_hash}\n"
                "TeamIdentifier=6SLY7V277V\n"
            )
            with patch.object(
                char_integrity, "_run",
                make_fake_run(codesign_display=mutated),
            ):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)
                self.assertIn("cdhash", {d.kind for d in rep.drifts})

    def test_macho_sha_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            self._baseline_for(bundle, baseline_path)
            # Tamper with the on-disk bytes of one of the Mach-Os.
            _write_fake_macho(
                bundle / "Contents" / "MacOS" / "char",
                sha_seed=b"\x99",
            )
            with patch.object(
                char_integrity, "_run", make_fake_run(),
            ):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)
                kinds = [d.kind for d in rep.drifts]
                self.assertIn("cdhash", kinds)
                # CDHash itself matches; only the per-file sha256 changed.
                msgs = " ".join(d.message for d in rep.drifts)
                self.assertIn("Contents/MacOS/char", msgs)

    def test_extra_macho_added_to_bundle(self):
        # Attacker drops a helper dylib into the bundle.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            self._baseline_for(bundle, baseline_path)
            _write_fake_macho(
                bundle / "Contents" / "Frameworks" / "evil.dylib",
                sha_seed=b"\xcc",
            )
            with patch.object(char_integrity, "_run", make_fake_run()):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)
                msgs = " ".join(d.message for d in rep.drifts)
                self.assertIn("evil.dylib", msgs)

    def test_macho_removed_from_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _make_fake_bundle(root)
            baseline_path = root / "baseline.json"
            self._baseline_for(bundle, baseline_path)
            (bundle / "Contents" / "MacOS" / "char-cli").unlink()
            with patch.object(char_integrity, "_run", make_fake_run()):
                rep = char_integrity.verify(bundle, baseline_path)
                self.assertFalse(rep.clean)


class SideLoadDetectionTests(unittest.TestCase):
    """DYLD_* env vars short-circuit the whole verify path so we
    never run codesign in a poisoned env."""

    def setUp(self):
        self._prior_env: dict[str, Optional[str]] = {}
        for k in char_integrity.DYLD_ENV_VARS:
            self._prior_env[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._prior_env.items():
            if v is not None:
                os.environ[k] = v

    def test_dyld_insert_libraries_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = _make_fake_bundle(Path(td))
            os.environ["DYLD_INSERT_LIBRARIES"] = "/tmp/evil.dylib"
            called: list[list[str]] = []

            def trap(cmd):
                called.append(cmd)
                return (0, "", "")

            with patch.object(char_integrity, "_run", trap):
                rep = char_integrity.verify(
                    bundle, Path(td) / "no.json",
                    require_baseline=False,
                )
            # No subprocess should run when DYLD_* is set.
            self.assertEqual(
                called, [],
                msg="codesign/spctl/otool ran while DYLD_* was set",
            )
            self.assertFalse(rep.clean)
            self.assertEqual(len(rep.drifts), 1)
            self.assertEqual(rep.drifts[0].kind, "sideload")
            self.assertIn("DYLD_INSERT_LIBRARIES",
                          rep.drifts[0].message)

    def test_dyld_library_path_also_detected(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = _make_fake_bundle(Path(td))
            os.environ["DYLD_LIBRARY_PATH"] = "/tmp"
            with patch.object(char_integrity, "_run", make_fake_run()):
                rep = char_integrity.verify(
                    bundle, Path(td) / "no.json",
                    require_baseline=False,
                )
            self.assertFalse(rep.clean)
            self.assertEqual(rep.drifts[0].kind, "sideload")

    def test_no_dyld_no_drift(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = _make_fake_bundle(Path(td))
            # Ensure DYLD_* is truly absent.
            for k in char_integrity.DYLD_ENV_VARS:
                self.assertNotIn(k, os.environ)
            with patch.object(char_integrity, "_run", make_fake_run()):
                rep = char_integrity.verify(
                    bundle, Path(td) / "no.json",
                    require_baseline=False,
                )
            self.assertTrue(rep.clean,
                            msg=f"unexpected drifts: {rep.drifts}")


# --- baseline persistence -------------------------------------------


class BaselineRoundTripTests(unittest.TestCase):

    def test_save_then_load(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = _make_fake_bundle(Path(td))
            p = Path(td) / "baseline.json"
            with patch.object(char_integrity, "_run", make_fake_run()):
                fp = char_integrity.collect_fingerprint(bundle)
            char_integrity.save_baseline(fp, p)
            loaded = char_integrity.load_baseline(p)
            self.assertEqual(loaded.cdhash_sha256_full, fp.cdhash_sha256_full)
            self.assertEqual(loaded.team_id, fp.team_id)
            self.assertEqual(len(loaded.mach_os), len(fp.mach_os))
            self.assertEqual(loaded.mach_os[0].sha256, fp.mach_os[0].sha256)

    def test_save_writes_0600(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = _make_fake_bundle(Path(td))
            p = Path(td) / "baseline.json"
            with patch.object(char_integrity, "_run", make_fake_run()):
                fp = char_integrity.collect_fingerprint(bundle)
            char_integrity.save_baseline(fp, p)
            mode = p.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600,
                             msg=f"baseline file mode {oct(mode)} is not 0600")

    def test_corrupt_baseline_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "baseline.json"
            p.write_text("{ not json")
            self.assertIsNone(char_integrity.load_baseline(p))


# --- enforcement ----------------------------------------------------


class EnforceOrDieTests(unittest.TestCase):

    def setUp(self):
        self._prior = os.environ.pop(char_integrity.ALLOW_DIRTY_ENV, None)
        # Also keep DYLD_* clean.
        self._dyld = {}
        for k in char_integrity.DYLD_ENV_VARS:
            self._dyld[k] = os.environ.pop(k, None)

    def tearDown(self):
        if self._prior is None:
            os.environ.pop(char_integrity.ALLOW_DIRTY_ENV, None)
        else:
            os.environ[char_integrity.ALLOW_DIRTY_ENV] = self._prior
        for k, v in self._dyld.items():
            if v is not None:
                os.environ[k] = v

    def test_dirty_without_override_raises(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = _make_fake_bundle(Path(td))
            baseline_path = Path(td) / "baseline.json"
            with patch.object(char_integrity, "_run", make_fake_run()):
                fp = char_integrity.collect_fingerprint(bundle)
                char_integrity.save_baseline(fp, baseline_path)
                # Force a CDHash drift.
                mutated = (
                    "Identifier=com.hyprnote.stable\n"
                    "CandidateCDHashFull sha256=" + "ee" * 32 + "\n"
                    "TeamIdentifier=6SLY7V277V\n"
                )
                with patch.object(
                    char_integrity, "_run",
                    make_fake_run(codesign_display=mutated),
                ):
                    rep = char_integrity.verify(bundle, baseline_path)
            buf = io.StringIO()
            with self.assertRaises(char_integrity.CharIntegrityError):
                char_integrity.enforce_or_die(rep, stream=buf)
            self.assertIn("CHAR INTEGRITY", buf.getvalue())

    def test_override_lets_dirty_pass(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = _make_fake_bundle(Path(td))
            baseline_path = Path(td) / "baseline.json"
            with patch.object(char_integrity, "_run", make_fake_run()):
                fp = char_integrity.collect_fingerprint(bundle)
                char_integrity.save_baseline(fp, baseline_path)
                # Force a CDHash drift.
                mutated = (
                    "Identifier=com.hyprnote.stable\n"
                    "CandidateCDHashFull sha256=" + "ee" * 32 + "\n"
                    "TeamIdentifier=6SLY7V277V\n"
                )
                with patch.object(
                    char_integrity, "_run",
                    make_fake_run(codesign_display=mutated),
                ):
                    rep = char_integrity.verify(bundle, baseline_path)
            os.environ[char_integrity.ALLOW_DIRTY_ENV] = "1"
            buf = io.StringIO()
            out = char_integrity.enforce_or_die(rep, stream=buf)
            self.assertIs(out, rep)
            self.assertIn("CHAR INTEGRITY", buf.getvalue())


class MissingBundleTests(unittest.TestCase):
    def test_no_char_app_is_missing_kind(self):
        with tempfile.TemporaryDirectory() as td:
            rep = char_integrity.verify(
                Path(td) / "does_not_exist.app",
                Path(td) / "no.json",
                require_baseline=False,
            )
            self.assertFalse(rep.clean)
            self.assertEqual(rep.drifts[0].kind, "missing")


if __name__ == "__main__":
    unittest.main()
