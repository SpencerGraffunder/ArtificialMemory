import SwiftUI
import ArtMemCore

// Inbox: the digest of clarifications the system wants answered (spec §7).
// Answering flows to the server → becomes a memory + vocabulary update.

struct InboxView: View {
    @EnvironmentObject var state: AppState
    @State private var questions: [Clarification] = []
    @State private var answers: [String: String] = [:]
    @State private var showAll = false
    @State private var all: [Clarification] = []
    @State private var busyId: String?
    @State private var failed = ""

    var body: some View {
        NavigationStack {
            List {
                Section(showAll ? "All open" : "Today’s digest") {
                    ForEach(showAll ? all : questions) { item in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(item.kind.replacingOccurrences(of: "_", with: " "))
                                    .font(.caption2.weight(.bold).smallCaps())
                                    .padding(.horizontal, 6).padding(.vertical, 2)
                                    .background(.tint.opacity(0.15), in: Capsule())
                                Spacer()
                                Text("EV \(item.importance * item.uncertainty, specifier: "%.2f")")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            Text(item.question).font(.body)
                            HStack {
                                TextField("Your answer…",
                                          text: binding(for: item.id))
                                    .textFieldStyle(.roundedBorder)
                                Button("Send") { answer(item) }
                                    .disabled(answers[item.id]?
                                        .trimmingCharacters(in: .whitespaces).isEmpty ?? true)
                            }
                        }
                        .padding(.vertical, 4)
                    }
                    if (showAll ? all : questions).isEmpty {
                        Text("Nothing to clarify — nice.")
                            .foregroundStyle(.secondary)
                    }
                    if !failed.isEmpty { Text(failed).font(.caption).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Inbox")
            .toolbar {
                Toggle("All", isOn: $showAll)
                    .onChange(of: showAll) { _, on in Task { await load(all: on) } }
            }
            .task { await load(all: false) }
            .refreshable { await load(all: showAll) }
        }
    }

    private func binding(for id: String) -> Binding<String> {
        Binding(get: { answers[id] ?? "" }, set: { answers[id] = $0 })
    }

    private func load(all: Bool) async {
        guard let api = state.api else { return }
        do {
            if all {
                self.all = try await api.clarifications().clarifications
            } else {
                questions = try await api.clarificationDigest().questions
            }
            failed = ""
        } catch { failed = error.localizedDescription }
    }

    private func answer(_ item: Clarification) {
        guard let api = state.api,
              let text = answers[item.id]?.trimmingCharacters(in: .whitespaces),
              !text.isEmpty else { return }
        busyId = item.id
        Task {
            do {
                _ = try await api.answerClarification(id: item.id, answer: text)
                answers[item.id] = ""
                await load(all: showAll)
                state.toast = "Answered — filed as a memory"
            } catch { failed = error.localizedDescription }
            busyId = nil
        }
    }
}
