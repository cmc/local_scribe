// touchid_keychain.swift — small CLI bridge to macOS Keychain with Touch ID gating.
//
// Compiled at bootstrap time by ``./run.sh`` (which invokes ``swiftc -O``).
// ``secret_store.py`` shells out to the resulting binary so the rest of the
// stack stays pure-Python (no PyObjC dependency, ~0 install size beyond what
// Xcode CLT already provides).
//
// Biometric gating: application-level, not Keychain-level
// -------------------------------------------------------
//
// On macOS 15 (Sequoia) and later, the Keychain-level ``SecAccessControl``
// flags that *automatically* prompt for biometric on item access
// (``.userPresence``, ``.biometryCurrentSet``, ``.biometryAny``) require a
// codesigned binary holding a ``keychain-access-groups`` entitlement bound
// to an Apple Developer Team ID. ``swiftc -O`` + ad-hoc ``codesign -s -``
// CANNOT obtain that entitlement: ``SecItemAdd`` then fails with
// ``errSecMissingEntitlement`` (-34018), and on the older path with the
// suppress-UI flag it failed with ``errSecParam`` (-50). This broke
// bootstrap on 2026-05-11; see SECURITY.md § Threat model.
//
// We work around the tightening by moving the biometric check OUT of the
// Keychain ACL and INTO this binary:
//
//   * ``store``  → ``SecItemAdd`` with ``kSecAttrAccessible =
//                  WhenUnlockedThisDeviceOnly`` (NO ACL). The item is
//                  readable whenever the Mac is unlocked.
//   * ``load``   → ``LAContext.evaluatePolicy(.deviceOwnerAuthentication)``
//                  prompts for Touch ID, with passcode fallback. On
//                  success, ``SecItemCopyMatching`` reads the item.
//   * ``exists`` → unchanged; no auth needed.
//   * ``delete`` → unchanged; no auth needed.
//
// Security delta vs. the old ACL-gated design
// -------------------------------------------
//
//   * Biometric UX is identical: the operator still taps Touch ID once
//     per unlock with our custom prompt string.
//   * An attacker who can already execute code inside the calling Python
//     process can bypass the LAContext gate and read the kc_half via a
//     direct ``SecItemCopyMatching``. The old design's ACL would have
//     blocked that. We accept this: our threat model targets "passer-by
//     at an unlocked screen", not "code execution in our process". A
//     local code-exec adversary is game-over for the vault regardless,
//     because the master key has to live unencrypted in memory while
//     the vault is mounted.
//   * iCloud-sync and locked-screen exposure are unchanged: the
//     ``WhenUnlockedThisDeviceOnly`` accessible class still ensures the
//     item never syncs and is unreadable when the Mac is locked.
//
// If/when local_scribe ships a notarized .app bundle, restoring the
// ACL-gated path is a 1-commit revert plus the entitlement plist.
//
// Usage:
//
//   touchid-keychain [--account NAME] <store|load|exists|delete> [prompt]
//
// The optional ``--account NAME`` flag (which MUST precede the subcommand)
// selects which Keychain item to operate on. We use multiple accounts under
// the same ``service=local_scribe`` namespace for the split-key Option C
// architecture: ``master_key`` is the legacy whole-key item (kept for
// backward compatibility / migration), and ``master_key_kc_half_v2`` holds
// one half of an XOR-split key; the other half lives in a YubiKey-encrypted
// age file on disk. Defaults to ``master_key`` when --account is omitted
// for backward compatibility with the pre-Option-C wire format.
//
// Commands (all data on stdin/stdout, never argv):
//
//   touchid-keychain [--account NAME] store
//       Reads a hex-encoded key from stdin, writes it to the Keychain item
//       (service=local_scribe, account=<NAME>) with an access control
//       requiring user presence (Touch ID + passcode fallback). Replaces any
//       existing item.
//
//   touchid-keychain [--account NAME] load [prompt]
//       Reads the item from the Keychain. macOS shows the Touch ID prompt
//       with ``prompt`` as the explanation text ("Unlock local_scribe
//       vault" by default). Prints the hex-encoded key on stdout.
//
//   touchid-keychain [--account NAME] exists
//       Exits 0 if the item exists (without prompting), 2 if not. Uses
//       ``kSecUseAuthenticationUISkip`` so a missing biometric session
//       won't pop UI.
//
//   touchid-keychain [--account NAME] delete
//       Removes the item if present. Always exits 0.
//
// Error exits (so callers can branch):
//   0  success
//   1  generic / argument error
//   2  errSecItemNotFound (key not stored yet)
//   3  errSecUserCanceled or errSecAuthFailed (user pressed Cancel /
//      failed Touch ID too many times)
//   4  any other OSStatus failure (printed to stderr for diagnosis)
//   5  hex parse failure
//
// All non-zero exits write a one-line error to stderr.

import Foundation
import Security
import LocalAuthentication

let SERVICE = "local_scribe"
let DEFAULT_ACCOUNT = "master_key"

func die(_ msg: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data("error: \(msg)\n".utf8))
    exit(code)
}

// MARK: - hex codec (no Foundation Data(hexString:) on stock SDK)

extension Data {
    var hexString: String {
        return map { String(format: "%02x", $0) }.joined()
    }
    static func fromHex(_ s: String) -> Data? {
        let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
        let len = trimmed.count
        if len == 0 || len % 2 != 0 { return nil }
        var out = Data(capacity: len / 2)
        var idx = trimmed.startIndex
        for _ in 0..<(len / 2) {
            let next = trimmed.index(idx, offsetBy: 2)
            guard let byte = UInt8(trimmed[idx..<next], radix: 16) else { return nil }
            out.append(byte)
            idx = next
        }
        return out
    }
}

// MARK: - Keychain ops

func deleteItem(account: String) {
    // Pure delete; ignore "not found".
    let q: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: SERVICE,
        kSecAttrAccount as String: account,
    ]
    _ = SecItemDelete(q as CFDictionary)
}

func storeKey(_ key: Data, account: String) {
    // Replace-on-add: simplest correct semantics for "store the latest key".
    deleteItem(account: account)

    // Biometric gating is enforced at LOAD time via LAContext; the keychain
    // item itself just has the "device unlocked, never sync" accessibility
    // class. See the file-header docstring for the threat-model trade-off
    // and why we no longer attach a SecAccessControl ACL here.
    let attrs: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: SERVICE,
        kSecAttrAccount as String: account,
        kSecValueData as String: key,
        kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    ]
    let status = SecItemAdd(attrs as CFDictionary, nil)
    if status != errSecSuccess {
        die("SecItemAdd failed: OSStatus=\(status)", code: 4)
    }
}

func loadKey(account: String, prompt: String) -> Data {
    // Step 1: prove the live operator is here. We use
    // .deviceOwnerAuthentication (Touch ID with passcode fallback) so a
    // user without enrolled fingerprints — or with a sensor that briefly
    // fails — can still unlock with their login password. The choice
    // mirrors the .userPresence ACL flag we used to attach to the
    // keychain item; see file-header docstring for the macOS 15 reason
    // we had to move this check out of the SecAccessControl path.
    //
    // evaluatePolicy is synchronous from the CLI's perspective (we wait
    // on the semaphore) so the parent process — secret_store.py via
    // subprocess.run() — sees a blocking call exactly like the old ACL
    // path. The prompt sheet appears in front of every window.
    let ctx = LAContext()
    var laErr: NSError?
    if !ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &laErr) {
        let detail = laErr?.localizedDescription ?? "no biometry / passcode configured"
        die("local auth unavailable: \(detail)", code: 4)
    }

    let sem = DispatchSemaphore(value: 0)
    var authOK = false
    var authErr: Error?
    ctx.evaluatePolicy(
        .deviceOwnerAuthentication,
        localizedReason: prompt
    ) { ok, err in
        authOK = ok
        authErr = err
        sem.signal()
    }
    sem.wait()

    if !authOK {
        // LAError codes: userCancel/.appCancel/.systemCancel/.userFallback
        // all map to our "user dismissed" code (3). Anything else
        // (authenticationFailed after too many retries, biometryLockout,
        // etc.) also gets exit 3 because from the caller's standpoint
        // the user simply can't unlock right now.
        if let err = authErr as NSError? {
            die("Touch ID cancelled / auth failed (LAError \(err.code))", code: 3)
        }
        die("Touch ID cancelled / auth failed", code: 3)
    }

    // Step 2: read the keychain item. Item has no ACL (only an
    // accessibility class) so this is a plain query — no extra prompt.
    let q: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: SERVICE,
        kSecAttrAccount as String: account,
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var item: CFTypeRef?
    let status = SecItemCopyMatching(q as CFDictionary, &item)
    if status == errSecItemNotFound {
        die("key not stored (run \"./run.sh key init\")", code: 2)
    }
    if status != errSecSuccess {
        die("SecItemCopyMatching: OSStatus=\(status)", code: 4)
    }
    guard let data = item as? Data else {
        die("Keychain returned non-Data result", code: 4)
    }
    return data
}

func itemExists(account: String) -> Bool {
    // kSecUseAuthenticationUISkip => return errSecInteractionNotAllowed
    // ( -25308 ) when the item exists but reading it would require user
    // interaction. That's exactly the "yes, it's there, but I'm not going
    // to ask the user right now" signal we want for an existence check.
    let q: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: SERVICE,
        kSecAttrAccount as String: account,
        kSecReturnData as String: false,
        kSecUseAuthenticationUI as String: kSecUseAuthenticationUISkip,
    ]
    let status = SecItemCopyMatching(q as CFDictionary, nil)
    return status == errSecSuccess || status == errSecInteractionNotAllowed
}

// MARK: - dispatch

// Parse a leading ``--account NAME`` flag, then the subcommand. We keep the
// CLI surface tiny and positional after the optional flag so the Swift code
// stays auditable; ``secret_store.py`` is the only intended caller.
var rest = Array(CommandLine.arguments.dropFirst())
var account = DEFAULT_ACCOUNT
if rest.count >= 2, rest[0] == "--account" {
    let candidate = rest[1]
    // Defensive whitelist: account names must look like identifiers so we
    // don't accidentally accept attacker-controlled metadata if someone
    // wires ``--account`` to user input. ``master_key`` and
    // ``master_key_kc_half_v2`` (the only callers in tree) pass; arbitrary
    // strings with spaces / shell metacharacters don't.
    let allowed: CharacterSet = {
        var s = CharacterSet.alphanumerics
        s.insert(charactersIn: "_-")
        return s
    }()
    if candidate.isEmpty || candidate.rangeOfCharacter(from: allowed.inverted) != nil {
        die("invalid --account value")
    }
    account = candidate
    rest = Array(rest.dropFirst(2))
}

guard let cmd = rest.first else {
    die("usage: touchid-keychain [--account NAME] <store|load|exists|delete> [prompt]")
}

switch cmd {
case "store":
    // Hex on stdin to keep the raw key out of argv / process listings.
    guard let line = readLine(strippingNewline: true) else {
        die("no input on stdin")
    }
    guard let bytes = Data.fromHex(line), !bytes.isEmpty else {
        die("invalid hex on stdin", code: 5)
    }
    storeKey(bytes, account: account)
    exit(0)

case "load":
    let prompt = rest.count >= 2
        ? rest[1...].joined(separator: " ")
        : "Unlock local_scribe vault"
    let data = loadKey(account: account, prompt: prompt)
    // stdout — append a newline so shell wrappers can `$(...)` cleanly.
    print(data.hexString)
    exit(0)

case "exists":
    exit(itemExists(account: account) ? 0 : 2)

case "delete":
    deleteItem(account: account)
    exit(0)

default:
    die("unknown command: \(cmd) (valid: store|load|exists|delete)")
}
