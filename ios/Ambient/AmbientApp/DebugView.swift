import SwiftUI
import AmbientCore

// Debug (spec §3.5): queue depth, endpoint, thermal state, and a demo utterance
// sender that pushes through the exact production path (outbox → batch/WS) so the
// full pipeline can be exercised on a device without ASR models bound.

struct DebugView: View {
    @EnvironmentObject var state: AppState
    @State private var demoText = "Jasmine says dinner on Friday will be chili."
    @State private var asWhisper = false
    @State private var thermal = ProcessInfo.ThermalState.nominal
    @State private var drops = 0
    @State private var endpoint = "—"

    var body: some View {
        NavigationStack {
            Form {
                Section("Pipeline") {
                    LabeledContent("Status", value: state.status.rawValue)
                    LabeledContent("Endpoint", value: endpoint)
                    LabeledContent("Queue drops", value: "\(drops)")
                    LabeledContent("Outbox queued", value: "\(state.queued)")
                    LabeledContent("Listening", value: state.listening.rawValue)
                    LabeledContent("Thermal", value: "\(thermal)")
                }
                Section("Demo utterance → real outbox + WS") {
                    TextField("text", text: $demoText, axis: .vertical)
                        .font(.body.monospaced())
                    Toggle("flag as whisper query", isOn: $asWhisper)
                    Button("Emit") { state.emitDemoUtterance(demoText, whisper: asWhisper) }
                        .buttonStyle(.borderedProminent)
                    Text("Watch the server log: ingest → extraction → card pushed back over /v1/live. This proves end-to-end capture→memory→card on a physical device.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Bindings still pending device spikes (M0)") {
                    Label("FluidAudio ASR + diarization", systemImage: "hourglass")
                    Label("Owner voice enrollment UI", systemImage: "hourglass")
                    Label("Offline refinement (pyannote) pass", systemImage: "hourglass")
                    Label("Live Activity / Dynamic Island", systemImage: "hourglass")
                    Text("See docs/DECISIONS.md — every pending binding is a seam in AmbientCore, wired in AppState; none change the wire protocol.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Debug")
            .task {
                while !Task.isCancelled {
                    thermal = ProcessInfo.processInfo.thermalState
                    drops = await state.outbox?.dropAlerts.count ?? 0
                    if let router = state.router {
                        endpoint = await router.current().absoluteString
                    }
                    try? await Task.sleep(for: .seconds(3))
                }
            }
        }
    }
}
