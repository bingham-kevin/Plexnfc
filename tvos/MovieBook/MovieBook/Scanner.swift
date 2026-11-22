#if os(iOS)
import SwiftUI
import VisionKit

/// The camera, reading the QR codes printed in the movie book. Closes on the first one.
struct BookScanner: View {
    let found: (String) -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Group {
                if DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                    QRScanner { code in
                        dismiss()
                        found(code)
                    }
                    .ignoresSafeArea()
                    .overlay(alignment: .bottom) {
                        Text("Point the camera at a movie's code in the book.")
                            .font(.callout)
                            .padding(.horizontal, 16).padding(.vertical, 10)
                            .background(.ultraThinMaterial, in: Capsule())
                            .padding(.bottom, 40)
                    }
                } else {
                    ContentUnavailableView(
                        "Can't use the camera", systemImage: "camera",
                        description: Text(DataScannerViewController.isSupported
                            ? "Allow Marquee to use the camera in Settings → Privacy & Security → Camera."
                            : "This \(UIDevice.current.model) can't scan codes."))
                }
            }
            .navigationTitle("Scan a Movie")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
    }
}

private struct QRScanner: UIViewControllerRepresentable {
    let found: (String) -> Void

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(recognizedDataTypes: [.barcode(symbologies: [.qr])],
                                                qualityLevel: .balanced, isHighlightingEnabled: true)
        scanner.delegate = context.coordinator
        try? scanner.startScanning()
        return scanner
    }

    func updateUIViewController(_ scanner: DataScannerViewController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(found: found) }

    @MainActor
    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let found: (String) -> Void
        private var done = false
        init(found: @escaping (String) -> Void) { self.found = found }

        func dataScanner(_ scanner: DataScannerViewController, didAdd items: [RecognizedItem],
                         allItems: [RecognizedItem]) {
            for case .barcode(let code) in items {
                guard !done, let text = code.payloadStringValue else { continue }
                done = true
                scanner.stopScanning()
                found(text)
            }
        }
    }
}
#endif
