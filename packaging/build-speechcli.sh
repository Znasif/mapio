#!/usr/bin/env bash
# Build the on-device transcriber bundle into tools/macos_stt/.
#
#   ./packaging/build-speechcli.sh [--sign "Developer ID Application: ..."]
#
# Two binaries exist in tools/macos_stt/; this builds the one that works.
# legacyspeechcli uses SFSpeechRecognizer, whose `contextualStrings` actually
# biases recognition toward POI names. speechcli uses SpeechAnalyzer, whose
# equivalent hook is present and measurably inert -- byte-identical output with
# and without hints. On proper nouns that is the entire failure mode, so only
# the legacy one is packaged.
#
# The bundle is a build artifact and is gitignored, like packaging/vendor.
#
# CFBundleIdentifier is FIXED from here on. TCC keys the Speech Recognition
# grant on bundle id and signature: change either and every existing user is
# prompted again. Ad-hoc signatures key on the code hash, so each rebuild
# re-prompts -- acceptable while developing, which is why release builds must
# pass --sign with a Developer ID. That identity is stable across rebuilds, so
# the grant survives updates.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(dirname "$HERE")/tools/macos_stt"
APP="$SRC/legacyspeechcli.app"
BUNDLE_ID="local.mapio.legacyspeechcli"

IDENTITY="-"   # ad-hoc
if [ "${1:-}" = "--sign" ]; then
  [ -n "${2:-}" ] || { echo "build-speechcli: --sign needs an identity" >&2; exit 1; }
  IDENTITY="$2"
fi

[ -f "$SRC/LegacySpeechCLI.swift" ] || {
  echo "build-speechcli: no LegacySpeechCLI.swift in $SRC" >&2; exit 1; }

echo "==> building $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O "$SRC/LegacySpeechCLI.swift" -o "$APP/Contents/MacOS/legacyspeechcli"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>legacyspeechcli</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>legacyspeechcli</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSBackgroundOnly</key><true/>
  <key>NSSpeechRecognitionUsageDescription</key><string>Transcribes map questions on-device</string>
</dict>
</plist>
PLIST

# Required, not cosmetic. Without it the linker-generated signature leaves
# Info.plist "not bound" and TCC ignores the bundle entirely -- which surfaces
# later as a recognition failure rather than a signing one.
echo "==> signing ($IDENTITY)"
codesign -f -s "$IDENTITY" "$APP"

sig="$(codesign -dv --verbose=2 "$APP" 2>&1)"
case "$sig" in
  *"Info.plist entries="*) ;;
  *) echo "build-speechcli: Info.plist did not bind into the signature" >&2; exit 1 ;;
esac

echo
echo "$APP"
echo "  bundle id : $BUNDLE_ID"
echo "  identity  : $IDENTITY"
echo "$sig" | grep -E "Identifier=|TeamIdentifier=|Info.plist entries=" | sed 's/^/  /'
