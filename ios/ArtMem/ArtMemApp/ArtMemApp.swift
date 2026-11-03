import SwiftUI
import ArtMemCore

@main
struct ArtMemApp: App {
    @StateObject private var state = AppState.shared

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(state)
                .tint(.orange)
                .onReceive(NotificationCenter.default.publisher(
                    for: UIApplication.didBecomeActiveNotification)) { _ in
                    Task { await state.refreshHealth() }
                }
        }
    }
}

struct RootView: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        TabView {
            LiveView().tabItem { Label("Live", systemImage: "waveform") }
            ConversationTimelineView().tabItem { Label("Timeline", systemImage: "timeline.selection") }
            BrowseView().tabItem { Label("Memories", systemImage: "brain") }
            InboxView().tabItem { Label("Inbox", systemImage: "tray.full") }
            SpeakersView().tabItem { Label("Speakers", systemImage: "person.2") }
            SettingsView().tabItem { Label("Settings", systemImage: "gearshape.2") }
            DebugView().tabItem { Label("Debug", systemImage: "ladybug") }
        }
        .overlay(alignment: .top) { ToastBar() }
    }
}

struct ToastBar: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        Group {
            if let toast = state.toast {
                Text(toast)
                    .font(.callout.weight(.medium))
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.top, 6)
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .task {
                        try? await Task.sleep(for: .seconds(4))
                        withAnimation { state.toast = nil }
                    }
                    .onTapGesture { withAnimation { state.toast = nil } }
            }
        }
        .animation(.snappy, value: state.toast)
    }
}
