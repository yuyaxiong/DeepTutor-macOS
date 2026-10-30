import Cocoa
import Darwin
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var backendProcess: Process?
    private var startupTimer: Timer?
    private var runtimeURL: URL?
    private var hasLoaded = false
    private var isProbing = false
    private var startupDeadline = Date()
    private var logHandle: FileHandle?
    private var activityToken: NSObjectProtocol?
    private var appVersion = ""
    private var buildId = ""
    private var healthTimer: Timer?
    private var healthFailures = 0
    private var autoRestarts = 0
    private var lastAutoRestart = Date.distantPast
    private var isHealthProbing = false
    private var isRestarting = false
    private var isTerminating = false
    private var runtimeBackendPort: Int?

    private let supportDirectory: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("DeepTutor", isDirectory: true)
    }()

    private let logDirectory: URL = {
        let base = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library")
        return base.appendingPathComponent("Logs/DeepTutor", isDirectory: true)
    }()

    private var runtimeFile: URL {
        supportDirectory.appendingPathComponent(".runtime/launch.json")
    }

    private var launcherLog: URL {
        logDirectory.appendingPathComponent("launcher.log")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        loadBuildInfo()
        activityToken = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .idleSystemSleepDisabled, .automaticTerminationDisabled],
            reason: "DeepTutor local service"
        )
        buildMainMenu()
        buildStatusItem()
        buildWindow()
        startBackend()
        beginStartupPolling()
        NSApp.activate(ignoringOtherApps: true)
        NSRunningApplication.current.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
        window.makeKeyAndOrderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if isTerminating {
            return .terminateNow
        }
        guard let process = backendProcess, process.isRunning else {
            return .terminateNow
        }
        isTerminating = true
        stopHealthMonitoring()
        process.terminate()
        appendLog("\n--- waiting up to 15s for backend shutdown ---\n")
        DispatchQueue.global(qos: .userInitiated).async {
            let deadline = Date().addingTimeInterval(15)
            while process.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.1)
            }
            if process.isRunning {
                kill(process.processIdentifier, SIGKILL)
            }
            DispatchQueue.main.async {
                NSApp.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopHealthMonitoring()
        startupTimer?.invalidate()
        if let token = activityToken {
            ProcessInfo.processInfo.endActivity(token)
            activityToken = nil
        }
    }

    // MARK: - Window and menus

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.preferences.setValue(true, forKey: "developerExtrasEnabled")
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = true
        configuration.mediaTypesRequiringUserActionForPlayback = []

        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.autoresizingMask = [.width, .height]

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "DeepTutor"
        window.minSize = NSSize(width: 900, height: 600)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("DeepTutorMainWindow")
        window.isReleasedWhenClosed = false
        showStartingPage()
    }

    private func buildMainMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)

        let appMenu = NSMenu(title: "DeepTutor")
        addMenuItem(appMenu, "关于 DeepTutor", #selector(showAbout(_:)), "")
        appMenu.addItem(NSMenuItem.separator())
        addMenuItem(appMenu, "打开主窗口", #selector(openMainWindow(_:)), "o")
        addMenuItem(appMenu, "在浏览器中打开", #selector(openInBrowser(_:)), "b")
        addMenuItem(appMenu, "重启服务", #selector(restartBackend(_:)), "r")
        appMenu.addItem(NSMenuItem.separator())
        addMenuItem(appMenu, "打开数据目录", #selector(openDataDirectory(_:)), "")
        addMenuItem(appMenu, "打开日志目录", #selector(openLogDirectory(_:)), "")
        appMenu.addItem(NSMenuItem.separator())
        addMenuItem(appMenu, "退出 DeepTutor", #selector(quitApp(_:)), "q")
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "编辑")
        addResponderMenuItem(editMenu, "撤销", Selector(("undo:")), "z")
        addResponderMenuItem(editMenu, "重做", Selector(("redo:")), "Z")
        editMenu.addItem(NSMenuItem.separator())
        addResponderMenuItem(editMenu, "剪切", #selector(NSText.cut(_:)), "x")
        addResponderMenuItem(editMenu, "复制", #selector(NSText.copy(_:)), "c")
        addResponderMenuItem(editMenu, "粘贴", #selector(NSText.paste(_:)), "v")
        addResponderMenuItem(editMenu, "全选", #selector(NSText.selectAll(_:)), "a")
        editMenuItem.submenu = editMenu

        NSApp.mainMenu = mainMenu
    }

    private func addResponderMenuItem(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = nil
        menu.addItem(item)
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "book.closed", accessibilityDescription: "DeepTutor")
        }
        let menu = NSMenu()
        addMenuItem(menu, "打开 DeepTutor", #selector(openMainWindow(_:)), "")
        addMenuItem(menu, "在浏览器中打开", #selector(openInBrowser(_:)), "")
        addMenuItem(menu, "重启服务", #selector(restartBackend(_:)), "")
        menu.addItem(NSMenuItem.separator())
        addMenuItem(menu, "退出 DeepTutor", #selector(quitApp(_:)), "")
        statusItem.menu = menu
    }

    private func addMenuItem(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        menu.addItem(item)
    }

    // MARK: - Backend lifecycle

    private func startBackend() {
        guard let resources = Bundle.main.resourceURL else {
            showFatal("应用资源目录缺失。")
            return
        }
        let python = resources.appendingPathComponent("python/bin/python3")
        let bootstrap = resources.appendingPathComponent("bootstrap.py")
        let nodeBin = resources.appendingPathComponent("node/bin")

        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            showFatal("应用缺少 Python 运行时：\(python.path)")
            return
        }
        guard FileManager.default.fileExists(atPath: bootstrap.path) else {
            showFatal("应用缺少启动引导：\(bootstrap.path)")
            return
        }

        try? FileManager.default.createDirectory(at: logDirectory, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: launcherLog.path) {
            FileManager.default.createFile(atPath: launcherLog.path, contents: nil)
        }
        logHandle = try? FileHandle(forWritingTo: launcherLog)
        logHandle?.seekToEndOfFile()

        let process = Process()
        process.executableURL = python
        process.arguments = ["-B", bootstrap.path]
        process.currentDirectoryURL = resources
        process.qualityOfService = .userInitiated

        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = nodeBin.path + ":" + (environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        environment["DEEPTUTOR_HOME"] = supportDirectory.path
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment.removeValue(forKey: "PYTHONPATH")
        environment.removeValue(forKey: "PYTHONHOME")
        process.environment = environment

        let pipe = Pipe()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { return }
            try? self?.logHandle?.write(contentsOf: data)
        }
        process.standardOutput = pipe
        process.standardError = pipe
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                self?.backendDidExit(status: finished.terminationStatus)
            }
        }

        do {
            try process.run()
            backendProcess = process
            appendLog("\n--- launching \(python.path) ---\n")
        } catch {
            showFatal("无法启动后端：\(error.localizedDescription)")
        }
    }

    private func stopBackend(grace: TimeInterval = 3.0) {
        startupTimer?.invalidate()
        startupTimer = nil
        guard let process = backendProcess, process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(grace)
        while process.isRunning && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }
    }

    private func backendDidExit(status: Int32) {
        guard !hasLoaded, !isRestarting, !isTerminating else { return }
        if let url = runtimeURL ?? readRuntimeURL() {
            probe(url)
            return
        }
        failStartup("后端进程已退出（状态码 \(status)）。\n日志：\(launcherLog.path)")
    }

    // MARK: - Startup polling

    private func beginStartupPolling() {
        startupDeadline = Date().addingTimeInterval(180)
        startupTimer?.invalidate()
        startupTimer = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: true) { [weak self] _ in
            self?.pollStartup()
        }
    }

    private func pollStartup() {
        guard !hasLoaded else {
            startupTimer?.invalidate()
            return
        }
        if Date() > startupDeadline {
            failStartup("启动超时。\n日志：\(launcherLog.path)")
            return
        }
        guard let url = readRuntimeURL() else { return }
        runtimeURL = url
        probe(url)
    }

    private func readRuntimeObject() -> [String: Any]? {
        guard
            let data = try? Data(contentsOf: runtimeFile),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let version = object["app_version"] as? String,
            let build = object["build_id"] as? String,
            version == appVersion,
            build == buildId
        else {
            return nil
        }
        return object
    }

    private func readRuntimeURL() -> URL? {
        guard
            let object = readRuntimeObject(),
            let raw = object["url"] as? String,
            let url = URL(string: raw)
        else {
            return nil
        }
        return url
    }

    private func probe(_ url: URL) {
        if isProbing { return }
        isProbing = true
        var request = URLRequest(url: url.appendingPathComponent("chat"))
        request.timeoutInterval = 2
        request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                self.isProbing = false
                if let http = response as? HTTPURLResponse, (200...399).contains(http.statusCode) {
                    self.loadUI(url)
                }
            }
        }.resume()
    }

    private func loadUI(_ url: URL) {
        guard !hasLoaded else { return }
        hasLoaded = true
        isRestarting = false
        runtimeURL = url
        if let object = readRuntimeObject(), let port = object["backend_port"] as? Int {
            runtimeBackendPort = port
        }
        startupTimer?.invalidate()
        webView.load(URLRequest(url: url.appendingPathComponent("chat")))
        window.title = "DeepTutor"
        appendLog("\n--- web ready at \(url.absoluteString) ---\n")
        startHealthMonitoring()
    }

    private func startHealthMonitoring() {
        stopHealthMonitoring()
        healthFailures = 0
        healthTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.checkHealth()
        }
    }

    private func stopHealthMonitoring() {
        healthTimer?.invalidate()
        healthTimer = nil
        isHealthProbing = false
    }

    private func checkHealth() {
        guard
            hasLoaded,
            !isRestarting,
            !isHealthProbing,
            let frontendURL = runtimeURL,
            let backendPort = runtimeBackendPort,
            let backendURL = URL(string: "http://127.0.0.1:\(backendPort)/health/ready")
        else {
            return
        }
        isHealthProbing = true
        let group = DispatchGroup()
        var frontendOK = false
        var backendOK = false

        group.enter()
        var frontendRequest = URLRequest(url: frontendURL.appendingPathComponent("chat"))
        frontendRequest.timeoutInterval = 3
        frontendRequest.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: frontendRequest) { _, response, _ in
            frontendOK = (response as? HTTPURLResponse).map { (200...399).contains($0.statusCode) } ?? false
            group.leave()
        }.resume()

        group.enter()
        var backendRequest = URLRequest(url: backendURL)
        backendRequest.timeoutInterval = 3
        backendRequest.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: backendRequest) { _, response, _ in
            backendOK = (response as? HTTPURLResponse).map { (200...399).contains($0.statusCode) } ?? false
            group.leave()
        }.resume()

        group.notify(queue: .main) { [weak self] in
            guard let self, self.healthTimer != nil else { return }
            self.isHealthProbing = false
            if frontendOK && backendOK {
                self.healthFailures = 0
                if Date().timeIntervalSince(self.lastAutoRestart) > 300 {
                    self.autoRestarts = 0
                }
            } else {
                self.healthFailures += 1
                self.appendLog(
                    "\n--- health check failed (\(self.healthFailures)) "
                    + "frontend=\(frontendOK) backend=\(backendOK) ---\n"
                )
                if self.healthFailures >= 3 {
                    self.handleHealthFailure()
                }
            }
        }
    }

    private func handleHealthFailure() {
        stopHealthMonitoring()
        guard !isRestarting else { return }
        if autoRestarts >= 2 {
            failStartup("本地服务多次异常，已停止自动重启。\n请从菜单栏选择“重启服务”。")
            return
        }
        autoRestarts += 1
        lastAutoRestart = Date()
        appendLog("\n--- auto restart \(autoRestarts) ---\n")
        isRestarting = true
        hasLoaded = false
        runtimeURL = nil
        runtimeBackendPort = nil
        isProbing = false
        stopBackend(grace: 3.0)
        backendProcess = nil
        showStartingPage()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.startBackend()
            self?.beginStartupPolling()
        }
    }

    private func showStartingPage() {
        let html = """
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <style>
            :root { color-scheme: light dark; }
            body {
              margin: 0; height: 100vh; display: flex; align-items: center; justify-content: center;
              font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
              background: #f5f5f4; color: #1c1917;
            }
            .card { text-align: center; }
            .spinner {
              width: 32px; height: 32px; margin: 0 auto 20px; border-radius: 50%;
              border: 3px solid #d6d3d1; border-top-color: #16a34a;
              animation: spin 0.9s linear infinite;
            }
            h1 { font-size: 20px; font-weight: 600; margin: 0 0 8px; }
            p { font-size: 13px; color: #57534e; margin: 0; }
            @keyframes spin { to { transform: rotate(360deg); } }
            @media (prefers-color-scheme: dark) {
              body { background: #171717; color: #f5f5f4; }
              p { color: #a8a29e; }
              .spinner { border-color: #3f3f46; border-top-color: #22c55e; }
            }
          </style>
        </head>
        <body>
          <div class="card">
            <div class="spinner"></div>
            <h1>正在启动 DeepTutor</h1>
            <p>首次启动需要加载本地服务，请稍候。</p>
          </div>
        </body>
        </html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    private func failStartup(_ message: String) {
        stopHealthMonitoring()
        isRestarting = false
        startupTimer?.invalidate()
        startupTimer = nil
        let escaped = message
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
        let html = """
        <!doctype html>
        <html lang="zh-CN">
        <head><meta charset="utf-8"><style>
          body { font-family: -apple-system, sans-serif; padding: 40px; line-height: 1.6; }
          pre { white-space: pre-wrap; background: #f5f5f4; padding: 16px; border-radius: 8px; }
        </style></head>
        <body>
          <h2>DeepTutor 启动失败</h2>
          <pre>\(escaped)</pre>
          <p>可从菜单栏选择“重启服务”，或打开日志目录查看详情。</p>
        </body>
        </html>
        """
        webView.loadHTMLString(html, baseURL: nil)
        window.title = "DeepTutor - 启动失败"
    }

    private func appendLog(_ text: String) {
        guard let handle = logHandle else { return }
        try? handle.write(contentsOf: Data(text.utf8))
    }

    private func loadBuildInfo() {
        guard
            let resources = Bundle.main.resourceURL,
            let data = try? Data(contentsOf: resources.appendingPathComponent("build-info.json")),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
            return
        }
        appVersion = object["app_version"] as? String ?? ""
        buildId = object["build_id"] as? String ?? ""
    }

    private func showFatal(_ message: String) {
        failStartup(message)
        let alert = NSAlert()
        alert.messageText = "DeepTutor 无法启动"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.addButton(withTitle: "确定")
        alert.runModal()
    }

    // MARK: - Actions

    @objc private func showAbout(_ sender: Any?) {
        NSApp.orderFrontStandardAboutPanel(nil)
    }

    @objc private func openMainWindow(_ sender: Any?) {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func openInBrowser(_ sender: Any?) {
        if let url = runtimeURL ?? readRuntimeURL() {
            NSWorkspace.shared.open(url)
        } else {
            NSSound.beep()
        }
    }

    @objc private func openDataDirectory(_ sender: Any?) {
        try? FileManager.default.createDirectory(at: supportDirectory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(supportDirectory)
    }

    @objc private func openLogDirectory(_ sender: Any?) {
        try? FileManager.default.createDirectory(at: logDirectory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(logDirectory)
    }

    @objc private func restartBackend(_ sender: Any?) {
        autoRestarts = 0
        lastAutoRestart = .distantPast
        isRestarting = true
        hasLoaded = false
        runtimeURL = nil
        runtimeBackendPort = nil
        isProbing = false
        stopHealthMonitoring()
        stopBackend(grace: 3.0)
        backendProcess = nil
        showStartingPage()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.startBackend()
            self?.beginStartupPolling()
        }
    }

    @objc private func quitApp(_ sender: Any?) {
        NSApp.terminate(nil)
    }

    // MARK: - Navigation

    private func isLoopback(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        return host == "127.0.0.1" || host == "localhost" || host == "::1"
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        if url.scheme == "http" || url.scheme == "https" {
            if isLoopback(url) {
                decisionHandler(.allow)
            } else {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
            }
            return
        }
        if url.scheme == "file" || url.scheme == "about" || url.scheme == "data" {
            decisionHandler(.allow)
            return
        }
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLoopback(url) {
                webView.load(URLRequest(url: url))
            } else {
                NSWorkspace.shared.open(url)
            }
        }
        return nil
    }

    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.begin { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    func webView(
        _ webView: WKWebView,
        requestMediaCapturePermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        type: WKMediaCaptureType,
        decisionHandler: @escaping (WKPermissionDecision) -> Void
    ) {
        decisionHandler(.grant)
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        if navigationResponse.canShowMIMEType {
            decisionHandler(.allow)
        } else {
            decisionHandler(.download)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(
        _ download: WKDownload,
        decideDestinationUsing response: URLResponse,
        suggestedFilename: String,
        completionHandler: @escaping (URL?) -> Void
    ) {
        let downloads = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Downloads")
        var destination = downloads.appendingPathComponent(suggestedFilename)
        let base = (suggestedFilename as NSString).deletingPathExtension
        let ext = (suggestedFilename as NSString).pathExtension
        var counter = 1
        while FileManager.default.fileExists(atPath: destination.path) {
            let name = ext.isEmpty ? "\(base) (\(counter))" : "\(base) (\(counter)).\(ext)"
            destination = downloads.appendingPathComponent(name)
            counter += 1
        }
        completionHandler(destination)
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        if !hasLoaded {
            failStartup("无法加载界面：\(error.localizedDescription)")
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if !hasLoaded {
            failStartup("无法加载界面：\(error.localizedDescription)")
        }
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.run()
