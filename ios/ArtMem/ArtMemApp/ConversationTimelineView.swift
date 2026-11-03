import SwiftUI
import ArtMemCore

// Timeline of conversations (server truth) with gap markers for local listening
// holes (spec §3.1: suspension/killed detection shows a gap in the timeline).

struct ConversationTimelineView: View {
    @EnvironmentObject var state: AppState
    @State private var conversations: [APIClient.ConversationSummary] = []
    @State private var failed = ""

    var body: some View {
        NavigationStack {
            List {
                if !state.gaps.isEmpty {
                    Section("Listening gaps") {
                        ForEach(state.gaps.suffix(10).reversed(), id: \.self) { gap in
                            Label(gap.formatted(date: .abbreviated, time: .shortened),
                                  systemImage: "exclamationmark.triangle")
                                .foregroundStyle(.orange)
                        }
                    }
                }
                Section("Conversations") {
                    ForEach(conversations) { conv in
                        NavigationLink(value: conv.id) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(conv.startedAt.prefix(16).replacingOccurrences(
                                    of: "T", with: " "))
                                    .font(.body.monospaced())
                                if let n = conv.utteranceCount {
                                    Text("\(n) utterances")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    if conversations.isEmpty && failed.isEmpty {
                        Text("No conversations yet — emit one from Debug.").foregroundStyle(.secondary)
                    }
                    if !failed.isEmpty {
                        Text(failed).font(.caption).foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("Timeline")
            .navigationDestination(for: String.self) { TranscriptView(id: $0) }
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private func load() async {
        guard let api = state.api else { return }
        do { conversations = try await api.conversations().conversations }
        catch { failed = error.localizedDescription }
    }
}

struct TranscriptView: View {
    let id: String
    @EnvironmentObject var state: AppState
    @State private var lines: [APIClient.TranscriptLine] = []

    var body: some View {
        List(lines.reversed()) { line in
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(line.speakerName ?? line.speakerLabel ?? "?")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.tint)
                    if line.isWhisper == 1 {
                        Image(systemName: "wind").font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(String(line.startTs.suffix(8)))
                        .font(.caption2).foregroundStyle(.secondary)
                }
                Text(line.text)
            }
            .listRowSeparator(.visible)
        }
        .navigationTitle("Transcript")
        .task {
            guard let api = state.api else { return }
            lines = (try? await api.conversation(id: id))?.utterances ?? []
        }
    }
}
