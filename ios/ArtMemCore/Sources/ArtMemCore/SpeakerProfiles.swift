import Foundation

// MARK: - Speaker profiles (spec §3.2.5/6): per-speaker mean embedding, cosine match vs
// configurable threshold, provisional `Speaker-N` profiles that accumulate embeddings,
// owner enrollment. Persistence in the local DB so the same stranger is recognized
// across days.

public func cosineSimilarity(_ a: [Float], _ b: [Float]) -> Double {
    guard a.count == b.count, !a.isEmpty else { return 0 }
    var dot: Double = 0, na: Double = 0, nb: Double = 0
    for i in 0..<a.count {
        let x = Double(a[i]), y = Double(b[i])
        dot += x * y; na += x * x; nb += y * y
    }
    guard na > 0, nb > 0 else { return 0 }
    return dot / (na.squareRoot() * nb.squareRoot())
}

public struct SpeakerProfile: Sendable, Equatable, Identifiable {
    public var id: String
    public var label: String        // "Speaker-3" | "Jasmine" | "Owner"
    public var name: String?        // set when the owner renames (spec §7)
    public var isOwner: Bool
    public var embedding: [Float]   // running mean
    public var sampleCount: Int
    public var displayName: String { name ?? label }
}

public actor SpeakerProfiles {
    private let db: SQLDatabase
    public var matchThreshold: Double   // cosine; configurable (spec §3.2.5)

    public init(path: String, matchThreshold: Double = 0.62) throws {
        db = try SQLDatabase(path: path)
        self.matchThreshold = matchThreshold
        try db.exec("""
        CREATE TABLE IF NOT EXISTS speaker_profiles(
            id          TEXT PRIMARY KEY,
            label       TEXT NOT NULL,
            name        TEXT,
            is_owner    INTEGER NOT NULL DEFAULT 0,
            embedding   BLOB NOT NULL,
            samples     INTEGER NOT NULL DEFAULT 0,
            last_seen   REAL NOT NULL
        );
        """)
    }

    /// Closest profile above threshold, else nil → caller should create provisional.
    public func setMatchThreshold(_ value: Double) { matchThreshold = value }

    public func match(_ embedding: [Float], now: Date = Date()) throws -> SpeakerProfile? {
        var best: (profile: SpeakerProfile, score: Double)?
        for row in try profiles() {
            let score = cosineSimilarity(embedding, row.embedding)
            if score >= matchThreshold, score > (best?.score ?? -1) { best = (row, score) }
        }
        if let hit = best {
            // absorb the new sample into the running mean (adaptive profiles)
            try absorb(into: hit.profile.id, sample: embedding, weight: 1, now: now)
            return hit.profile
        }
        return nil
    }

    @discardableResult
    public func createProvisional(embedding: [Float], now: Date = Date()) throws
        -> SpeakerProfile {
        let used = try db.query("SELECT label FROM speaker_profiles WHERE label LIKE 'Speaker-%'")
            .compactMap { row -> Int? in
                guard let l = row.text("label"), l.hasPrefix("Speaker-") else { return nil }
                return Int(l.dropFirst("Speaker-".count))
            }
        let n = (used.max() ?? 0) + 1
        return try insert(id: UUID().uuidString, label: "Speaker-\(n)", name: nil,
                          isOwner: false, embedding: embedding, now: now)
    }

    public func enrollOwner(embedding: [Float], now: Date = Date()) throws -> SpeakerProfile {
        if let existing = try db.query("SELECT id FROM speaker_profiles WHERE is_owner=1")
            .first, let id = existing.text("id") {
            try absorb(into: id, sample: embedding, weight: 6, now: now)   // enrollment counts heavy
            return try require(id)
        }
        return try insert(id: UUID().uuidString, label: "Owner", name: "Owner",
                          isOwner: true, embedding: embedding, now: now)
    }

    public func owner() throws -> SpeakerProfile? {
        guard let row = try db.query("SELECT id FROM speaker_profiles WHERE is_owner=1")
            .first else { return nil }
        return try require(row.text("id") ?? "")
    }

    public func setOwnerFlag(id: String, owner: Bool) throws {
        try db.run("UPDATE speaker_profiles SET is_owner=? WHERE id=?",
                   [.int(owner ? 1 : 0), .text(id)])
    }

    public func rename(id: String, to name: String) throws {
        try db.run("UPDATE speaker_profiles SET name=? WHERE id=?", [.text(name), .text(id)])
    }

    /// Merge a provisional profile into a named one (spec §7: "merge with an existing
    /// profile if chosen"). Weighted by accumulated samples; source row disappears.
    public func merge(from: String, into: String) throws {
        guard let src = try? require(from), let dst = try? require(into) else { return }
        let total = max(1, src.sampleCount + dst.sampleCount)
        let merged = zip(dst.embedding, src.embedding).map {
            (Float($0.0) * Float(dst.sampleCount) + Float($0.1) * Float(src.sampleCount))
                / Float(total)
        }
        try db.transaction {
            try db.run("UPDATE speaker_profiles SET embedding=?, samples=?, last_seen=? WHERE id=?",
                       [.blob(Self.data(merged)), .int(Int64(total)),
                        .double(Date().timeIntervalSince1970), .text(into)])
            try db.run("DELETE FROM speaker_profiles WHERE id=?", [.text(from)])
        }
    }

    public func delete(id: String) throws {
        try db.run("DELETE FROM speaker_profiles WHERE id=?", [.text(id)])
    }

    public func all() throws -> [SpeakerProfile] { try profiles() }

    // MARK: internals

    private func insert(id: String, label: String, name: String?, isOwner: Bool,
                        embedding: [Float], now: Date) throws -> SpeakerProfile {
        try db.run("INSERT INTO speaker_profiles(id,label,name,is_owner,embedding,samples,"
                 + "last_seen) VALUES(?,?,?,?,?,?,?)",
                   [.text(id), .text(label), name.map { .text($0) } ?? .null,
                    .int(isOwner ? 1 : 0), .blob(Self.data(embedding)), .int(1),
                    .double(now.timeIntervalSince1970)])
        return SpeakerProfile(id: id, label: label, name: name, isOwner: isOwner,
                              embedding: embedding, sampleCount: 1)
    }

    /// running mean: emb = (emb·samples + sample·weight) / (samples + weight)
    private func absorb(into id: String, sample: [Float], weight: Int, now: Date) throws {
        let p = try require(id)
        let total = max(1, p.sampleCount + weight)
        let merged = zip(p.embedding, sample).map {
            ($0.0 * Float(p.sampleCount) + $0.1 * Float(weight)) / Float(total)
        }
        try db.run("UPDATE speaker_profiles SET embedding=?, samples=?, last_seen=? WHERE id=?",
                   [.blob(Self.data(merged)), .int(Int64(total)),
                    .double(now.timeIntervalSince1970), .text(id)])
    }

    private func require(_ id: String) throws -> SpeakerProfile {
        guard let row = try db.query("SELECT * FROM speaker_profiles WHERE id=?",
                                     [.text(id)]).first,
              let pid = row.text("id") else {
            throw SQLiteError(message: "no profile \(id)")
        }
        return SpeakerProfile(
            id: pid, label: row.text("label") ?? "", name: row.text("name"),
            isOwner: (row.int("is_owner") ?? 0) != 0,
            embedding: Self.floats(row.blob("embedding") ?? Data()),
            sampleCount: Int(row.int("samples") ?? 0))
    }

    private func profiles() throws -> [SpeakerProfile] {
        try db.query("SELECT * FROM speaker_profiles ORDER BY last_seen DESC").map { row in
            SpeakerProfile(
                id: row.text("id") ?? "", label: row.text("label") ?? "",
                name: row.text("name"), isOwner: (row.int("is_owner") ?? 0) != 0,
                embedding: Self.floats(row.blob("embedding") ?? Data()),
                sampleCount: Int(row.int("samples") ?? 0))
        }
    }

    static func data(_ floats: [Float]) -> Data {
        floats.withUnsafeBytes { Data($0) }
    }

    static func floats(_ data: Data) -> [Float] {
        data.withUnsafeBytes { Array($0.bindMemory(to: Float32.self)) }
    }
}
