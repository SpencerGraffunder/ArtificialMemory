import Foundation

// MARK: - Rolling local audio buffer (spec §3.2.8): chunk files on disk, delete anything
// older than the retention window (default 10 min). Files (not one ring file) so the
// offline refinement pass can read a contiguous WAV per conversation easily.

public struct RollingAudioConfig: Sendable, Equatable {
    public var retentionSeconds: Double = 600       // spec default: 10 min
    public var chunkSeconds: Double = 5
    public var sampleRate: Double = 16_000
    public init() {}

    public var bytesPerChunk: Int {
        Int(sampleRate * chunkSeconds * 4)  // 16 kHz mono Float32
    }
}

/// Pure eviction decision, no filesystem: which chunk ids to delete given "now".
public func audioEvictions(chunks: [(id: String, endedAt: Date)], now: Date,
                           retention: TimeInterval) -> [String] {
    chunks.filter { now.timeIntervalSince($0.endedAt) > retention }.map(\.id)
}

public actor RollingAudioBuffer {
    public let directory: URL
    public var config: RollingAudioConfig
    private var manifest: [(id: String, url: URL, endedAt: Date)] = []

    public init(directory: URL, config: RollingAudioConfig = RollingAudioConfig()) throws {
        self.directory = directory
        self.config = config
        try FileManager.default.createDirectory(at: directory,
                                                withIntermediateDirectories: true)
        // data-at-rest per spec §3.2.8 (NSFileProtectionCompleteUntilFirstUserAuthentication)
        try? (directory as NSURL).setResourceValue(
            FileProtectionType.completeUntilFirstUserAuthentication,
            forKey: .fileProtectionKey)
        // rebuild manifest from filenames ("<ms-since-epoch>-chunk.pcm") on relaunch
        let fm = FileManager.default
        if let files = try? fm.contentsOfDirectory(at: directory,
                                                   includingPropertiesForKeys: nil) {
            for url in files where url.pathExtension == "pcm" {
                let ms = Double(url.deletingPathExtension().lastPathComponent) ?? 0
                manifest.append((id: url.lastPathComponent, url: url,
                                 endedAt: Date(timeIntervalSince1970: ms / 1000)))
            }
        }
    }

    /// Append a Float32 frame buffer; starts a new chunk every `chunkSeconds`.
    public func append(_ samples: [Float], at date: Date) throws {
        let id = "\(Int(date.timeIntervalSince1970 * 1000))-chunk.pcm"
        let url = directory.appendingPathComponent(id)
        if manifest.last?.id != id {
            manifest.append((id, url, date))
        }
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: url)
        defer { try? handle.close() }
        try handle.seekToEnd()
        try samples.withUnsafeBytes { try handle.write(contentsOf: $0) }
        // end time refresh so eviction measures the chunk's LAST sample
        if let i = manifest.lastIndex(where: { $0.id == id }) {
            manifest[i].endedAt = date
        }
        _ = try sweep(now: date)
    }

    /// Delete chunks older than retention. Returns deleted ids (caller may alert).
    @discardableResult
    public func sweep(now: Date) throws -> [String] {
        let pairs = manifest.map { ($0.id, $0.endedAt) }
        let doomed = audioEvictions(chunks: pairs.map { (id: $0.0, endedAt: $0.1) },
                                    now: now, retention: config.retentionSeconds)
        for id in doomed {
            if let i = manifest.firstIndex(where: { $0.id == id }) {
                try? FileManager.default.removeItem(at: manifest[i].url)
                manifest.remove(at: i)
            }
        }
        return doomed
    }

    /// Contiguous WAV for [from, to] for the offline refinement pass (spec §3.2.4).
    public func wav(from: Date, to: Date) throws -> Data {
        var pcm = Data()
        for entry in manifest where entry.endedAt >= from && entry.endedAt <= to {
            if let data = FileManager.default.contents(atPath: entry.url.path) { pcm.append(data) }
        }
        return Self.wav(fromFloat32LE: pcm, sampleRate: Int(config.sampleRate))
    }

    public func chunkCount() -> Int { manifest.count }

    static func wav(fromFloat32LE pcm: Data, sampleRate: Int) -> Data {
        // convert float32 → int16 mono PCM + 44-byte RIFF header
        var pcmData = Data(capacity: pcm.count / 2)
        pcm.withUnsafeBytes { raw in
            for f in raw.bindMemory(to: Float32.self) {
                let s = max(-1, min(1, f))
                pcmData.append(contentsOf: withUnsafeBytes(of: Int16(s * 32767)) {
                    Array($0)
                })
            }
        }
        var header = Data()
        func ascii(_ s: String) { header.append(contentsOf: s.utf8) }
        func u32(_ v: UInt32) { header.append(contentsOf: withUnsafeBytes(of: v.littleEndian) { Array($0) }) }
        func u16(_ v: UInt16) { header.append(contentsOf: withUnsafeBytes(of: v.littleEndian) { Array($0) }) }
        ascii("RIFF"); u32(UInt32(36 + pcmData.count)); ascii("WAVEfmt ")
        u32(16); u16(1); u16(1)
        u32(UInt32(sampleRate)); u32(UInt32(sampleRate * 2))
        u16(2); u16(16)
        ascii("data"); u32(UInt32(pcmData.count))
        return header + pcmData
    }
}
