#!/usr/bin/env bash
# Build the two dependencies a packaged install cannot get from PyPI.
#
#   ./packaging/build-wheels.sh [outdir]     default: packaging/vendor
#
# Everything else in the dependency set resolves from PyPI as a wheel. These two
# do not:
#
#   pyaudio                 no macOS wheels on PyPI at all, so pip builds it from
#                           source against a portaudio the user is expected to
#                           have installed. A package cannot expect that.
#   SpeechRecognition fork  lives in a git repo, so installing it needs git and
#                           reachable GitHub. A package cannot expect those either.
#
# Wheels are build artifacts and are gitignored; this script is how they are
# reproduced. Run it on Apple Silicon with the Xcode command line tools present.

set -euo pipefail

OUT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vendor}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PYTHON_VERSION=3.11
PORTAUDIO_URL="https://files.portaudio.com/archives/pa_stable_v190700_20210406.tgz"
SR_FORK="git+https://github.com/Matteo-3033/speech_recognition.git"

# The deployment target for the whole package. mediapipe and opencv publish
# macosx_11_0 wheels, so anything we build ourselves has to reach at least as
# far back or it becomes the floor for the entire install.
export MACOSX_DEPLOYMENT_TARGET=11.0

mkdir -p "$OUT"
echo "==> build environment"
uv venv --python "$PYTHON_VERSION" --seed "$WORK/bld" >/dev/null
"$WORK/bld/bin/pip" install -q delocate

# ---------------------------------------------------------------------------
# portaudio, from source.
#
# Homebrew is not usable here even when it is installed: its bottles are built
# for the host OS, so on macOS 26 libportaudio comes out with `minos 26.0` and
# delocate faithfully retags the wheel macosx_26_0_arm64 -- which then refuses
# to install on macOS 15 and earlier. Building it ourselves is the only way to
# control the deployment target.
# ---------------------------------------------------------------------------
echo "==> portaudio (deployment target $MACOSX_DEPLOYMENT_TARGET)"
curl -sSL -o "$WORK/pa.tgz" "$PORTAUDIO_URL"
tar xzf "$WORK/pa.tgz" -C "$WORK"
(
  cd "$WORK/portaudio"
  CFLAGS="-mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET -arch arm64" \
  LDFLAGS="-mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET -arch arm64" \
    ./configure --prefix="$WORK/painstall" \
                --disable-mac-universal --enable-shared --disable-static >/dev/null 2>&1
  make -j"$(sysctl -n hw.ncpu)" >/dev/null 2>&1
  make install >/dev/null 2>&1
)

# ---------------------------------------------------------------------------
# pyaudio, linked against it, then delocated so the dylib rides inside the wheel.
#
# --no-cache-dir is load-bearing: pip will otherwise hand back a previously
# built pyaudio wheel that was linked against whatever portaudio was around at
# the time, and the flags above are silently ignored.
# ---------------------------------------------------------------------------
echo "==> pyaudio"
CFLAGS="-I$WORK/painstall/include -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET" \
LDFLAGS="-L$WORK/painstall/lib -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET" \
  "$WORK/bld/bin/pip" wheel --no-cache-dir --no-deps --no-binary :all: \
    "pyaudio==0.2.14" -w "$WORK/raw" >/dev/null
"$WORK/bld/bin/delocate-wheel" -w "$OUT" "$WORK"/raw/pyaudio-*.whl

echo "==> SpeechRecognition fork"
"$WORK/bld/bin/pip" wheel --no-deps "$SR_FORK" -w "$OUT" >/dev/null

echo
echo "wheels in $OUT:"
for whl in "$OUT"/*.whl; do
  printf '  %-58s %s\n' "$(basename "$whl")" "$(du -h "$whl" | cut -f1)"
done

# A wheel tagged for a newer macOS than the deployment target means the
# portaudio build did not take, and the failure would otherwise only surface on
# someone else's older machine.
if ls "$OUT"/pyaudio-*macosx_11_0_arm64.whl >/dev/null 2>&1; then
  echo
  echo "OK: pyaudio is tagged macosx_11_0_arm64 and bundles its own portaudio."
else
  echo
  echo "ERROR: pyaudio wheel is not tagged macosx_11_0_arm64 -- check the portaudio build." >&2
  exit 1
fi
