#!/usr/bin/env bash
# tools/secret_scan.sh — scan the repo (working tree or just-staged
# changes) for key material that should never be committed.
#
# Why this exists
# ----------------
# local_scribe's threat model says the master key, kc_half, yk_half,
# DR passphrase, derived service tokens, and the operator's YubiKey
# recipients never leave the operator's machine. The canonical homes
# for those artefacts are the macOS Keychain, ~/.config/local_scribe/,
# and ~/.cache/local_scribe/ — all outside this working tree. This
# script enforces that boundary at the git layer: if anything that
# *looks* like a key, an API token, or a PEM block ever lands in a
# tracked or staged file, the hook refuses the commit and points the
# operator at the canonical SECURITY.md guidance.
#
# Two modes:
#
#   * default          — scan the entire working tree (manual audit)
#   * --staged         — scan only what `git diff --cached` produces
#                        (use as a pre-commit hook)
#
# Exit codes:
#   0  — clean
#   1  — at least one finding
#   2  — invocation / environment error
#
# This script intentionally has zero non-stdlib dependencies. If
# trufflehog is on $PATH we use it for the entropy + regex layer,
# but a vanilla `git` + `grep` are sufficient for the high-signal
# pattern checks. Re-run after every install of a new secret-bearing
# tool (cosign keys, GH tokens for releases, etc.) and add new
# patterns here when needed.

set -euo pipefail

MODE="working_tree"
if [[ "${1:-}" == "--staged" ]]; then
  MODE="staged"
elif [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,/^set/p' "$0" | sed 's/^# \{0,1\}//' | head -n 30
  exit 0
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

# High-signal regexes. Each must be specific enough that a hit is
# almost certainly a real secret. Order: most specific → least.
#
# IMPORTANT: keep these patterns conservative. False positives turn
# the hook into noise that operators learn to bypass with --no-verify,
# which is exactly the failure mode this layer is meant to prevent.
declare -a PATTERNS=(
  # PEM private-key blocks (RSA, OpenSSH, EC, PGP, age, ENCRYPTED…)
  '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----'
  '-----BEGIN OPENSSH PRIVATE KEY-----'
  '-----BEGIN AGE ENCRYPTED FILE-----'
  '-----BEGIN PGP PRIVATE KEY BLOCK-----'

  # age plugin / age secret-key identifiers (private half).
  # Public ``age1…`` recipients are NOT secret and are not matched.
  'AGE-SECRET-KEY-1[A-Z0-9]{20,}'
  'AGE-PLUGIN-YUBIKEY-1[A-Z0-9]{20,}'

  # Vendor-prefixed API keys with the published canonical shape.
  'sk-[A-Za-z0-9_-]{32,}'             # OpenAI / generic "sk-" keys
  'sk-ant-[A-Za-z0-9_-]{32,}'         # Anthropic
  'AKIA[0-9A-Z]{16}'                  # AWS access key ID
  'ASIA[0-9A-Z]{16}'                  # AWS session token
  'ghp_[A-Za-z0-9]{36,}'              # GitHub personal access token
  'gho_[A-Za-z0-9]{36,}'              # GitHub OAuth
  'ghu_[A-Za-z0-9]{36,}'              # GitHub user-to-server
  'ghs_[A-Za-z0-9]{36,}'              # GitHub server-to-server
  'github_pat_[A-Za-z0-9_]{82,}'      # GitHub fine-grained PAT
  'xox[abprs]-[0-9a-zA-Z-]{20,}'      # Slack bot/user/refresh tokens

  # JWTs — three base64url chunks separated by dots. The minimum
  # plausible header is ``eyJhbGciOiJIUzI1NiJ9`` (20 chars), so we
  # need {10,} *after* the eyJ prefix to span the smallest real
  # header without inviting random-text false positives.
  'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'
)

# Files (or path prefixes) that should NEVER be committed even if
# the contents pass the regex layer. Listed by extension since the
# .gitignore already covers them as a primary defense; this is the
# belt-and-braces check.
declare -a FORBIDDEN_PATHS=(
  '\.age$'
  '\.pem$'
  '\.p12$'
  '\.pfx$'
  '\.jks$'
  '\.keystore$'
  '\.env$'
  '\.envrc$'
  '(^|/)id_rsa$'
  '(^|/)id_ed25519$'
  '(^|/)id_ecdsa$'
  '(^|/)id_dsa$'
  '(^|/)credentials\.json$'
)

# Files the regex layer should never look INSIDE (we still check
# their path against FORBIDDEN_PATHS above). Binary / vendored /
# build-output trees: scanning them produces false-positive noise
# AND wastes time.
declare -a SKIP_PATH_PATTERNS=(
  '^venv/'
  '^venv\.backup/'
  '^\.venv/'
  '^__pycache__/'
  '^.*/__pycache__/'
  '^\.pytest_cache/'
  '^\.mypy_cache/'
  '^node_modules/'
  '^bin/touchid-keychain$'
  '^\.git/'
  '\.pyc$'
  '\.so$'
  '\.dylib$'
)

skip_path() {
  local p="$1"
  for pat in "${SKIP_PATH_PATTERNS[@]}"; do
    if [[ "$p" =~ $pat ]]; then return 0; fi
  done
  return 1
}

# Resolve the file list to scan. In --staged mode this is what's
# actually about to be committed; in working-tree mode this is the
# full tracked + untracked set (minus .gitignore'd paths) so the
# manual audit catches anything sitting on disk that we haven't
# rejected yet.
list_files() {
  if [[ "$MODE" == "staged" ]]; then
    # Diff filter ACMR = Added, Copied, Modified, Renamed. -z for
    # null-delimited names so we survive unusual filenames.
    git diff --cached --name-only --diff-filter=ACMR -z |
      tr '\0' '\n'
  else
    {
      git ls-files
      git ls-files --others --exclude-standard
    } | sort -u
  fi
}

findings=0
report_finding() {
  if (( findings == 0 )); then
    echo "secret_scan: potential secret material detected:" >&2
    echo >&2
  fi
  findings=$((findings + 1))
  echo "  - $*" >&2
}

# Layer 1: filename / path checks (cheap, always run).
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  for pat in "${FORBIDDEN_PATHS[@]}"; do
    if [[ "$f" =~ $pat ]]; then
      report_finding "forbidden path: $f (matches /$pat/)"
    fi
  done
done < <(list_files)

# Layer 2: content checks against the high-signal regex list.
# We scan each non-skipped, non-binary file with `grep -n -E`.
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  [[ ! -f "$f" ]] && continue
  if skip_path "$f"; then continue; fi
  # Heuristic binary check: if `file -b` says "binary" / "data" /
  # "executable", skip the content scan.
  if file -b "$f" 2>/dev/null | grep -qiE '(binary|executable|data|image|archive)'; then
    continue
  fi
  for pat in "${PATTERNS[@]}"; do
    # -I = ignore binary, -n = line numbers, -E = ERE. We grep
    # against the file on disk in --staged mode too — the hook
    # runs before the commit, so the working-tree copy IS the
    # staged copy for ACMR diffs.
    # ``-e PATTERN`` (and ``--`` before the path) disambiguates
    # patterns that start with ``-`` — without this, grep treats
    # ``-----BEGIN PRIVATE KEY-----`` as flags and falls back to
    # printing usage instead of matching.
    if matches="$(grep -InE -e "$pat" -- "$f" 2>/dev/null || true)"; then
      if [[ -n "$matches" ]]; then
        # Filter known-public exclusions inline:
        #   * Sentry public DSN keys are emitted by Sentry SDKs as
        #     "https://<pubkey>@…sentry.io/<projid>" and are
        #     designed by Sentry to be public. We don't ship a
        #     real one, but Char's binary references one and that
        #     reference is reproduced verbatim in docs/CHAR_REVIEW.md.
        #   * Synthetic test fixtures use long runs of a single
        #     character ("AAAAAAA…", "0000…") so they don't trip
        #     real-secret heuristics.
        while IFS= read -r line; do
          if [[ "$line" =~ ingest\.(us\.)?sentry\.io ]]; then continue; fi
          if [[ "$line" =~ (AAAAAAAA|00000000|deadbeef|cafebabe) ]]; then continue; fi
          report_finding "$f: ${line}"
        done <<< "$matches"
      fi
    fi
  done
done < <(list_files)

# Layer 3: optional trufflehog pass. Only in working-tree mode —
# in --staged mode the pre-commit window is too small to scan the
# full git history every commit, and trufflehog's git mode wants
# committed objects anyway.
if [[ "$MODE" == "working_tree" ]] && command -v trufflehog >/dev/null 2>&1; then
  echo "secret_scan: running trufflehog regex layer (full history)…" >&2
  th_out="$(trufflehog --regex --entropy=False --max_depth 200 \
              "file://$(pwd)" 2>&1 || true)"
  if [[ -n "$th_out" ]]; then
    # trufflehog uses ANSI colors; strip them for the report.
    echo "$th_out" | sed -E 's/\x1b\[[0-9;]*m//g' >&2
    findings=$((findings + 1))
  fi
fi

if (( findings > 0 )); then
  cat >&2 <<'EOF'

secret_scan: refusing to proceed.

If a hit is a true positive:
  1. Untrack the file: `git rm --cached <path>`
  2. Add a covering pattern to .gitignore (and to this script if
     warranted)
  3. If the secret was ever committed, rotate it AND scrub the
     blob from history with git-filter-repo or the BFG. See
     SECURITY.md § "Defense layer 6 — Signed pinned config" for
     the runbook.

If a hit is a false positive:
  * Add the file path to SKIP_PATH_PATTERNS at the top of this
    script, OR add an inline exclusion to Layer 2 (the
    "known-public exclusions inline" block). Don't just bypass
    with --no-verify; future commits would silently re-trip.

To install this as a pre-commit hook:
  ./tools/install_git_hooks.sh
EOF
  exit 1
fi

echo "secret_scan: clean."
exit 0
