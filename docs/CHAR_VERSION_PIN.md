# Char version pin

> Moved from the top-level README on 2026-05-12 as part of
> the condense-and-link pass. The content below is the
> canonical reference. The README keeps a short pointer
> paragraph linking back here.
>
> **Related docs:** [`docs/CHAR_REVIEW.md`](CHAR_REVIEW.md), [`docs/CONFIGURE_CHAR.md`](CONFIGURE_CHAR.md)

Char is open-source and ships frequent updates. Some of those updates rename
keys in `settings.json`, change the multipart contract on
`POST /v1/audio/transcriptions`, or restructure the bundle. Any of those
would silently break our auto-config or pipeline.

To stop you from drifting into an untested combination, this repo pins a
specific Char build it has been end-to-end-validated against:

| field | value |
|---|---|
| Pinned version | `1.0.24` |
| Release tag | [`desktop_v1.0.24`](https://github.com/fastrepl/anarlog/releases/tag/desktop_v1.0.24) (2026-04-16) |
| arm64 DMG sha256 | `7f9c06881b9593b2aec17c8eddd65e5eb67d2c1072bfd008501989eb4181da89` |
| x86_64 DMG sha256 | `e7061d274308b563df724d7da5ede80e0cc68ff7082a3586b41ed8cc2c815503` |

Both SHAs and the version itself are constants (`CHAR_KNOWN_GOOD_VERSION`,
`CHAR_DMG_SHA256_AARCH64`, `CHAR_DMG_SHA256_X86_64`) at the top of `run.sh`.

### Installing the pinned version

```bash
./run.sh install-char
```

What it does:

1. Detects your CPU arch (`arm64` or `x86_64`).
2. Downloads the matching DMG from the GitHub Release shown above
   (≈600 MB on Apple Silicon, ≈125 MB on Intel).
3. Verifies the file's SHA256 against the constant in `run.sh`. **Refuses
   to install on mismatch** — that would mean either the release was
   retagged or the download was tampered with.
4. Mounts the DMG, copies `Char.app` (or `Hyprnote.app` if Char's old
   bundle name is still in there) to `/Applications`, unmounts.
5. Strips the macOS quarantine attribute so Gatekeeper doesn't pop the
   "downloaded from internet" warning the first time you launch (you've
   already opted in by verifying the pinned SHA).

If `Char.app` is already installed at the pinned version, this is a no-op.
If a *different* version is installed, it asks first (default No) before
replacing.

### When `run.sh` warns about drift

`./run.sh doctor`, `./run.sh configure-char`, and the bootstrap flow all
read `CFBundleShortVersionString` from `/Applications/Char.app` and compare
it to `CHAR_KNOWN_GOOD_VERSION`. If they don't match, you'll see:

```text
○ Char 1.0.27 installed; 1.0.24 pinned -- run `./run.sh install-char` to align
```

The warning never blocks — most patches are backwards-compatible — it just
flags that the auto-config flow hasn't been validated against your build.
If you hit weirdness after a Char update, downgrade with
`./run.sh install-char` and check whether the bug reproduces.

### Bumping the pin (for repo maintainers)

When a new Char release ships:

1. Download `hyprnote-macos-aarch64.dmg` and `.sha256`, plus the x86_64
   pair, from the new tag's release page.
2. Smoke-test end-to-end: record a call (live recording → Parakeet),
   import an existing audio file and click *Generate* (`/v1/audio/transcriptions`),
   confirm both work.
3. Update the four constants at the top of `run.sh`
   (`CHAR_KNOWN_GOOD_VERSION`, `CHAR_RELEASE_TAG` is derived,
   `CHAR_DMG_SHA256_AARCH64`, `CHAR_DMG_SHA256_X86_64`).
4. Update this section's table.

