import Foundation
import SQLite3

// Minimal dependency-free SQLite binding (the iOS store has no GRDB — see DECISIONS.md).
// Apple's system libsqlite3 includes FTS5; that is what phone-side search runs on.

private let kSQLITE_ROWK = 100  // SQLITE_ROW, spelled for one call-site clarity below

enum SQLValue {
    case null
    case text(String)
    case int(Int64)
    case double(Double)
    case blob(Data)
}

struct SQLRow {
    /// Values COPIED at step-time: rows outlive the prepared statement that made them.
    fileprivate let values: [String: SQLValue]

    fileprivate init(stmt: OpaquePointer?) {
        var v: [String: SQLValue] = [:]
        let n = Int(sqlite3_column_count(stmt))
        for i in 0..<n {
            let name = sqlite3_column_name(stmt, Int32(i)).map { String(cString: $0) } ?? "\(i)"
            let value: SQLValue
            switch sqlite3_column_type(stmt, Int32(i)) {
            case SQLITE_NULL: value = .null
            case SQLITE_INTEGER: value = .int(sqlite3_column_int64(stmt, Int32(i)))
            case SQLITE_FLOAT: value = .double(sqlite3_column_double(stmt, Int32(i)))
            case SQLITE_BLOB:
                if let ptr = sqlite3_column_blob(stmt, Int32(i)) {
                    value = .blob(Data(bytes: ptr, count: Int(sqlite3_column_bytes(stmt, Int32(i)))))
                } else { value = .null }
            default:
                value = .text(sqlite3_column_text(stmt, Int32(i)).map { String(cString: $0) } ?? "")
            }
            v[name] = value
        }
        values = v
    }

    func column(_ name: String) -> SQLValue { values[name] ?? .null }

    func string(_ name: String) -> String? {
        switch column(name) {
        case .text(let s): return s
        case .int(let i): return String(i)
        case .double(let d): return String(d)
        default: return nil
        }
    }

    /// Alias used by call-sites that mean "TEXT column by name".
    func text(_ name: String) -> String? { string(name) }

    func int(_ name: String) -> Int? { int64(name).map { Int($0) } }

    func int64(_ name: String) -> Int64? {
        if case .int(let v) = column(name) { return v }
        return nil
    }

    func double(_ name: String) -> Double? {
        switch column(name) {
        case .double(let d): return d
        case .int(let i): return Double(i)
        case .text(let s): return Double(s)
        default: return nil
        }
    }

    func blob(_ name: String) -> Data? {
        if case .blob(let d) = column(name) { return d }
        return nil
    }

    func has(_ name: String) -> Bool { values[name] != nil }
}

final class SQLDatabase: @unchecked Sendable {
    private let handle: OpaquePointer?
    private let lock = NSLock()

    var lastInsertRowId: Int64 {
        lock.lock(); defer { lock.unlock() }
        return sqlite3_last_insert_rowid(handle)
    }

    init(path: String) throws {
        // ensure parent directory exists
        let dir = (path as NSString).deletingLastPathComponent
        if !dir.isEmpty {
            try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        }
        var h: OpaquePointer?
        let rc = sqlite3_open_v2(path, &h,
                                 SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
                                 nil)
        guard rc == SQLITE_OK else {
            let msg = h.map { String(cString: sqlite3_errmsg($0)) } ?? "sqlite rc \(rc)"
            throw SQLiteError(message: msg)
        }
        handle = h
        // exec() is multi-statement aware: DDL batches with semicolons just work
        try exec("PRAGMA journal_mode=WAL;")
        try exec("PRAGMA synchronous=NORMAL;")
        try exec("PRAGMA foreign_keys=ON;")
    }

    deinit { sqlite3_close_v2(handle) }

    /// Runs one or many statements. Binds `params` when the batch is a single
    /// statement (multi-statement DDL batches must not carry params).
    func exec(_ sql: String, _ params: [SQLValue] = []) throws {
        lock.lock(); defer { lock.unlock() }
        var statementCount = 0
        var failure: SQLiteError?
        sql.withCString { cstr in
            var tail: UnsafePointer<CChar>? = cstr
            while let current = tail, current.pointee != 0 {
                var stmt: OpaquePointer?
                var next: UnsafePointer<CChar>?
                let prc = sqlite3_prepare_v2(handle, current, -1, &stmt, &next)
                if prc != SQLITE_OK {
                    failure = SQLiteError(message:
                        "\(String(cString: sqlite3_errmsg(handle))) — [\(Self.trim(current))]")
                    sqlite3_finalize(stmt)
                    return
                }
                statementCount += 1
                if let stmt {
                    if !params.isEmpty, statementCount > 1 {
                        failure = SQLiteError(message: "exec(:) with params must be single-statement")
                        sqlite3_finalize(stmt)
                        return
                    }
                    let brc = Self.bindRaw(stmt, params)
                    if brc != SQLITE_OK {
                        failure = SQLiteError(message: "bind: \(String(cString: sqlite3_errmsg(handle)))")
                        sqlite3_finalize(stmt)
                        return
                    }
                    let src = sqlite3_step(stmt)
                    sqlite3_finalize(stmt)
                    // PRAGMAs answer with a row; ROW counts as success
                    if src != SQLITE_OK && src != SQLITE_DONE && src != kSQLITE_ROWK {
                        failure = SQLiteError(message:
                            "\(String(cString: sqlite3_errmsg(handle))) — [\(Self.trim(current))]")
                        return
                    }
                }
                tail = next
            }
        }
        if let failure { throw failure }
    }

    private static func trim(_ p: UnsafePointer<CChar>) -> String {
        let s = String(cString: p)
        return String(s.prefix(120)).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func run(_ sql: String, _ params: [SQLValue] = []) throws {
        try exec(sql, params)
    }

    /// BEGIN IMMEDIATE / body / COMMIT, with ROLLBACK on any throw.
    func transaction(_ body: () throws -> Void) throws {
        try exec("BEGIN IMMEDIATE")
        do {
            try body()
            try exec("COMMIT")
        } catch {
            try? exec("ROLLBACK")
            throw error
        }
    }

    func query(_ sql: String, _ params: [SQLValue] = []) throws -> [SQLRow] {
        var rows: [SQLRow] = []
        try queryEach(sql, params) { rows.append($0) }
        return rows
    }

    func queryEach(_ sql: String, _ params: [SQLValue] = [],
                   _ body: (SQLRow) throws -> Void) throws {
        lock.lock(); defer { lock.unlock() }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &stmt, nil) == SQLITE_OK else {
            let msg = String(cString: sqlite3_errmsg(handle))
            throw SQLiteError(message: "\(msg) — [\(sql)]")
        }
        defer { sqlite3_finalize(stmt) }
        try bind(stmt, params)
        while true {
            let rc = sqlite3_step(stmt)
            if rc == SQLITE_ROW {
                try body(SQLRow(stmt: stmt))
                continue
            }
            if rc == SQLITE_DONE { break }
            let msg = String(cString: sqlite3_errmsg(handle))
            throw SQLiteError(message: "\(msg) — [\(sql)]")
        }
    }

    private static func bindRaw(_ stmt: OpaquePointer?, _ params: [SQLValue]) -> Int32 {
        for (i, p) in params.enumerated() {
            let idx = Int32(i + 1)
            let rc: Int32
            switch p {
            case .null: rc = sqlite3_bind_null(stmt, idx)
            case .text(let s):
                rc = sqlite3_bind_text(stmt, idx, s, -1,
                                       unsafeBitCast(-1, to: sqlite3_destructor_type.self))
            case .int(let v): rc = sqlite3_bind_int64(stmt, idx, v)
            case .double(let v): rc = sqlite3_bind_double(stmt, idx, v)
            case .blob(let d):
                rc = d.withUnsafeBytes { buf in
                    sqlite3_bind_blob(stmt, idx, buf.baseAddress, Int32(d.count),
                                      unsafeBitCast(-1, to: sqlite3_destructor_type.self))
                }
            }
            if rc != SQLITE_OK { return rc }
        }
        return SQLITE_OK
    }

    private func bind(_ stmt: OpaquePointer?, _ params: [SQLValue]) throws {
        let rc = Self.bindRaw(stmt, params)
        guard rc == SQLITE_OK else {
            throw SQLiteError(message: "bind failed rc=\(rc)")
        }
    }
}

struct SQLiteError: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

// MARK: - Date/ISO/millis helpers shared by the stores

/// NSDateFormatter/ISO8601DateFormatter are not thread-safe → serialize behind one lock.
enum AmbientISO {
    private static let lock = NSLock()
    nonisolated(unsafe) private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    nonisolated(unsafe) private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func date(_ s: String) -> Date? {
        lock.lock(); defer { lock.unlock() }
        return fractional.date(from: s) ?? plain.date(from: s)
    }

    static func string(_ d: Date) -> String {
        lock.lock(); defer { lock.unlock() }
        return fractional.string(from: d)
    }
}

extension String {
    /// ISO-8601 (with or without fraction seconds) → Date; epoch fallback.
    func toDate() -> Date {
        AmbientISO.date(self) ?? Date(timeIntervalSince1970: Double(self) ?? 0)
    }
}

extension Date {
    var ms: SQLValue { .int(Int64((timeIntervalSince1970 * 1000).rounded())) }
    init(ms: Int64) { self.init(timeIntervalSince1970: Double(ms) / 1000) }

    /// Wire format the server accepts (fractional-seconds ISO, UTC)
    public var isoWire: String { AmbientISO.string(self) }
}
