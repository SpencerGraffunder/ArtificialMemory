import SwiftUI
import ArtMemCore

// Spec §3.5: ≤3 large high-contrast cards, arm-length readable; swipe = dismiss,
// long-press = pin, thumbs = feedback, tap = expand source. Big listen toggle +
// offline/queued banner (§3.4) + hold-to-ask (§3.6 fallback b).

struct LiveView: View {
    @EnvironmentObject var state: AppState
    @State private var askText = ""
    @State private var expandingCard: Card?

    var body: some View {
        NavigationStack {
            VStack(spacing: 14) {
                statusBanner
                ListenToggle()
                if state.cards.isEmpty {
                    ContentUnavailableView(
                        "No cards right now",
                        systemImage: "bubble.left",
                        description: Text("Related memories will appear here while you talk."))
                        .frame(maxHeight: .infinity)
                } else {
                    Spacer(minLength: 0)
                    ForEach(state.cards.reversed()) { card in
                        CardView(card: card)
                            .zIndex(card.priority ? 1 : 0)
                    }
                    Spacer(minLength: 0)
                }
                askField
            }
            .padding(.horizontal)
            .navigationTitle("ArtMem")
            .refreshable { await state.refreshHealth() }
        }
    }

    private var statusBanner: some View {
        HStack {
            switch state.status {
            case .online:
                Label("Connected", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
            case .offline:
                Label("Offline — \(state.queued) queued", systemImage: "wifi.slash")
                    .foregroundStyle(.orange)
            case .needsToken:
                Label("Set server + token in Settings", systemImage: "key.slash")
                    .foregroundStyle(.red)
            case .unknown:
                Label("Connecting…", systemImage: "circle.dashed")
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .font(.subheadline.weight(.medium))
        .padding(10)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
    }

    private var askField: some View {
        HStack {
            TextField("Ask a memory…", text: $askText)
                .textFieldStyle(.roundedBorder)
                .onSubmit { submit() }
            Button("Ask") { submit() }
                .buttonStyle(.borderedProminent)
                .disabled(askText.trimmingCharacters(in: .whitespaces).isEmpty)
        }
        .accessibilityIdentifier("askField")
    }

    private func submit() {
        let q = askText.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        askText = ""
        Task { await state.ask(q) }
    }
}

struct ListenToggle: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        Button(action: state.toggleListening) {
            VStack(spacing: 6) {
                Image(systemName: iconName)
                    .font(.system(size: 40, weight: .medium))
                Text(state.listening.rawValue.uppercased())
                    .font(.caption.weight(.bold))
                    .tracking(2)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 18)
            .background(background, in: RoundedRectangle(cornerRadius: 20))
            .foregroundStyle(.white)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("listenToggle")
    }

    private var iconName: String {
        switch state.listening {
        case .listening: "mic.fill"
        case .paused: "pause.circle.fill"
        case .interrupted: "exclamationmark.arrow.trianglehead.2.clockwise.rotate.90"
        case .error: "xmark.octagon.fill"
        case .idle: "mic.slash.fill"
        }
    }
    private var background: Color {
        switch state.listening {
        case .listening: .pink
        case .interrupted, .error: .red
        default: Color(.systemGray3)
        }
    }
}

struct CardView: View {
    let card: Card
    @EnvironmentObject var state: AppState
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(card.headline)
                        .font(.title3.weight(.bold))
                        .foregroundStyle(.primary)
                    if expanded {
                        Text(card.detail).font(.body)
                        if let url = card.url, let link = URL(string: url) {
                            Link("Source", destination: link).font(.callout)
                        }
                    }
                }
                Spacer(minLength: 0)
                if card.priority {
                    Image(systemName: "pin.fill").foregroundStyle(.yellow)
                }
            }
            HStack {
                Label(card.sourceChip, systemImage: card.origin == .web
                      ? "globe" : "brain.head.profile")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button { state.feedback(card, kind: .thumbDown) } label: {
                    Image(systemName: "hand.thumbs.down") }
                Button { state.feedback(card, kind: .thumbUp) } label: {
                    Image(systemName: "hand.thumbs.up") }
            }
            .buttonStyle(.bordered)
            .tint(.secondary)
        }
        .padding(14)
        .background(.regularMaterial,
                    in: RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18)
            .stroke(card.priority ? .yellow.opacity(0.6) : .white.opacity(0.08),
                    lineWidth: 1))
        .contentShape(Rectangle())
        .onTapGesture { withAnimation(.snappy) { expanded.toggle() } }
        .onLongPressGesture { state.feedback(card, kind: .pin) }
        .gesture(swipeAway)
        .accessibilityIdentifier("card-\(card.id)")
    }

    private var swipeAway: some Gesture {
        DragGesture(minimumDistance: 40)
            .onEnded { value in
                if value.translation.width < -50 || abs(value.translation.height) > 60 {
                    withAnimation(.snappy) { state.feedback(card, kind: .dismiss) }
                }
            }
    }
}
