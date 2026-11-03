import SwiftUI
import AmbientCore

// Memory browser + search (text & semantic go through the server hybrid), plus the
// in-app search bar wired to /v1/query (spec §5.5 second surface after whisper).

struct BrowseView: View {
    @EnvironmentObject var state: AppState
    @State private var query = ""
    @State private var memories: [Memory] = []
    @State private var total = 0
    @State private var failed = ""

    var body: some View {
        NavigationStack {
            List {
                ForEach(memories) { memory in
                    NavigationLink { MemoryDetail(memory: memory) } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(memory.text).font(.body)
                            HStack(spacing: 8) {
                                Text(memory.type).font(.caption2.weight(.semibold))
                                    .padding(.horizontal, 6).padding(.vertical, 2)
                                    .background(.tint.opacity(0.15), in: Capsule())
                                if let start = memory.eventStart {
                                    Text(String(start.prefix(10)))
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text("imp \(memory.importance, specifier: "%.1f")")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if memories.isEmpty {
                    Text(query.isEmpty
                         ? "Search or browse — memories appear as the server extracts them."
                         : "Nothing found.")
                        .foregroundStyle(.secondary)
                }
                if !failed.isEmpty { Text(failed).foregroundStyle(.red).font(.caption) }
            }
            .searchable(text: $query, prompt: "Search memories")
            .onSubmit(of: .search) { Task { await search() } }
            .navigationTitle("\(total) memories")
            .task { await search() }
            .refreshable { await search() }
        }
    }

    private func search() async {
        guard let api = state.api else { return }
        do {
            let page = try await api.memories(q: query)
            memories = page.memories
            total = page.total
            failed = ""
        } catch { failed = error.localizedDescription }
    }
}

struct MemoryDetail: View {
    let memory: Memory

    var body: some View {
        List {
            Section("Fact") { Text(memory.text) }
            Section("Timeline") {
                LabeledContent("Valid from", value: memory.validFrom)
                if let to = memory.validTo {
                    LabeledContent("Valid to", value: to)
                } else {
                    LabeledContent("Valid to", value: "still true")
                }
                if let s = memory.eventStart {
                    LabeledContent("Event", value: s + (memory.eventEnd.map { " → \($0)" } ?? ""))
                }
                LabeledContent("Recorded", value: memory.recordedAt)
            }
            Section("Provenance") {
                LabeledContent("Type", value: memory.type)
                LabeledContent("Source", value: memory.source)
                if let who = memory.speakerName { LabeledContent("Speaker", value: who) }
                LabeledContent("Status", value: memory.status)
                LabeledContent("Confidence", value: String(format: "%.2f", memory.confidence))
            }
        }
        .navigationTitle("Memory")
    }
}
