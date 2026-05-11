# Key Safety: data-loss scenarios and mitigations

This document enumerates **every scenario** in which the operator
could lose access to encrypted local_scribe data (vault, audio,
transcripts) through a key-management mistake, and describes the
safety mechanism that stands between the mistake and the data loss.

The two universal invariants this design enforces:

1. **Physical presence is required for every key-change.** Each
   destructive operation forces a tap of the currently-enrolled
   YubiKey *before* any state changes. The only ops that can run
   without a tap are the explicit "I have lost my YubiKey"
   recovery paths (`dr-restore --no-reinit`, `destroy --no-presence`),
   and both require additional typed confirmations.
2. **Every destructive op is reversible until you say otherwise.**
   Before mutating any key artefact, a snapshot of the soon-to-be-
   replaced material is written to
   `~/.config/local_scribe/key-backups/<timestamp>-<op>/`. Snapshots
   are NEVER auto-pruned — only the operator can dispose of one,
   and that itself requires a typed `DELETE`.

Together: even if you press the wrong button at 3 am, your YubiKey
must be plugged in *and tapped* to do anything, and whatever just
happened can be rolled back from the on-disk snapshot.

**See also.** Where this document enumerates the *operational*
failure modes around key state changes, [`CRYPTO.md`](../CRYPTO.md)
documents the *cryptographic* choices (XOR split, HKDF-SHA256, `age`
+ X25519 to a YubiKey PIV slot, scrypt-via-`age -p` for the
disaster-recovery passphrase) and why each one was picked over the
alternatives. The two documents are complementary: if you're
designing a recovery flow, read this one; if you're evaluating
whether a primitive choice is sound, read that one.

---

## Table of contents

* [Threat surfaces](#threat-surfaces) — where mistakes can land
* [Scenario catalogue](#scenario-catalogue) — full enumeration with mitigations
* [Recovery flowchart](#recovery-flowchart)
* [What we explicitly cannot defend against](#what-we-explicitly-cannot-defend-against)
* [Pre-install checklist](#pre-install-checklist)
* [Operator commands quick reference](#operator-commands-quick-reference)

---

## Threat surfaces

The artefacts that, if lost or corrupted, render encrypted data
unreadable:

| Artefact | Where it lives | Sensitivity |
|---|---|---|
| `master_key_kc_half_v2` | macOS Keychain, Touch ID-gated | half of the master key |
| `yk_half.age` | `~/.config/local_scribe/yk_half.age` | half of the master key, age-encrypted to the YubiKey recipient(s) |
| `yubikey_recipients.txt` | `~/.config/local_scribe/yubikey_recipients.txt` | public; loss = inconvenience, not data loss |
| `disaster_recovery.age` | `~/.config/local_scribe/disaster_recovery.age` | whole master key, age-encrypted with a passphrase (`age -p`) |
| `master_key_kc_half_v2_backup_<ts>` | macOS Keychain, Touch ID-gated, one per preflight snapshot | snapshot of a prior kc_half |
| YubiKey hardware | physically | the only thing that can decrypt `yk_half.age` |
| DR passphrase | operator's memory / password manager | the only thing that can decrypt `disaster_recovery.age` |

The **master key** is never on disk in unwrapped form. It is
reconstructed at unlock time by XORing `kc_half ⊕ yk_half`. Losing
*both halves* destroys access; losing *one* is recoverable as long
as the other half + the recipient/passphrase still exist.

---

## Scenario catalogue

Each scenario is keyed by the action that triggers it. "Mitigation"
describes what local_scribe does to make the action either safe-
by-design or recoverable. "Override" describes the explicit knob a
sufficiently motivated operator must turn to break the mitigation.

### S1. `./run.sh key init --force` overwrites an existing v2 install

* **What can go wrong:** generates a fresh master key, replacing
  both halves. Any data encrypted under the old master is
  unrecoverable unless DR was set up *and* the DR passphrase still
  works.
* **Mitigation:**
  1. Refuses to proceed without `--force` when v2 is present (this
     check has always existed).
  2. Requires the operator to type `REPLACE` on stdin (TTY).
  3. Requires a YubiKey tap on the *current* enrolled key, proving
     physical possession before anything changes.
  4. Writes a pre-flight snapshot to
     `~/.config/local_scribe/key-backups/<ts>-init-force/` so the
     old halves remain recoverable via
     `./run.sh key backups restore-kc-half <account>` + copying
     `yk_half.age` back.
* **Override path:** `./run.sh key init --force`, type `REPLACE`,
  tap the YubiKey. (No way to skip the tap.)

### S2. `./run.sh key rotate` while vault is wired but not re-keyed

* **What can go wrong:** rotation produces a new master key.
  Anything previously sealed with the *old* master (vault keybag,
  HKDF-derived service tokens cached on disk) becomes unreadable
  with the new master.
* **Mitigation:**
  1. Requires typed `ROTATE` confirmation.
  2. Unlock of the OLD key is required to begin rotation — that
     unlock itself requires a YubiKey tap (physical presence).
  3. Pre-flight snapshot of BOTH halves + DR file is written
     *before* the new key is drawn, so the previous key remains
     reconstructible from the snapshot for as long as the snapshot
     directory + the matching Keychain backup account exist.
  4. Returns BOTH the old and new MasterKey handles so the caller
     can re-wrap any downstream artefacts (vault keybag) in the
     same transaction.

  > **Pending wiring:** the vault re-encrypt step is the next
  > implementation milestone. Until it's done, rotation is
  > effectively a "split-key plumbing smoke test" — the run.sh
  > preamble warns the operator about this.

* **Override path:** none. Rotation cannot skip the YubiKey tap.

### S3. `./run.sh key dr-restore` silently overwrites a live v2 install

* **What can go wrong:** DR file holds a master key that may be
  *older* than the live one (if you rotated since the DR was
  written). Re-initialising the split-key flow with the recovered
  master overwrites the live `kc_half` + `yk_half.age`, after which
  any data encrypted under the live key becomes unreadable.
* **Mitigation:**
  1. `dr_restore()` detects a live v2 install and **refuses** to
     re-init unless `overwrite_existing_v2=True` is passed.
  2. The shell wrapper requires the operator to type
     `RESTORE-AND-OVERWRITE` before passing that flag.
  3. The wrapper warns about the "DR is older than live key" race
     in plain English.
  4. Pre-flight snapshot of the live halves + DR file is written
     before any overwrite.
* **Override path:** `./run.sh key dr-restore`, type
  `RESTORE-AND-OVERWRITE`, tap YubiKey for the snapshot. If the
  YubiKey is genuinely lost, use `--no-reinit` to skip re-init
  entirely — you'll get the master in-memory only and routine
  unlock won't work until you re-enroll a key.

### S4. `./run.sh key add-yubikey` writes a corrupt `yk_half.age`

* **What can go wrong:** re-wrapping `yk_half` to a larger
  recipient set fails partway through. If the partial write
  replaces the old file before the new ciphertext is committed,
  the operator's existing YubiKey can no longer decrypt it.
* **Mitigation:**
  1. Pre-flight snapshot of `yk_half.age` + `yubikey_recipients.txt`
     before the re-wrap.
  2. `yubikey_backup.add_recipient()` writes the new ciphertext to
     a tempfile and `os.replace()`s it into position (atomic on
     POSIX).
  3. The decrypt step that obtains the current `yk_half` plaintext
     IS the physical-presence proof — a corrupt or absent YubiKey
     fails the check and the re-wrap never starts.
* **Override path:** none.

### S5. `./run.sh key migrate` from v1 leaves the install in a partial state

* **What can go wrong:** legacy v1 (whole-key Keychain item) is
  split into v2 halves. If the round-trip verify fails between
  writing v2 and deleting v1, the operator could be left with
  both, or with neither, depending on the failure mode.
* **Mitigation:**
  1. Pre-flight snapshot of the v1 Keychain item (copied to a
     `master_key_v1_backup_<ts>` account) before split.
  2. v2 written *before* v1 deletion.
  3. Round-trip-verify (encrypt-decrypt-compare) before v1 is
     deleted; failure leaves v1 in place.
  4. `unlock_master_key()` re-triggers migrate on subsequent
     calls if v1 is still present.
* **Override path:** none.

### S6. `./run.sh key destroy` wipes everything by mistake

* **What can go wrong:** the operator runs destroy intending to
  wipe a test install, but actually has live data they wanted to
  keep. Every artefact is removed.
* **Mitigation:**
  1. Typed `DESTROY` confirmation in the shell.
  2. YubiKey tap before any deletion (proves physical possession;
     a remote attacker with shell access cannot run destroy).
  3. Pre-flight snapshot (`EVERYTHING` scope) including kc_half
     copy to backup Keychain account, `yk_half.age` copy,
     `disaster_recovery.age` copy — every artefact captured.
  4. The destroy invocation prints the snapshot path so the
     operator immediately knows how to undo. Recovery is
     `./run.sh key backups restore-kc-half <account>` plus
     `cp <snapshot>/yk_half.age ~/.config/local_scribe/`.
* **Override path:** `./run.sh key destroy --no-presence` (for
  the lost-YubiKey case) or `./run.sh key destroy
  --purge-everything` (requires a SECOND typed confirmation —
  `PURGE-EVERYTHING` — and is the only operation in the entire
  system that produces an irreversible state).

### S7. `./run.sh key destroy --purge-everything` is THE irreversible op

* **What can go wrong:** the operator wants a true zero state and
  asks for it. There is no recovery from this point if they were
  wrong.
* **Mitigation:**
  1. TWO typed confirmations (`DESTROY` and `PURGE-EVERYTHING`).
  2. YubiKey tap (unless `--no-presence`).
  3. All key-backup snapshots under `~/.config/local_scribe/key-
     backups/` are removed.
  4. Every Keychain backup account matching
     `master_key_kc_half_v2_backup_*` and
     `master_key_v1_backup_*` is deleted.
  5. The shell preamble explicitly tells the operator that this is
     the only op without rollback.
* **Override path:** this IS the override path for the other
  destroy mode. There is no override that skips both
  confirmations.

### S8. `./run.sh key backups prune <id>` deletes a snapshot

* **What can go wrong:** the operator prunes the only snapshot
  that preserved a recoverable key state, then later realises they
  needed it.
* **Mitigation:**
  1. Typed `DELETE` confirmation.
  2. The shell preamble identifies *which* snapshot is being
     pruned and warns that the rollback path for that operation
     will no longer exist.
  3. `prune_backup()` refuses path traversal (will not delete
     anything outside `~/.config/local_scribe/key-backups/`).
* **Override path:** the typed `DELETE` itself is the override.
  There's no further gate — pruning is the operator's explicit
  decision to forget.

### S9. Operator deletes `~/.config/local_scribe/yk_half.age` manually

* **What can go wrong:** `rm yk_half.age` outside of the run.sh
  flow bypasses every mitigation. If the operator doesn't have a
  snapshot, the data encrypted under the current master is gone.
* **Mitigation:** out-of-band — we can't enforce filesystem ACLs
  against a user with rwx in their own homedir.
  - Documented loudly in this file + `SECURITY.md`.
  - `./run.sh doctor` reports drift (kc_half present + yk_half.age
    missing → "split-key partial state, run `./run.sh key
    backups list` to find a recoverable snapshot").
* **Override path:** the operator manually deleted the file. No
  in-process override exists.

### S10. Operator deletes the Keychain item via Keychain Access.app

* **What can go wrong:** Keychain Access.app lets the user delete
  any item they own without a re-prompt. The Touch ID ACL gates
  *reading*, not deletion.
* **Mitigation:** out-of-band.
  - Documented in `CHAR_REVIEW.md` and `SECURITY.md`.
  - We use a non-obvious account name (`master_key_kc_half_v2`)
    that wouldn't be deleted by accident if the operator is just
    grooming legacy items.
  - `./run.sh doctor` detects "yk_half.age present + kc_half
    Keychain item missing" and surfaces it as drift.
* **Override path:** none in-process.

### S11. `security delete-generic-password -s local_scribe -a master_key_kc_half_v2`

* **What can go wrong:** macOS `security` CLI lets you delete a
  Keychain item without Touch ID, given a current Keychain unlock
  password (the user's login password). Effectively the same as
  S10.
* **Mitigation:** same as S10.
* **Override path:** none in-process.

### S12. YubiKey physical loss / damage / factory-reset

* **What can go wrong:** `yk_half.age` is decryptable only by the
  enrolled YubiKey. If the hardware is gone, the file becomes a
  brick.
* **Mitigation:**
  1. `./run.sh key init` walks the operator through writing a
     **disaster-recovery passphrase** by default. The DR file
     holds the whole master key encrypted via `age -p`; the
     passphrase is the only thing that can decrypt it. (Operator
     can opt out with `--no-dr`; the shell warns this means YubiKey
     loss = data loss.)
  2. `./run.sh key add-yubikey` lets the operator enroll a
     *second* YubiKey so two hardware tokens can each decrypt
     `yk_half.age`. Strongly recommended.
  3. `./run.sh key dr-restore` is the recovery path: prompt for
     the DR passphrase, recover the master key, re-init the
     split-key flow with a new YubiKey.
* **Override path:** N/A — this is the scenario, not an
  operation.

### S13. DR passphrase forgotten

* **What can go wrong:** DR file is the recovery-of-last-resort.
  If the passphrase is gone, the DR file is a brick.
* **Mitigation:**
  1. The shell `./run.sh key init` prompts for the passphrase
     twice and aborts if they don't match.
  2. The doctor report shows "disaster_recovery.age present" so
     the operator knows it exists.
  3. We document strongly that the passphrase should go in a
     reputable password manager BEFORE leaving init.
* **Override path:** none — if you've lost both the YubiKey AND
  the passphrase, the encrypted data is gone.

### S14. macOS reinstall / new machine

* **What can go wrong:** Keychain isn't migrated to the new OS
  install. `kc_half` is gone. If `yk_half.age` was on a separate
  Time Machine backup, the operator still has half a key.
* **Mitigation:**
  1. DR file copy-able to the new machine alongside the rest of
     `~/.config/local_scribe/`. DR-restore brings the master key
     back.
  2. `./run.sh key add-yubikey` to a *second* hardware token, kept
     in a safe deposit box, provides an alternate decrypt path for
     `yk_half.age`.
* **Override path:** N/A.

### S15. Time Machine restore predates a key rotation

* **What can go wrong:** TM restore brings back an older
  `yk_half.age`. Combined with the current Keychain `kc_half`, the
  reconstructed master key doesn't match the one that encrypted
  the current vault → vault undecryptable.
* **Mitigation:**
  1. Every rotate writes a pre-flight snapshot. After a TM restore
     to an older state, the operator can `./run.sh key backups
     list`, identify the matching `rotate` snapshot, and
     `restore-kc-half` the corresponding Keychain account to bring
     `kc_half` back in sync with the TM-restored `yk_half.age`.
  2. The doctor report compares the kc_half/yk_half pair-hash to
     a vault sentinel and flags mismatches (TODO — pending vault
     wire-up).
* **Override path:** N/A.

### S16. Concurrent destroy / rotate during an active recording

* **What can go wrong:** while Char is actively writing audio /
  the ASR pipeline is producing transcripts, the operator rotates
  the master key. The in-flight encrypted bytes are sealed under
  the *old* key but the new key is now resident.
* **Mitigation:**
  1. **Sequencing:** key-lifecycle operations are CLI commands; we
     don't ship a path in the UI to rotate while services are
     running. `./run.sh stop` is the documented prerequisite.
  2. `./run.sh key rotate` returns BOTH old and new MasterKey
     handles so the caller (vault re-keying code, when wired) can
     migrate in-flight artefacts atomically.
  3. The launch-session gate (`launch.lock`) closes when
     `./run.sh stop` runs; subsequent service requests are
     rejected, so a rotate that happens between stop and start
     can't be racing live traffic.
* **Override path:** N/A — bypassing the recommended sequencing
  is itself the failure mode.

### S17. Snapshot directory pruned by external tool (e.g. CleanMyMac)

* **What can go wrong:** the operator runs a "clean up old
  files" tool that wipes
  `~/.config/local_scribe/key-backups/`. Snapshots vanish without
  the operator's awareness.
* **Mitigation:**
  1. We chmod the directory `0700` so it doesn't look like junk to
     conservative cleaners.
  2. Backups are placed under `~/.config/`, which most cleaners
     leave alone by default.
  3. `./run.sh doctor` reports snapshot count + ages so the
     operator notices if the count drops.
* **Override path:** N/A.

### S18. Keychain account name collision (1-second timestamp granularity)

* **What can go wrong:** two preflight snapshots taken in the same
  second would generate the same backup account name. The second
  would conflict with the first.
* **Mitigation:** `preflight_backup()` detects same-second
  collisions and adds a microsecond suffix to the snapshot
  directory. The Keychain account name is independent
  (`master_key_kc_half_v2_backup_<ts>` — same timestamp prefix)
  but Keychain store-on-existing-account is a replace, so a true
  collision would silently overwrite the prior backup. We further
  defend by:
  1. Detecting any existing account with the same name and
     deferring by one second if so (TODO — pending implementation
     hardening).
  2. Making backup ops one-at-a-time in the run.sh wrapper.
* **Override path:** N/A.

---

## Recovery flowchart

```
       ┌──────────────────────────────────┐
       │  Cannot unlock the master key    │
       └──────────────────┬───────────────┘
                          │
                          ▼
            ┌─────────────────────────┐
            │ Is YubiKey present and  │
            │   functional?           │
            └───┬─────────────────┬───┘
                │YES              │NO
                ▼                 ▼
   ┌────────────────────┐  ┌──────────────────────────┐
   │ kc_half in         │  │ Do you have the DR       │
   │   Keychain?        │  │   passphrase?            │
   └─┬──────────────┬───┘  └────┬──────────────────┬──┘
     │YES           │NO         │YES               │NO
     ▼              ▼           ▼                  ▼
  unlock works   try latest   key dr-restore     try latest
  (Touch ID +    snapshot's   (gets master back; snapshot's
   tap)          restore-kc-  re-enrolls new     restore-kc-
                 half         YubiKey)           half + age-d
                                                 of an old
                                                 yk_half.age
```

If you reach the bottom-right leaf (no YubiKey, no DR, no
snapshot), the encrypted data is permanently lost.

---

## What we explicitly cannot defend against

The following are *outside* the trust boundary of this design.
Documenting them so an operator knows what NOT to rely on us for:

* **Filesystem-level deletion.** Anyone with shell access can
  `rm -rf ~/.config/local_scribe/`. We chmod 0700 and document
  loudly but cannot stop it.
* **Keychain Access.app deletion.** The Touch ID ACL only gates
  reading; deletion only requires a Keychain unlock password
  (your login password, typically). Same for `security delete-
  generic-password`. This is a macOS-level limit.
* **A coerced operator tapping the YubiKey.** Physical presence
  proves possession, not consent. If someone forces the operator
  to tap, the gate opens.
* **Operator memory loss.** A forgotten DR passphrase is gone.
  Password managers exist for a reason — use one.
* **A motivated attacker with root + DTRace.** They can read
  master-key bytes out of the process's heap once it's unlocked.
  Our defense is to keep the unlock window short and zero
  buffers on the way out, but we are not a kernel rootkit
  defense.

---

## Pre-install checklist

Before running `./run.sh bootstrap` for the first time, the
operator should have:

- [ ] **Two YubiKeys** plugged in or available. Enroll the second
      with `./run.sh key add-yubikey` immediately after init.
- [ ] A **disaster-recovery passphrase** generated by a password
      manager, stored in that manager + a printed copy in a safe.
      DO NOT use a passphrase you might forget — the DR file is
      the recovery path of last resort.
- [ ] An **off-machine backup strategy** for
      `~/.config/local_scribe/disaster_recovery.age`. (The DR file
      is passphrase-encrypted; it's safe to copy to a USB drive,
      another machine, etc.)
- [ ] Knowledge that **`./run.sh key destroy --purge-everything`
      has no recovery path**. Everything else does.

---

## Operator commands quick reference

| Goal | Command | Confirmations | Snapshot? |
|---|---|---|---|
| Inspect key state | `./run.sh key status` | — | — |
| First-time setup | `./run.sh key init` | passphrase x2 | — |
| Overwrite v2 install | `./run.sh key init --force` | `REPLACE`, YK tap | yes (`init-force`) |
| Rotate master key | `./run.sh key rotate` | `ROTATE`, YK tap | yes (`rotate`) |
| Add a YubiKey | `./run.sh key add-yubikey <recipient>` | YK tap | yes (`add-yubikey`) |
| Recover from DR | `./run.sh key dr-restore` | passphrase | yes only if live v2 |
| Recover from DR over existing | `./run.sh key dr-restore` (auto-detected) | `RESTORE-AND-OVERWRITE`, YK tap, passphrase | yes (`dr-restore-overwrite`) |
| Recover lost-YubiKey only | `./run.sh key dr-restore --no-reinit` | passphrase | no (no overwrite) |
| Migrate v1 → v2 | `./run.sh key migrate` | — | yes (`migrate-v1-to-v2`) |
| Wipe everything (reversible) | `./run.sh key destroy` | `DESTROY`, YK tap | yes (`destroy`) |
| Wipe with no recovery | `./run.sh key destroy --purge-everything` | `DESTROY`, `PURGE-EVERYTHING`, YK tap | NO (intentional) |
| List snapshots | `./run.sh key backups list` | — | — |
| Delete a snapshot | `./run.sh key backups prune <id>` | `DELETE` | — |
| Restore kc_half | `./run.sh key backups restore-kc-half <account>` | `RESTORE`, Touch ID | — |

---

## See also

* [`SECURITY.md`](./SECURITY.md) — security posture, threat model, audit.
* [`CHAR_REVIEW.md`](./CHAR_REVIEW.md) — Char binary review + outbound firewall.
* [`ARCHITECTURE.md`](./ARCHITECTURE.md) — Mermaid diagrams for the
  whole system including the master-key state machine (§4, §15).
* [`README.md` § "Master key management"](./README.md#master-key-management) — operator-facing summary.
