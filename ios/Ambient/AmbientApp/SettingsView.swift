import SwiftUI
import AmbientCore

// Settings (spec §3.5): endpoints + token, card limit, haptics, whisper sensitivity,
// location toggle. Everything the spec demands configurable is here, stored in
// UserDefaults, read through AppState accessors.

struct SettingsView: View {
    @EnvironmentObject var state: AppState
    @State private var endpoints: [String] = []
    @State private var token = ""
    @State private var newEndpoint = ""
    @State private var healthLine = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Server endpoints (tried in order)") {
                    ForEach(Array(endpoints.enumerated()), id: \.offset) { i, url in
                        HStack {
                            Image(systemName: "server.rack")
                            TextField("http://…:8378", text: endpointBinding(i))
                                .textInputAutocapitalization(.never)
                                .font(.body.monospaced())
                            Button { endpoints.remove(at: i) } label: {
                                Image(systemName: "minus.circle").foregroundStyle(.red)
                            }
                        }
                    }
                    HStack {
                        TextField("http://homebox.local:8378", text: $newEndpoint)
                            .textInputAutocapitalization(.never)
                            .font(.body.monospaced())
                        Button {
                            guard !newEndpoint.isEmpty else { return }
                            endpoints.append(newEndpoint); newEndpoint = ""
                        } label: { Image(systemName: "plus.circle") }
                    }
                    Text("The app relies on Tailscale (or your LAN) being connected; it remembers the last endpoint that worked.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Token") {
                    SecureField("AMBIENT_TOKEN from your server", text: $token)
                        .textInputAutocapitalization(.never)
                        .font(.body.monospaced())
                    Button("Save & reconnect") { save() }
                    if !healthLine.isEmpty {
                        Text(healthLine).font(.caption)
                            .foregroundStyle(healthLine.hasPrefix("✓") ? .green : .red)
                    }
                }
                Section("Cards") {
                    Stepper("On-screen limit: \(state.cardBudget)",
                            value: Binding(get: { state.cardBudget },
                                           set: { state.cardBudget = $0}),
                            in: 1...5)
                    Toggle("Haptic tap on high-confidence card", isOn: Binding(
                        get: { state.hapticsOn }, set: { state.hapticsOn = $0 }))
                }
                Section("Whisper-to-self") {
                    VStack(alignment: .leading) {
                        Text("Whisper sensitivity: \(state.whisperRatio, specifier: "%.2f")")
                        Slider(value: Binding(get: { state.whisperRatio },
                                              set: { state.whisperRatio = $0}),
                               in: 0.05...0.5)
                        Text("How far below your calibrated speaking level counts as a whisper. Lower = stricter.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Text("Trigger-phrase fallback: “memory, …”")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Privacy") {
                    Toggle("Share coarse location with server", isOn: Binding(
                        get: { state.shareLocation }, set: { state.shareLocation = $0 }))
                    Text("Raw audio stays in the 10-minute rolling buffer on this device only; transcripts and embeddings are the persistent record.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
            .onAppear {
                endpoints = state.endpoints.map(\.absoluteString)
                token = state.token
            }
        }
    }

    private func endpointBinding(_ i: Int) -> Binding<String> {
        Binding(get: { endpoints[i] }, set: { endpoints[i] = $0 })
    }

    private func save() {
        state.endpoints = endpoints.compactMap(URL.init(string:))
        state.token = token.trimmingCharacters(in: .whitespaces)
        Task {
            await state.settingsChanged()
            switch state.status {
            case .online: healthLine = "✓ connected"
            case .needsToken: healthLine = "✗ token missing"
            default: healthLine = "✗ unreachable — queued locally, nothing lost"
            }
        }
    }
}
