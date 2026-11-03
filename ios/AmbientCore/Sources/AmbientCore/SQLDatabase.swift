import Foundation
import SQLite3

// MARK: - Minimal SQLite wrapper (WAL). Zero external dependencies; GRDB spike failed
// the toolchain (see Package.swift / DECISIONS.md).

enum SQLValue: Sendable {
    case null, int(Int64), double(Double), text(String), blob(Data)
}

struct SQLRow: Sendable {
    private let values: [String: SQLValue]
    init(_ values: [String: SQLValue]) { self.values = values }
    func int(_ key: String) -> Int64? { if case .int(let v)? = values[key] { return v }; return nil }
    func double(_ key: String) -> Double? {
        if case .double(let v)? = values[key] { return v }
        if case .int(let v)? = values[key] { return Double(v) }
        return nil
    }
    func text(_ key: String) -> String? { if case .text(let v)? = values[key] { return v }; return nil }
    func blob(_ key: String) -> Data? { if case .blob(let v)? = values[key] { return v }; return nil }
    func isNull(_ key: String) -> Bool {
        guard let v = values[key] else { return true }
        if case .null = v { return true }
        return false
    }
    var keys: [String] { Array(values.keys) }
}

private func check(_ db: OpaquePointer?, _ rc: Int32, _ sql: String) throws {
    if rc != SQLITE_OK && rc != SQLITE_DONE && rc != SQLITE_ROW {
        let msg = db.map { String(cString: sqlite3_errmsg($0)) } ?? "sqlite rc \(rc)"
        throw SQLiteError(message: "\(msg) — [\(sql)]")
    }
}

public struct SQLiteError: Error, CustomStringConvertible, LocalizedError {
    public let message: String
    public var description: String { message }
    public var errorDescription: String? { message }
}

/// Connection-safe: serialize all use through one actor or queue.
final class SQLDatabase: @unchecked Sendable {
    private var handle: OpaquePointer?
    private let lock = NSLock()

    init(path: String) throws {
        // create the parent directory (databases live under app groups/Caches)
        let parent = (path as NSString).deletingLastPathComponent
        if !parent.isEmpty { try? FileManager.default.createDirectory(
            atPath: parent, withIntermediateDirectories: true) }
        var h: OpaquePointer?
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX
        guard sqlite3_open_v2(path, &h, flags, nil) == SQLITE_OK else {
            let msg = h.map { String(cString: sqlite3_errmsg($0)) } ?? "open failed"
            sqlite3_close(h)
            throw SQLiteError(message: msg)
        }
        handle = h
        try exec("PRAGMA journal_mode=WAL")
        try exec("PRAGMA synchronous=NORMAL")
        try exec("PRAGMA foreign_keys=ON")
        try exec("PRAGMA busy_timeout=5000")
    }

    deinit { sqlite3_close(handle) }

    func exec(_ sql: String, _ params: [SQLValue] = []) throws {
        lock.lock(); defer { lock.unlock() }
        var stmt: OpaquePointer?
        try check(handle, sqlite3_prepare_v2(handle, sql, -1, &stmt, nil), sql)
        defer { sqlite3_finalize(stmt) }
        try Self.bind(stmt, params)
        try check(handle, sqlite3_step(stmt), sql)
    }

    @discardableResult
    func run(_ sql: String, _ params: [SQLValue] = []) throws -> Int64 {
        try exec(sql, params)
        lock.lock(); defer { lock.unlock() }
        return sqlite3_last_insert_rowid(handle)
    }

    func query(_ sql: String, _ params: [SQLValue] = []) throws -> [SQLRow] {
        lock.lock(); defer { lock.unlock() }
        var stmt: OpaquePointer?
        try check(handle, sqlite3_prepare_v2(handle, sql, -1, &stmt, nil), sql)
        defer { sqlite3_finalize(stmt) }
        try Self.bind(stmt, params)
        var rows: [SQLRow] = []
        while true {
            let rc = sqlite3_step(stmt)
            if rc == SQLITE_ROW {
                var values: [String: SQLValue] = [:]
                for i in 0..<sqlite3_column_count(stmt) {
                    let name = String(cString: sqlite3_column_name(stmt, i))
                    switch sqlite3_column_type(stmt, i) {
                    case SQLITE_INTEGER: values[name] = .int(sqlite3_column_int64(stmt, i))
                    case SQLITE_FLOAT: values[name] = .double(sqlite3_column_double(stmt, i))
                    case SQLITE_TEXT:
                        values[name] = .text(String(cString: sqlite3_column_text(stmt, i)))
                    case SQLITE_BLOB:
                        let n = Int(sqlite3_column_bytes(stmt, i))
                        values[name] = .blob(Data(bytes: sqlite3_column_blob(stmt, i), count: n))
                    default: values[name] = .null
                    }
                }
                rows.append(SQLRow(values))
            } else {
                try check(handle, rc, sql)
                break
            }
        }
        return rows
    }

    func transaction(_ body: () throws -> Void) throws {
        try exec("BEGIN IMMEDIATE")
        do { try body() } catch {
            try? exec("ROLLBACK")
            throw error
        }
        try exec("COMMIT")
    }

    private static func bind(_ stmt: OpaquePointer?, _ params: [SQLValue]) throws {
        for (i, v) in params.enumerated() {
            let idx = Int32(i + 1)
            let rc: Int32
            switch v {
            case .null: rc = sqlite3_bind_null(stmt, idx)
            case .int(let x): rc = sqlite3_bind_int64(stmt, idx, x)
            case .double(let x): rc = sqlite3_bind_double(stmt, idx, x)
            case .text(let x): rc = sqlite3_bind_text(stmt, idx, x, -1, VTable.destructor)
            case .blob(let x):
                rc = x.withUnsafeBytes {
                    sqlite3_bind_blob(stmt, idx, $0.baseAddress, Int32(x.count),
                                      VTable.destructor)
                }
            }
            if rc != SQLITE_OK { throw SQLiteError(message: "bind failed rc \(rc)") }
        }
    }

    private enum VTable {
        static let destructor: sqlite3_destructor_type = unsafeBitCast(
            -1, to: sqlite3_destructor_type.self) // SQLITE_TRANSIENT
    }
}
