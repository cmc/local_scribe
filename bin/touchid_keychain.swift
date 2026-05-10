// touchid_keychain.swift — small CLI bridge to macOS Keychain with Touch ID gating.
//
// Compiled at bootstrap time by ``./run.sh`` (which invokes ``swiftc -O``).
// ``secret_store.py`` shells out to the resulting binary so the rest of the
// stack stays pure-Python (no PyObjC dependency, ~0 install size beyond what
// Xcode CLT already provides).
//
// Commands (one positional arg, all data on stdin/stdout, never argv):
//
//   touchid-keychain store
//       Reads a hex-encoded key from stdin, writes it to the Keychain item
//       (service=local_scribe, account=master_key) with an access control
//       requiring user presence (Touch ID + passcode fallback). Replaces any
//       existing item.
//
//   touchid-keychain load [prompt]
//       Reads the item from the Keychain. macOS shows the Touch ID prompt
//       with ``prompt`` as the explanation text ("Unlock local_scribe
//       vault" by default). Prints the hex-encoded key on stdout.
//
//   touchid-keychain exists
//       Exits 0 if the item exists (without prompting), 2 if not. Uses
//       ``kSecUseAuthenticationUISkip`` so a missing biometric session
//       won't pop UI.
//
//   touchid-keychain delete
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
let ACCOUNT = "master_key"

// errSecUserCanceled is defined as -128 in macOS SDKs; pull from the
// Security framework so we don't hardcode an integer.
let kCanceledStatuses: Set<OSStatus> = [
    errSecUserCanceled,
    errSecAuthFailed,
]

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

func deleteItem() {
    // Pure delete; ignore "not found".
    let q: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: SERVICE,
        kSecAttrAccount as String: ACCOUNT,
    ]
    _ = SecItemDelete(q as CFDictionary)
}

func storeKey(_ key: Data) {
    var err: Unmanaged<CFError>?
    // .userPresence = Touch ID *or* device passcode. We deliberately accept
    // the passcode fallback so users without enrolled fingerprints (or with
    // a finger sensor that briefly fails) can still unlock with their login
    // password. The vault is already gated by the Mac login session.
    guard let access = SecAccessControlCreateWithFlags(
        nil,
        kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        .userPresence,
        &err
    ) else {
        die("SecAccessControlCreateWithFlags: \(err?.takeRetainedValue().localizedDescription ?? "?")")
    }

    // Replace-on-add: simplest correct semantics for "store the latest key".
    deleteItem()

    let attrs: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: SERVICE,
        kSecAttrAccount as String: ACCOUNT,
        kSecValueData as String: key,
        kSecAttrAccessControl as String: access,
        // We *want* the auth UI on read (later); the add itself happens
        // unattended right after vault creation, so suppress UI here so
        // bootstrap doesn't pop an extra Touch ID prompt for the *add*.
        kSecUseAuthenticationUI as String: kSecUseAuthenticationUISkip,
    ]
    let status = SecItemAdd(attrs as CFDictionary, nil)
    if status != errSecSuccess {
        die("SecItemAdd failed: OSStatus=\(status)", code: 4)
    }
}

func loadKey(prompt: String) -> Data {
    // Modern replacement for the deprecated kSecUseOperationPrompt: build
    // an LAContext with our user-facing reason string and pass it via
    // kSecUseAuthenticationContext. The result is identical (Touch ID
    // sheet with custom copy) but works on macOS 11+ without warnings.
    let ctx = LAContext()
    ctx.localizedReason = prompt

    let q: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: SERVICE,
        kSecAttrAccount as String: ACCOUNT,
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitOne,
        kSecUseAuthenticationContext as String: ctx,
    ]
    var item: CFTypeRef?
    let status = SecItemCopyMatching(q as CFDictionary, &item)
    if status == errSecItemNotFound {
        die("key not stored (run \"./run.sh vault init\")", code: 2)
    }
    if kCanceledStatuses.contains(status) {
        die("Touch ID cancelled / auth failed", code: 3)
    }
    if status != errSecSuccess {
        die("SecItemCopyMatching: OSStatus=\(status)", code: 4)
    }
    guard let data = item as? Data else {
        die("Keychain returned non-Data result", code: 4)
    }
    return data
}

func itemExists() -> Bool {
    // kSecUseAuthenticationUISkip => return errSecInteractionNotAllowed
    // ( -25308 ) when the item exists but reading it would require user
    // interaction. That's exactly the "yes, it's there, but I'm not going
    // to ask the user right now" signal we want for an existence check.
    let q: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: SERVICE,
        kSecAttrAccount as String: ACCOUNT,
        kSecReturnData as String: false,
        kSecUseAuthenticationUI as String: kSecUseAuthenticationUISkip,
    ]
    let status = SecItemCopyMatching(q as CFDictionary, nil)
    return status == errSecSuccess || status == errSecInteractionNotAllowed
}

// MARK: - dispatch

let args = Array(CommandLine.arguments.dropFirst())
guard let cmd = args.first else {
    die("usage: touchid-keychain <store|load|exists|delete> [prompt]")
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
    storeKey(bytes)
    exit(0)

case "load":
    let prompt = args.count >= 2
        ? args[1...].joined(separator: " ")
        : "Unlock local_scribe vault"
    let data = loadKey(prompt: prompt)
    // stdout — append a newline so shell wrappers can `$(...)` cleanly.
    print(data.hexString)
    exit(0)

case "exists":
    exit(itemExists() ? 0 : 2)

case "delete":
    deleteItem()
    exit(0)

default:
    die("unknown command: \(cmd) (valid: store|load|exists|delete)")
}
