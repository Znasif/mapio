// SpeechCLI — macOS 26 SpeechAnalyzer/SpeechTranscriber wrapper.
//
// Reads a WAV file, prints the transcript to stdout, exits. Errors go to
// stderr with a non-zero exit code so the Python side can fall back.
//
//   ./speechcli /tmp/utterance.wav
//   ./speechcli /tmp/utterance.wav --locale en-US
//
// Build: swiftc -O -parse-as-library SpeechCLI.swift -o speechcli
// (-parse-as-library is required: @main cannot coexist with the top-level
// `die` in a file swiftc would otherwise treat as the main file.)
//
// NOTE: --hints is accepted but has no measured effect on this engine. See
// README "Custom vocabulary" before choosing this binary over the legacy one.

import AVFoundation
import Foundation
import Speech

func die(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

@main
struct SpeechCLI {
    static func main() async {
        var args = Array(CommandLine.arguments.dropFirst())
        var localeID = "en-US"
        var hints: [String] = []

        if let i = args.firstIndex(of: "--locale"), i + 1 < args.count {
            localeID = args[i + 1]
            args.removeSubrange(i...(i + 1))
        }
        if let i = args.firstIndex(of: "--hints"), i + 1 < args.count {
            hints = args[i + 1]
                .split(separator: ",")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            args.removeSubrange(i...(i + 1))
        }
        guard let path = args.first else {
            die("usage: speechcli <wav-path> [--locale en-US] [--hints \"a,b,c\"]")
        }

        let url = URL(fileURLWithPath: path)
        guard FileManager.default.fileExists(atPath: path) else {
            die("no such file: \(path)")
        }

        let locale = Locale(identifier: localeID)

        do {
            // --- 1. locale support -------------------------------------------------
            let supported = await SpeechTranscriber.supportedLocales
            let wanted = locale.identifier(.bcp47)
            guard supported.contains(where: { $0.identifier(.bcp47) == wanted }) else {
                die("locale \(wanted) not supported; available: "
                    + supported.map { $0.identifier(.bcp47) }.joined(separator: ", "))
            }

            // --- 2. transcriber ----------------------------------------------------
            // Available presets: .transcription, .transcriptionWithAlternatives,
            // .timeIndexedTranscriptionWithAlternatives, .progressiveTranscription,
            // .timeIndexedProgressiveTranscription. The plain one is final-results-
            // only, which is what a batch file transcription wants.
            let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)

            // --- 3. make sure the on-device model is installed ---------------------
            // First run downloads it; subsequent runs are a no-op. This is why the
            // very first invocation can take a while and needs a network connection.
            if let request = try await AssetInventory.assetInstallationRequest(
                supporting: [transcriber]
            ) {
                try await request.downloadAndInstall()
            }

            // --- 4. analyse the file ------------------------------------------------
            let analyzer = SpeechAnalyzer(modules: [transcriber])

            // AnalysisContext.contextualStrings is this API's counterpart to
            // SFSpeechRecognitionRequest.contextualStrings. It exists and is
            // accepted, but measured against POI names it changed nothing --
            // output was byte-identical with and without. Wired up anyway so a
            // future OS release can be re-measured cheaply.
            if !hints.isEmpty {
                let context = AnalysisContext()
                context.contextualStrings[.general] = hints
                try await analyzer.setContext(context)
            }

            let audioFile = try AVAudioFile(forReading: url)
            try await analyzer.start(inputAudioFile: audioFile, finishAfterFile: true)

            var transcript = ""
            for try await result in transcriber.results where result.isFinal {
                transcript += String(result.text.characters)
            }

            let cleaned = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !cleaned.isEmpty else { die("empty transcript") }
            print(cleaned)
            exit(0)
        } catch {
            die("speech error: \(error.localizedDescription)")
        }
    }
}
