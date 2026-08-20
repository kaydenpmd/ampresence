import SwiftUI

@main
struct AMPresenceApp: App {
    @StateObject private var controller = PresenceController()

    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(controller)
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var controller: PresenceController
    @State private var running = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Relay") {
                    TextField("https://music.yourdomain.com/now-playing",
                              text: $controller.endpoint)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    SecureField("Shared secret", text: $controller.secret)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }

                Section("Status") {
                    LabeledContent("Link", value: controller.linkStatus)
                    LabeledContent("Now playing", value: controller.lastPushed)
                    LabeledContent("Media access",
                                   value: controller.monitor.authorized ? "Granted" : "Not granted")
                }

                Section {
                    Button(running ? "Stop" : "Start") {
                        Task {
                            if running { await controller.stop() } else { await controller.start() }
                            running.toggle()
                        }
                    }
                    .disabled(controller.endpoint.isEmpty || controller.secret.isEmpty)
                }

                Section {
                    Text("Your PC must be awake with the Discord desktop app running. "
                         + "Presence clears automatically after 90 seconds of silence.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("AM Presence")
        }
    }
}
