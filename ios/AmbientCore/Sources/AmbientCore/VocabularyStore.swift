import Foundation

// MARK: - ASR vocabulary (spec §3.2.7 / §7): names & acronyms for boosting, synced from
// the server on connect; clarification answers flow back through the server list.
// Locally-added terms survive sync (source = local).

public actor VocabularyStore {
    private let db: SQLDatabase

    public init(path: String) throws {
        db = try SQLDatabase(path: path)
        try db.exec("""
        CREATE TABLE IF NOT EXISTS vocabulary(
            term       TEXT PRIMARY KEY,
            kind       TEXT NOT NULL DEFAULT 'name',
            definition TEXT,
            source     TEXT NOT NULL DEFAULT 'server',
            updated_at REAL NOT NULL
        );
        """)
    }

    /// Server snapshot replaces everything previously synced (idempotent full sync).
    public func syncFromServer(_ entries: [VocabularyEntry], now: Date = Date()) throws {
        try db.transaction {
            try db.run("DELETE FROM vocabulary WHERE source='server'")
            for e in entries {
                try db.run("INSERT OR REPLACE INTO vocabulary(term,kind,definition,source,"
                         + "updated_at) VALUES(?,?,?,?,?)",
                           [.text(e.term.lowercased()), .text(e.kind),
                            e.definition.map { .text($0) } ?? .null,
                            .text("server"), .double(now.timeIntervalSince1970)])
            }
        }
    }

    public func addLocal(term: String, kind: String = "name") throws {
        try db.run("INSERT OR REPLACE INTO vocabulary(term,kind,definition,source,updated_at)"
                 + " VALUES(?,?,NULL,'local',?)",
                   [.text(term.lowercased()), .text(kind),
                    .double(Date().timeIntervalSince1970)])
    }

    public func remove(term: String) throws {
        try db.run("DELETE FROM vocabulary WHERE term=?", [.text(term.lowercased())])
    }

    public func all() throws -> [(term: String, kind: String, source: String)] {
        try db.query("SELECT * FROM vocabulary ORDER BY term").map { row in
            (row.text("term") ?? "", row.text("kind") ?? "name", row.text("source") ?? "server")
        }
    }

    /// Boost strings for the ASR models — deduped, casefolded, local terms first
    /// (owner-enrolled names outrank server jargon when the scorer has a budget).
    public func boostTerms() throws -> [String] {
        let rows = try db.query(
            "SELECT term, CASE source WHEN 'local' THEN 0 ELSE 1 END AS prio "
          + "FROM vocabulary ORDER BY prio, term")
        var seen = Set<String>()
        return rows.compactMap { row in
            guard let t = row.text("term")?.lowercased(), !seen.contains(t) else { return nil }
            seen.insert(t)
            return t
        }
    }
}

// MARK: - Card history & feedback log (spec §3.5: UI logs feedback; §5.4 weight fitting
// happens server-side from these + server-side logs). Local copy drives UI state
// (already-dismissed badges, haptics throttle) and the Debug screen.

public struct CardRecord: Sendable, Equatable, Identifiable {
    public var id: Int64
    public var cardId: String
    public var memoryId: String?
    public var kind: FeedbackKind?   // nil = shown
    public var score: Double
    public var headline: String
    public var at: Date
}

public actor CardHistory {
    private let db: SQLDatabase

    public init(path: String) throws {
        db = try SQLDatabase(path: path)
        try db.exec("""
        CREATE TABLE IF NOT EXISTS card_history(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id   TEXT NOT NULL,
            memory_id TEXT,
            kind      TEXT,
            score     REAL NOT NULL DEFAULT 0,
            headline  TEXT NOT NULL DEFAULT '',
            at        REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS card_history_mem ON card_history(memory_id);
        """)
    }

    public func recordShown(card: Card, now: Date = Date()) throws {
        try db.run("INSERT INTO card_history(card_id,memory_id,kind,score,headline,at)"
                 + " VALUES(?,?,NULL,?,?,?)",
                   [.text(card.id), card.memoryId.map { .text($0) } ?? .null,
                    .double(card.score), .text(card.headline),
                    .double(now.timeIntervalSince1970)])
    }

    public func recordFeedback(card: Card, kind: FeedbackKind, now: Date = Date()) throws {
        try db.run("INSERT INTO card_history(card_id,memory_id,kind,score,headline,at)"
                 + " VALUES(?,?,?,?,?,?)",
                   [.text(card.id), card.memoryId.map { .text($0) } ?? .null,
                    .text(kind.rawValue), .double(card.score), .text(card.headline),
                    .double(now.timeIntervalSince1970)])
    }

    /// Times this memory was dismissed (matches server's dismissed_before feature).
    public func dismissCount(memoryId: String) throws -> Int {
        Int(try db.query(
            "SELECT COUNT(*) AS c FROM card_history WHERE memory_id=? AND kind='dismiss'",
            [.text(memoryId)]).first?.int("c") ?? 0)
    }

    public func recent(limit: Int = 50) throws -> [CardRecord] {
        let sql = "SELECT * FROM card_history ORDER BY at DESC LIMIT ?"
        return try db.query(sql, [.int(Int64(limit))]).map { row in
            CardRecord(id: row.int("id") ?? 0, cardId: row.text("card_id") ?? "",
                       memoryId: row.text("memory_id"),
                       kind: row.text("kind").flatMap(FeedbackKind.init(rawValue:)),
                       score: row.double("score") ?? 0,
                       headline: row.text("headline") ?? "",
                       at: Date(timeIntervalSince1970: row.double("at") ?? 0))
        }
    }
}
