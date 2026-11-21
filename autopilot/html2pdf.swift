// html2pdf IN.html OUT.pdf: prints an HTML file to a paginated A4 PDF with macOS's own WebKit (the journal's PDF
// pages, autopilot/pdf.py). Built once with `swiftc -O -o html2pdf html2pdf.swift`; needs a logged-in session, as
// the launchd agent has. Exits 0 when the PDF is written, 1 on a failure or after 60 seconds.
import Cocoa
import WebKit

let args = CommandLine.arguments
guard args.count == 3 else { FileHandle.standardError.write("usage: html2pdf IN.html OUT.pdf\n".data(using: .utf8)!); exit(2) }
let input = URL(fileURLWithPath: args[1])
let output = URL(fileURLWithPath: args[2])

final class Printer: NSObject, WKNavigationDelegate {
    let web: WKWebView
    init(_ web: WKWebView) { self.web = web }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self.print() }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { fail("\(error)") }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { fail("\(error)") }
    func print() {
        let info = NSPrintInfo()
        info.paperSize = NSSize(width: 595.28, height: 841.89)            // A4 in points
        info.topMargin = 36; info.bottomMargin = 36; info.leftMargin = 36; info.rightMargin = 36
        info.horizontalPagination = .fit
        info.verticalPagination = .automatic
        info.isHorizontallyCentered = false
        info.isVerticallyCentered = false
        info.jobDisposition = .save
        info.dictionary()[NSPrintInfo.AttributeKey.jobSavingURL] = output
        let op = web.printOperation(with: info)
        op.showsPrintPanel = false
        op.showsProgressPanel = false
        op.view?.frame = web.bounds
        op.runModal(for: window, delegate: self, didRun: #selector(printed(_:success:contextInfo:)), contextInfo: nil)
    }
    @objc func printed(_ op: NSPrintOperation, success: Bool, contextInfo: UnsafeMutableRawPointer?) {
        exit(success ? 0 : 1)
    }
}

func fail(_ msg: String) -> Never { FileHandle.standardError.write((msg + "\n").data(using: .utf8)!); exit(1) }

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 523, height: 770))
let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 523, height: 770), styleMask: [.borderless],
                      backing: .buffered, defer: false)
window.contentView = web
let printer = Printer(web)
web.navigationDelegate = printer
web.loadFileURL(input, allowingReadAccessTo: input.deletingLastPathComponent())
DispatchQueue.main.asyncAfter(deadline: .now() + 60) { fail("timed out after 60 s") }
app.run()
