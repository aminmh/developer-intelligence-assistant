import Darwin
import Foundation
import Security

func fail(_ status: OSStatus) -> Never {
    let kind = status == errSecInteractionNotAllowed || status == errSecAuthFailed
        ? "keychain-locked" : "keychain-error"
    fputs("\(kind):\(status)\n", stderr)
    exit(1)
}

func writeJSON(_ value: Any) -> Never {
    do {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
        exit(0)
    } catch {
        fail(errSecParam)
    }
}

let input = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONSerialization.jsonObject(with: input) as? [String: Any],
      let action = request["action"] as? String,
      let account = request["account"] as? String,
      let service = request["service"] as? String else {
    fail(errSecParam)
}

let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrAccount as String: account,
    kSecAttrService as String: service,
]

switch action {
case "store":
    guard let value = request["value"],
          let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else {
        fail(errSecParam)
    }
    let attributes: [String: Any] = [kSecValueData as String: data]
    var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
    if status == errSecItemNotFound {
        var item = query
        item[kSecValueData as String] = data
        status = SecItemAdd(item as CFDictionary, nil)
        if status == errSecDuplicateItem {
            status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        }
    }
    guard status == errSecSuccess else { fail(status) }
    writeJSON(["saved": true])

case "read":
    var lookup = query
    lookup[kSecReturnData as String] = true
    lookup[kSecMatchLimit as String] = kSecMatchLimitOne
    var item: CFTypeRef?
    let status = SecItemCopyMatching(lookup as CFDictionary, &item)
    if status == errSecItemNotFound { writeJSON(["found": false]) }
    guard status == errSecSuccess, let data = item as? Data,
          let value = try? JSONSerialization.jsonObject(with: data) else {
        fail(status == errSecSuccess ? errSecDecode : status)
    }
    writeJSON(["found": true, "value": value])

case "delete":
    let status = SecItemDelete(query as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else { fail(status) }
    writeJSON(["deleted": true])

default:
    fail(errSecParam)
}
