import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let stateURL: URL
    private let controlURL: URL
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let detail = NSMenuItem(title: "Waiting for Codex Local…", action: nil, keyEquivalent: "")
    private let activityDetail = NSMenuItem(title: "◇ Ready", action: nil, keyEquivalent: "")
    private let countsDetail = NSMenuItem(title: "No requests yet", action: nil, keyEquivalent: "")
    private var timer: Timer?
    private var frame = 0

    override init() {
        let args = CommandLine.arguments
        guard args.count >= 3 else {
            fputs("usage: CodexLocalMenu STATE_JSON CONTROL_JSONL\n", stderr)
            exit(2)
        }
        stateURL = URL(fileURLWithPath: args[1])
        controlURL = URL(fileURLWithPath: args[2])
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        setStatusTitle("◇ Local", color: .secondaryLabelColor)
        let menu = NSMenu()
        detail.isEnabled = false
        activityDetail.isEnabled = false
        countsDetail.isEnabled = false
        menu.addItem(detail)
        menu.addItem(activityDetail)
        menu.addItem(countsDetail)
        menu.addItem(.separator())
        menu.addItem(item("Restart local model", #selector(restartModel)))
        menu.addItem(item("Unload local model", #selector(unloadModel)))
        menu.addItem(item("Open diagnostics", #selector(openDiagnostics)))
        menu.addItem(.separator())
        menu.addItem(item("Quit menu controller", #selector(quitController)))
        statusItem.menu = menu
        refresh()
        timer = Timer.scheduledTimer(timeInterval: 0.25, target: self, selector: #selector(refresh), userInfo: nil, repeats: true)
    }

    private func item(_ title: String, _ action: Selector) -> NSMenuItem {
        let value = NSMenuItem(title: title, action: action, keyEquivalent: "")
        value.target = self
        return value
    }

    private func setStatusTitle(_ title: String, color: NSColor) {
        statusItem.button?.attributedTitle = NSAttributedString(
            string: title,
            attributes: [.foregroundColor: color]
        )
    }

    @objc private func refresh() {
        guard let data = try? Data(contentsOf: stateURL),
              let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        let server = state["server"] as? String ?? "oMLX"
        let model = state["model"] as? String ?? "local model"
        let slot = state["local_slot"] as? String ?? "Codex slot"
        let route = state["route"] as? String ?? "ready"
        let activity = state["activity"] as? String ?? "idle"
        let first = state["first_byte_ms"] as? Int
        let visible = state["first_visible_ms"] as? Int
        let total = state["total_ms"] as? Int
        let resident = state["resident"] as? Bool ?? false
        let prefixCached = state["prefix_cached"] as? Bool ?? false
        let warning = state["performance_warning"] as? String
        let localRequests = state["local_requests"] as? Int ?? 0
        let localResponses = state["local_responses"] as? Int ?? 0
        let remoteRequests = state["remote_requests"] as? Int ?? 0
        let remoteResponses = state["remote_responses"] as? Int ?? 0
        let frames = ["◐", "◓", "◑", "◒"]
        switch activity {
        case "local_receiving":
            setStatusTitle("◈ Local \(frames[frame % frames.count])", color: .systemOrange)
            activityDetail.title = "◈ Request received by \(server)"
        case "local_generating":
            setStatusTitle("◈ Local \(frames[frame % frames.count])", color: .systemGreen)
            activityDetail.title = first.map { "● \(model) generating · first byte \($0)ms" }
                ?? "● \(model) generating"
        case "local_success":
            setStatusTitle("◆ Local ✓", color: .systemGreen)
            activityDetail.title = total.map { "✓ Local response delivered in \($0)ms" }
                ?? "✓ Local response delivered"
        case "local_error":
            setStatusTitle("◆ Local !", color: .systemRed)
            activityDetail.title = "! Local inference failed"
        case "remote":
            setStatusTitle("◇ Local", color: .systemBlue)
            activityDetail.title = "○ OpenAI pass-through active"
        case "remote_error":
            setStatusTitle("◇ Local !", color: .systemRed)
            activityDetail.title = "! OpenAI pass-through failed"
        default:
            setStatusTitle(resident ? "◇ Local ✓" : "◇ Local", color: resident ? .systemGreen : .secondaryLabelColor)
            let readiness = [
                resident ? "resident" : nil,
                prefixCached ? "prefix cached" : nil
            ].compactMap { $0 }.joined(separator: " · ")
            activityDetail.title = readiness.isEmpty
                ? "◇ Ready for local requests"
                : "◇ \(readiness)"
        }
        frame += 1
        detail.title = "\(slot) → \(server) · \(model)"
        let timings = [
            first.map { "first \($0)ms" },
            visible.map { "visible \($0)ms" },
            total.map { "total \($0)ms" },
            warning.map { "! \($0.replacingOccurrences(of: "_", with: " "))" }
        ].compactMap { $0 }.joined(separator: " · ")
        let counts = "Local \(localResponses)/\(localRequests) · OpenAI \(remoteResponses)/\(remoteRequests)"
        countsDetail.title = timings.isEmpty ? counts : "\(counts) · \(timings)"
        statusItem.button?.toolTip = "\(detail.title)\n\(route)"
    }

    private func send(_ command: String) {
        let line = "{\"command\":\"\(command)\",\"time\":\(Date().timeIntervalSince1970)}\n"
        let data = Data(line.utf8)
        if !FileManager.default.fileExists(atPath: controlURL.path) {
            FileManager.default.createFile(atPath: controlURL.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: controlURL) else { return }
        defer { try? handle.close() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: data)
    }

    @objc private func restartModel() { send("restart") }
    @objc private func unloadModel() { send("unload") }
    @objc private func openDiagnostics() {
        NSWorkspace.shared.open(stateURL.deletingLastPathComponent())
    }
    @objc private func quitController() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
