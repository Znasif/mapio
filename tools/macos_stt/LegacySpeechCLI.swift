// LegacySpeechCLI — SFSpeechRecognizer wrapper (macOS 10.15+).
//
// Exists for one reason: SFSpeechRecognizer has `contextualStrings`, the direct
// replacement for the `preferred_phrases` we lose by dropping Google Cloud.
// If macOS 26's SpeechTranscriber turns out to expose no equivalent custom
// vocabulary hook, this is the better backend for map POI names
// ("Gammeeok", "Stavros Niarchos Foundation Library", "Solle Spa").
//
//   ./legacyspeechcli /tmp/utterance.wav --hints "Cafe China,Solle Spa,Gammeeok"
//
// Prints the transcript to stdout; errors to stderr with a non-zero exit.
//
// Must be run from inside an .app bundle -- see README. A bare binary cannot
// get Speech Recognition access no matter how its Info.plist is embedded.

import Foundation
import Speech

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

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
    fail("usage: legacyspeechcli <wav-path> [--locale en-US] [--hints \"a,b,c\"]")
}
guard FileManager.default.fileExists(atPath: path) else { fail("no such file: \(path)") }

// TCC: the first run prompts for Speech Recognition access. A bare CLI has no
// bundle, so the grant attributes to the parent app (Terminal / your IDE). See
// README for embedding an Info.plist if the prompt never appears.
let authSemaphore = DispatchSemaphore(value: 0)
var authorized = false
SFSpeechRecognizer.requestAuthorization { status in
    authorized = (status == .authorized)
    authSemaphore.signal()
}
authSemaphore.wait()
guard authorized else { fail("speech recognition not authorized") }

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeID)) else {
    fail("no recognizer for locale \(localeID)")
}
guard recognizer.isAvailable else { fail("recognizer unavailable") }
guard recognizer.supportsOnDeviceRecognition else {
    // Refuse rather than silently sending audio to Apple's servers -- the whole
    // point of this exercise is that nothing leaves the machine.
    fail("on-device recognition unavailable: install the dictation language model "
         + "(System Settings > Keyboard > Dictation)")
}

let request = SFSpeechURLRecognitionRequest(url: URL(fileURLWithPath: path))
request.requiresOnDeviceRecognition = true
request.shouldReportPartialResults = false
request.contextualStrings = hints
request.taskHint = .search   // short lookups, not long-form dictation

// The recognition handler is delivered through the main run loop, so the main
// thread must stay free to service it. Waiting on a DispatchSemaphore here
// deadlocks -- the handler never gets to run and every invocation gives up
// without a transcript.
var transcript: String?
var failure: String?

recognizer.recognitionTask(with: request) { result, error in
    if let error = error {
        failure = error.localizedDescription
        CFRunLoopStop(CFRunLoopGetMain())
        return
    }
    guard let result = result else { return }
    if result.isFinal {
        transcript = result.bestTranscription.formattedString
        CFRunLoopStop(CFRunLoopGetMain())
    }
}

// Runs until the handler above stops it. A short clip comes back in well under
// a second once the model is resident.
CFRunLoopRun()
if let failure = failure { fail("speech error: \(failure)") }

let cleaned = (transcript ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
guard !cleaned.isEmpty else { fail("empty transcript") }
print(cleaned)
