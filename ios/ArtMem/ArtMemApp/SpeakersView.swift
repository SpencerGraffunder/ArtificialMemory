import SwiftUI
import ArtMemCore

// Speakers: server registry (rename = “that Speaker-2 is Jasmine”, merge two
// clusters). Local voice-print management is the M2 offline-refinement UI follow-up.

struct SpeakersView: View {
    @EnvironmentObject var state: AppState
    @State private var speakers: [Speaker] = []
    @State private var renameTarget: Speaker?
    @State private var newName = ""
    @State private var mergeFrom: Speaker?
    @State private var failed = ""

    var body: some View {
        NavigationStack {
            List {
                ForEach(speakers) { speaker in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(speaker.name ?? speaker.label ?? speaker.id)
                                .font(.body.weight(speaker.isOwner != 0 ? .bold : .regular))
                            Text("\(speaker.utteranceCount ?? 0) utterances · "
                                 + (speaker.lastSeen.map { String($0.prefix(10)) } ?? "never"))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if speaker.isOwner != 0 {
                            Image(systemName: "person.crop.circle.badge.checkmark")
                                .foregroundStyle(.tint)
                        }
                    }
                    .swipeActions {
                        Button("Rename") {
                            newName = speaker.name ?? speaker.label ?? ""
                            renameTarget = speaker
                        }.tint(.blue)
                        Button("Merge…") { mergeFrom = speaker }.tint(.purple)
                    }
                }
                if !failed.isEmpty { Text(failed).font(.caption).foregroundStyle(.red) }
            }
            .navigationTitle("Speakers")
            .task { await load() }
            .refreshable { await load() }
            .alert("Rename speaker", isPresented: Binding(
                get: { renameTarget != nil }, set: { if !$0 { renameTarget = nil } })) {
                TextField("Name", text: $newName)
                Button("Save") { Task { await rename() } }
                Button("Cancel", role: .cancel) {}
            }
            .alert("Merge speaker", isPresented: Binding(
                get: { mergeFrom != nil && speakers.count > 1 },
                set: { if !$0 { mergeFrom = nil } })) {
                ForEach(speakers.filter { $0.id != mergeFrom?.id }) { target in
                    Button("into \(target.name ?? target.label ?? "…")") {
                        Task { await merge(into: target) }
                    }
                }
                Button("Cancel", role: .cancel) {}
            }
        }
    }

    private func load() async {
        guard let api = state.api else { return }
        do { speakers = try await api.speakers().speakers; failed = "" }
        catch { failed = error.localizedDescription }
    }

    private func rename() async {
        guard let api = state.api, let target = renameTarget,
              !newName.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        do {
            try await api.renameSpeaker(id: target.id, name: newName.trimmingCharacters(in: .whitespaces))
            renameTarget = nil
            await load()
            state.toast = "Renamed — future speech will use this name"
        } catch { failed = error.localizedDescription }
    }

    private func merge(into target: Speaker) async {
        guard let api = state.api, let from = mergeFrom else { return }
        do {
            try await api.mergeSpeakers(from: from.id, into: target.id)
            mergeFrom = nil
            await load()
            state.toast = "Merged clusters"
        } catch { failed = error.localizedDescription }
    }
}
