# macOS on-device STT

Replaces the Google Cloud call in `src/view/audio/stt.py` with Apple's
on-device recogniser. No network, no API key, and no RAM taken from the
llama.cpp budget — recognition runs in a system service, not a process we host.

Two binaries. **`legacyspeechcli` is the one to use.** `speechcli` is kept
because it is faster to deploy (no TCC, no bundle) and because a future OS
release may fix the one thing that disqualifies it today.

| binary | API | macOS | custom vocabulary |
|---|---|---|---|
| `speechcli` | `SpeechAnalyzer` / `SpeechTranscriber` | 26+ | hook exists, **no effect** |
| `legacyspeechcli` | `SFSpeechRecognizer` | 10.15+ | **`contextualStrings`, works** |

## Why the older API wins

Measured on macOS 26.6 / Swift 6.2, one 4.02 s clip, same audio each time:

| | transcript |
|---|---|
| reference | "Where is **Gammeeok** and **Solle Spa** near the **Stavros Niarchos Foundation Library**" |
| `speechcli`, hints or not | "Whereas **Gamioc**, and **solely spawn** near the Stavros **Nyarkas** Foundation Library." |
| `legacyspeechcli`, no hints | "Where is **Cammock** and **Soli spa** near the Stavros Niarchos foundation library" |
| `legacyspeechcli`, `--hints` | "Where is **Gammeeok** and **Solle Spa** near the Stavros Niarchos Foundation Library" |

The legacy binary with hints is exact, capitalisation included, reproducibly.

The new API is not missing a custom-vocabulary hook — `AnalysisContext`
`.contextualStrings[.general]` is its equivalent of `preferred_phrases`, and
`speechcli` passes `--hints` through it. It simply does not bias the result:
output was byte-identical with and without, tested through both injection
paths (`analyzer.setContext(_:)` and the `analysisContext:` initialiser). Do
not read `--hints` on `speechcli` as working biasing.

Speed is not a deciding factor — both are far under realtime:

| | cold | warm |
|---|---|---|
| `speechcli` | 1.31 s | 0.35 s |
| `legacyspeechcli` (incl. app launch) | 0.83 s | ~0.46 s |

For reference, sending the same audio to `l3` over the tunnel costs **~1.9×
realtime**. Either binary is a large win; accuracy is what decides.

> Caveat on all of the above: the test clip was generated with `say`, which is
> cleaner than live microphone input. The ranking is not in doubt — the hints
> gap is far too large to be an artifact — but absolute accuracy will be worse
> on real recordings. Re-measure on a real mic clip before deleting the Google
> path.

## Build

Needs Xcode Command Line Tools (`xcode-select --install`).

`speechcli` is a plain binary:

```bash
swiftc -O -parse-as-library SpeechCLI.swift -o speechcli
```

`-parse-as-library` is required, not optional: `@main` cannot coexist with the
file-scope `die` helper in a file swiftc otherwise treats as the main file.

`legacyspeechcli` **must live in an .app bundle** — see Permissions below for
why. Build it as one:

```bash
APP=legacyspeechcli.app
mkdir -p "$APP/Contents/MacOS"
swiftc -O LegacySpeechCLI.swift -o "$APP/Contents/MacOS/legacyspeechcli"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>legacyspeechcli</string>
  <key>CFBundleIdentifier</key><string>local.camio.legacyspeechcli</string>
  <key>CFBundleName</key><string>legacyspeechcli</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSBackgroundOnly</key><true/>
  <key>NSSpeechRecognitionUsageDescription</key><string>Transcribes map questions on-device</string>
</dict>
PLIST
echo '</plist>' >> "$APP/Contents/Info.plist"
codesign -f -s - "$APP"
```

The `codesign` step is required. `swiftc -sectcreate` alone leaves the plist
`Info.plist=not bound` in the linker-generated signature, and TCC ignores it.

## Verify standalone

```bash
# ~5 seconds; ctrl-C to stop
ffmpeg -f avfoundation -i ":default" -ar 16000 -ac 1 -t 5 /tmp/test.wav

./speechcli /tmp/test.wav

open -W -n --stdout /tmp/out.txt --stderr /tmp/err.txt \
  ./legacyspeechcli.app --args /tmp/test.wav \
  --hints "Cafe China,Solle Spa,Gammeeok,Stavros Niarchos Foundation Library"
cat /tmp/out.txt
```

Run the legacy one both with and without `--hints` — the difference on POI
names is the whole reason it exists.

## Permissions

`speechcli` needs nothing. `SpeechAnalyzer` never prompts.

`legacyspeechcli` needs Speech Recognition access (TCC), and this is the
fiddliest part of the whole exercise:

- A **bare binary cannot get it**, whatever you do to its Info.plist. It
  aborts with a `TCC` namespace crash (`__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__`),
  not a catchable error. Embedding the plist via `-sectcreate` and re-signing
  is still not enough on its own.
- TCC attributes the request to the **responsible process**, which is whatever
  launched it — inheriting up the process tree, not the binary itself. Launch
  the `.app` through LaunchServices (`open -n ./legacyspeechcli.app --args …`)
  so the app is its own responsible process and gets its own grant.
- The first such launch raises a system dialog and the process blocks until it
  is answered. Answer it once; the grant persists.

On-device recognition also needs the dictation language model downloaded:
**System Settings → Keyboard → Dictation**. `legacyspeechcli` checks
`supportsOnDeviceRecognition` and refuses rather than silently falling back to
Apple's servers — the audio should never leave the machine.

**When wiring this into `mapio.py`, the responsible process becomes your Python
app**, so the grant has to be attributed there. Confirm that early — it is the
one remaining thing that could still block the integration.

## Wire it in

mapio needs a camera, a microphone and a display, so it runs on the machine
with the physical map — Windows, natively, not WSL (WSL2 webcam passthrough
needs usbipd and mediapipe wants a real device). The Mac stays a pure server:
llama.cpp on one port, transcription on another, both over the SSH tunnel.

Nothing is streamed. The client records locally and POSTs the whole WAV: 5 s of
16 kHz mono 16-bit is ~160 KB (208 KB base64'd), a few milliseconds on a LAN
against ~0.5 s of recognition.

**On the Mac:**

```bash
cd tools/macos_stt
python3 stt_server.py --bin ./legacyspeechcli.app --port 11435
```

Answer the Speech Recognition dialog once on the first request. Starting the
server from a Terminal means the grant is established there, not inside mapio.

**On the client, alongside the existing LLM tunnel:**

```bash
ssh -N -L 11435:localhost:11435 <mac>
curl localhost:11435/health          # {"ok": true, "bin": "..."}

export STT_BACKEND=apple
export STT_SERVER=http://localhost:11435
```

`STT_BACKEND` still defaults to `google`, so unsetting it restores current
behaviour.

**If mapio ever runs on the Mac itself**, leave `STT_SERVER` unset and point
`STT_APPLE_BIN` at the bundle; `audio_to_text` shells out directly instead.

```bash
export STT_APPLE_BIN=$PWD/tools/macos_stt/legacyspeechcli.app
```

A path ending in `.app` switches the invocation to
`open -W -n --stdout … --stderr … <app> --args <wav>`, because an .app cannot
be exec'd directly and its bare binary aborts on TCC. Output is read back from
the redirect files, and a non-empty transcript is the success signal —
`open -W` does not reliably propagate the app's exit status. Any other path is
exec'd directly, which is the `speechcli` case.

`--hints` is passed only for the `.app`, since it is the only build where
biasing has any measured effect. It comes from `STT.commands`, the same set
that was going to Google as `preferred_phrases`.

**Still unverified:** whether TCC attributes the recogniser to the Python app
rather than to the bundle when `open` is invoked from `subprocess`. `open`
hands off to LaunchServices, so the app should remain its own responsible
process and keep its own grant — but confirm before assuming, and expect at
worst one extra one-time dialog attributed to the terminal running `mapio.py`.

Note that `PHRASE_TIME_LIMIT` in `stt.py` is already `30`.

## Fixed along the way

Both files needed correcting before they ran; recorded here so the same ground
is not re-covered.

`SpeechCLI.swift` did not compile:

1. `@main` plus a file-scope `die` — needs `-parse-as-library`.
2. `SpeechTranscriber.Preset.offlineTranscription` does not exist. The real
   set is `.transcription`, `.transcriptionWithAlternatives`,
   `.timeIndexedTranscriptionWithAlternatives`, `.progressiveTranscription`,
   `.timeIndexedProgressiveTranscription`.

Everything else previously flagged as uncertain was fine as written:
`SpeechTranscriber(locale:preset:)`, `analyzer.start(inputAudioFile:finishAfterFile:)`
and `String(result.text.characters)` all exist.

`LegacySpeechCLI.swift` compiled but **never produced a transcript**: it waited
on a `DispatchSemaphore` on the main thread, while `recognitionTask`'s result
handler needs the main run loop to fire. Every run burned the full 60 s and
exited `timed out`. It now uses `CFRunLoopRun()` / `CFRunLoopStop`. The
authorization callback arrives off-main, which is why that semaphore appeared
to work and hid the problem.
