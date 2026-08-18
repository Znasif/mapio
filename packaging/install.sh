#!/usr/bin/env bash
# Install MapIO and its local serving stack on an Apple Silicon Mac.
#
#   ./packaging/install.sh                      install with defaults
#   ./packaging/install.sh --prefix /opt/MapIO  install elsewhere
#   ./packaging/install.sh --no-bootstrap       generate everything, start nothing
#   ./packaging/install.sh --dry-run            print what would happen
#
# Two roots, and the split is the point:
#
#   --prefix   the application. Code, res/, the locked venv, the llama binary,
#              the transcriber bundle. Replaced wholesale on update.
#   --home     the user's data: models/, .env, embedding cache, chat logs.
#              Never touched by an update.
#
# Idempotent: re-running upgrades the application and leaves data alone.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATES="$REPO/packaging/templates"

PREFIX="${MAPIO_PREFIX:-$HOME/Applications/MapIO}"
HOME_DIR="${MAPIO_HOME:-$HOME/Library/Application Support/MapIO}"
LABEL="local.mapio"
LLM_PORT=8091
STT_PORT=11445
LLAMA_BIN="${MAPIO_LLAMA_BIN:-}"
# Pinned so every install serves the same engine. Overridable, but a moving
# target here means a bug report cannot be reproduced.
LLAMA_VERSION="${MAPIO_LLAMA_VERSION:-b10344}"
# Overridable so a test install can be validated without writing into the real
# LaunchAgents directory, where a stray plist outlives the test.
AGENTS="${MAPIO_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
BOOTSTRAP=1
PULL=1
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)       PREFIX="$2"; shift 2 ;;
    --home)         HOME_DIR="$2"; shift 2 ;;
    --label)        LABEL="$2"; shift 2 ;;
    --llm-port)     LLM_PORT="$2"; shift 2 ;;
    --stt-port)     STT_PORT="$2"; shift 2 ;;
    --llama-bin)     LLAMA_BIN="$2"; shift 2 ;;
    --llama-version) LLAMA_VERSION="$2"; shift 2 ;;
    --agents-dir)   AGENTS="$2"; shift 2 ;;
    --no-bootstrap) BOOTSTRAP=0; shift ;;
    --no-pull)      PULL=0; shift ;;
    --dry-run)      DRY=1; BOOTSTRAP=0; shift ;;
    -h|--help)      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "install: unknown argument $1" >&2; exit 1 ;;
  esac
done

say()  { printf '==> %s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf '    [dry] %s\n' "$*"; else "$@"; fi; }

# ---------------------------------------------------------------------------
# Preflight. Each check names the fix, because the failures they catch surface
# much later as something that looks unrelated.
# ---------------------------------------------------------------------------
say "preflight"

[ "$(uname -s)" = "Darwin" ] || { echo "install: macOS only" >&2; exit 1; }
# uname -m reports the architecture of THIS PROCESS, not of the machine. A
# terminal set to "Open using Rosetta" -- which MapIO's own README once
# recommended, to get an Intel Python for PyAudio -- reports x86_64 on an M-series
# Mac, and the old message here then blamed the hardware for a shell setting.
if [ "$(uname -m)" != "arm64" ]; then
  translated="$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)"
  is_arm="$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)"

  if [ "$is_arm" = "1" ]; then
    echo "install: this shell is x86_64 on Apple Silicon hardware" \
         "($(sysctl -n machdep.cpu.brand_string 2>/dev/null))." >&2
    [ "$translated" = "1" ] && echo "         It is being translated by Rosetta." >&2
    echo "         Start a native shell and re-run:" >&2
    echo "           arch -arm64 /bin/zsh" >&2
    echo "           ./packaging/install.sh" >&2
    echo "         To make it permanent, uncheck 'Open using Rosetta' in the" >&2
    echo "         terminal application's Get Info panel." >&2
  else
    echo "install: Apple Silicon only -- the vendored pyaudio wheel, the llama" >&2
    echo "         build and the Metal serving path are all arm64." >&2
  fi
  exit 1
fi

macos_major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$macos_major" -ge 12 ] || {
  echo "install: macOS 12 or later required (found $(sw_vers -productVersion))" >&2
  exit 1; }

# uv provisions the pinned Python 3.11, so the install never depends on a
# system Python. Installed into the prefix rather than ~/.local/bin when it is
# missing: it is this application's build tool, not something the user asked to
# have on their PATH, and deleting the prefix should take it with it.
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ]; then
  if [ "$DRY" = 1 ]; then
    echo "    [dry] would install uv into $PREFIX/runtime/bin"
    UV=uv
  else
    say "installing uv (not found on PATH)"
    mkdir -p "$PREFIX/runtime/bin"
    curl -LsSf --retry 3 https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="$PREFIX/runtime/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 || {
        echo "install: could not install uv automatically. Install it and re-run:" >&2
        echo "           curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
        exit 1; }
    UV="$PREFIX/runtime/bin/uv"
    [ -x "$UV" ] || { echo "install: uv did not land at $UV" >&2; exit 1; }
  fi
fi
# build-wheels.sh calls `uv` by name, so put ours first for the whole run.
export PATH="$(dirname "$UV"):$PATH"

# The wheels packaging/build-wheels.sh produces. Without them pip falls back to
# building pyaudio from source against a portaudio the user is not expected to
# have, and fetching the SpeechRecognition fork over git.
# Both of these are build artifacts, gitignored, and therefore absent from a
# fresh clone -- which is exactly how this gets installed. Build them rather
# than telling the user to run two scripts first; a working install from
# `git clone && ./packaging/install.sh` is the whole point.
shopt -s nullglob
wheels=("$REPO/packaging/vendor"/*.whl)
shopt -u nullglob
if [ ${#wheels[@]} -lt 2 ]; then
  if [ "$DRY" = 1 ]; then
    echo "    [dry] would build vendored wheels"
  else
    say "building vendored wheels (portaudio from source; a few minutes)"
    "$REPO/packaging/build-wheels.sh" >/dev/null || {
      echo "install: wheel build failed. Run ./packaging/build-wheels.sh to see why." >&2
      exit 1; }
  fi
fi

APP_BUNDLE="$REPO/tools/macos_stt/legacyspeechcli.app"
if [ ! -d "$APP_BUNDLE" ]; then
  if [ "$DRY" = 1 ]; then
    echo "    [dry] would build the transcriber bundle"
  else
    command -v swiftc >/dev/null 2>&1 || {
      echo "install: the transcriber has to be compiled and swiftc is missing." >&2
      echo "         Install the Xcode command line tools:" >&2
      echo "           xcode-select --install" >&2; exit 1; }
    say "building the transcriber bundle"
    # SIGN_IDENTITY is honoured so a release build gets a stable Developer ID
    # signature; without it the bundle is ad-hoc signed, which works but makes
    # macOS re-ask for Speech Recognition after every rebuild.
    if [ -n "${SIGN_IDENTITY:-}" ]; then
      "$REPO/packaging/build-speechcli.sh" --sign "$SIGN_IDENTITY" >/dev/null || exit 1
    else
      "$REPO/packaging/build-speechcli.sh" >/dev/null || exit 1
    fi
  fi
fi

if [ -z "$LLAMA_BIN" ]; then
  for candidate in "$REPO/packaging/vendor/llama" "$HOME/.llama-app/llama" "$HOME/.local/bin/llama"; do
    [ -x "$candidate" ] && { LLAMA_BIN="$candidate"; break; }
  done
fi
# Not fatal when absent: it is downloaded below. A machine that has never
# served models locally has no llama, and telling the user to go and find one
# is the opposite of an installer.
if [ -n "$LLAMA_BIN" ] && [ -x "$LLAMA_BIN" ]; then
  LLAMA_SOURCE="existing: $LLAMA_BIN"
else
  LLAMA_BIN=""
  LLAMA_SOURCE="download: llama.cpp $LLAMA_VERSION"
fi

# --models-max, sized from installed RAM rather than hardcoded. Two large tiers
# resident at once overflow the Metal working set on 8 GB, and that failure is
# not graceful: kIOGPUCommandBufferCallbackErrorOutOfMemory leaves the backend
# in an error state that only a restart clears.
mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
if   [ "$mem_gb" -ge 32 ]; then MODELS_MAX=4
elif [ "$mem_gb" -ge 16 ]; then MODELS_MAX=3
else                            MODELS_MAX=2
fi

# An upgrade has to stop its own services before anything else: they hold the
# very ports the check below tests, and their files are about to be replaced
# underneath them. Without this, re-running the installer fails on "port 8091 is
# already in use" and points at --llm-port, which is exactly the wrong advice.
if [ "$DRY" = 0 ]; then
  for unit in llm stt; do
    if launchctl print "gui/$(id -u)/$LABEL.$unit" >/dev/null 2>&1; then
      say "stopping $LABEL.$unit for upgrade"
      launchctl bootout "gui/$(id -u)/$LABEL.$unit" 2>/dev/null || true
    fi
  done
  sleep 2
fi

for port in "$LLM_PORT" "$STT_PORT"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "install: port $port is in use by something that is not $LABEL.*." >&2
    echo "         Pick another with --llm-port / --stt-port." >&2; exit 1
  fi
done

echo "    prefix      $PREFIX"
echo "    data        $HOME_DIR"
echo "    ports       llm $LLM_PORT, stt $STT_PORT"
echo "    label       $LABEL"
echo "    memory      ${mem_gb} GB -> --models-max $MODELS_MAX"
echo "    llama       $LLAMA_SOURCE"

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
say "installing application to $PREFIX"
run mkdir -p "$PREFIX/bin" "$PREFIX/runtime/bin"

# pyproject.toml and uv.lock are build inputs, not runtime files, and are
# deliberately not installed: their [tool.uv.sources] point at
# packaging/vendor, which is not part of the payload, so a copy in the
# prefix would only be a project that cannot be synced.
for item in mapio.py src res tools; do
  run rsync -a --delete-after "$REPO/$item" "$PREFIX/"
done
if [ -n "$LLAMA_BIN" ]; then
  # A binary already on this machine. The llama.cpp installer produces a
  # self-contained one, so copying it is enough.
  run cp "$LLAMA_BIN" "$PREFIX/runtime/bin/llama"
elif [ "$DRY" = 1 ]; then
  echo "    [dry] download llama.cpp $LLAMA_VERSION"
else
  # The release build is NOT self-contained: llama links 17 dylibs beside it,
  # so the whole directory is kept and bin/llama is a symlink into it. dyld
  # resolves the symlink before expanding @loader_path, so the libraries are
  # found.
  say "downloading llama.cpp $LLAMA_VERSION"
  tarball="llama-$LLAMA_VERSION-bin-macos-arm64.tar.gz"
  url="https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_VERSION/$tarball"
  tmp="$(mktemp -d)"
  curl -fsSL --retry 3 -o "$tmp/$tarball" "$url" || {
    echo "install: could not download $url" >&2
    echo "         Pass --llama-bin /path/to/llama to use a local build." >&2
    rm -rf "$tmp"; exit 1; }
  rm -rf "$PREFIX/runtime/llama"
  mkdir -p "$PREFIX/runtime/llama"
  tar xzf "$tmp/$tarball" -C "$PREFIX/runtime/llama" --strip-components 1
  rm -rf "$tmp"
  ln -sf ../llama/llama "$PREFIX/runtime/bin/llama"
fi

# Downloaded binaries carry the quarantine attribute, and a quarantined llama
# is killed by Gatekeeper on first exec with a dialog rather than an error.
run xattr -dr com.apple.quarantine "$PREFIX/runtime" 2>/dev/null || true

say "creating the locked environment"
# Synced from the source tree, into a venv that lives in the prefix. The
# vendored wheel paths in uv.lock resolve relative to the project root, so
# the project has to be the tree that carries packaging/vendor -- this repo
# when developing, the unpacked tarball when installing from a release.
run env UV_PROJECT_ENVIRONMENT="$PREFIX/runtime/venv" \
    "$UV" sync --frozen --project "$REPO" --no-dev
PYTHON="$PREFIX/runtime/venv/bin/python"

# The two locally built wheels, installed after the lock rather than through it.
# uv.lock records a hash per artifact and a rebuilt wheel never reproduces the
# one that was locked -- zip timestamps are enough to change it -- so locking
# these made `uv sync --frozen` fail with "Hash mismatch" on exactly the fresh
# clone this installer is for. Versions stay pinned by the filenames
# build-wheels.sh produces.
say "installing vendored wheels"
run env VIRTUAL_ENV="$PREFIX/runtime/venv" \
    "$UV" pip install --quiet "$REPO/packaging/vendor"/*.whl

# ---------------------------------------------------------------------------
# Data directory. Created if absent, never overwritten.
# ---------------------------------------------------------------------------
say "preparing data directory $HOME_DIR"
run mkdir -p "$HOME_DIR/models" "$HOME_DIR/cache/kv" "$HOME_DIR/out"

if [ ! -e "$HOME_DIR/models/new_york" ] && [ -d "$REPO/models/new_york" ]; then
  say "seeding bundled maps"
  for m in "$REPO"/models/*/; do
    name="$(basename "$m")"
    [ -f "$m/$name.json" ] || continue
    [ -e "$HOME_DIR/models/$name" ] || run cp -R "$m" "$HOME_DIR/models/$name"
  done
fi

if [ ! -f "$HOME_DIR/.env" ]; then
  say "writing $HOME_DIR/.env"
  if [ "$DRY" = 0 ]; then
    cat > "$HOME_DIR/.env" <<ENV
# MapIO configuration. Written once by install.sh; edit freely.

# Local serving. Setting LLM_BASE_URL is the single switch that selects the
# local stack and the curated prompt formatter with its L1 retrieval step.
LLM_BASE_URL="http://127.0.0.1:$LLM_PORT/v1"
LLM_MODEL="l3"
LLM_EMBED_MODEL="l1"

# On-device speech. No key, no network, no RAM taken from the llama.cpp budget.
STT_BACKEND="apple"
STT_SERVER="http://127.0.0.1:$STT_PORT"

# Only needed for street-by-street routing against Google's Routes API.
# GOOGLE_ROUTES_API_KEY=""
ENV
  fi
fi

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
say "generating scripts and launch agents"
subst() {
  sed -e "s|@APP@|$PREFIX|g" \
      -e "s|@HOME_DIR@|$HOME_DIR|g" \
      -e "s|@USER@|$(id -un)|g" \
      -e "s|@LABEL@|$LABEL|g" \
      -e "s|@LLM_PORT@|$LLM_PORT|g" \
      -e "s|@STT_PORT@|$STT_PORT|g" \
      -e "s|@PYTHON@|$PYTHON|g" \
      -e "s|@MODELS_MAX@|$MODELS_MAX|g" \
      "$1"
}

emit() {  # emit <template> <destination> [mode]
  if [ "$DRY" = 1 ]; then printf '    [dry] write %s\n' "$2"; return; fi
  subst "$1" > "$2"
  [ -n "${3:-}" ] && chmod "$3" "$2"
  return 0
}

emit "$TEMPLATES/bin/mapio-llm.in" "$PREFIX/bin/mapio-llm" 755
emit "$TEMPLATES/bin/mapio-stt.in" "$PREFIX/bin/mapio-stt" 755

# models.ini lives with the data, not the application: it is the one piece of
# configuration a user has a legitimate reason to tune per machine, and an
# application update must not overwrite their edits.
if [ ! -f "$HOME_DIR/models.ini" ]; then
  emit "$TEMPLATES/config/models.ini.in" "$HOME_DIR/models.ini"
else
  echo "    keeping existing $HOME_DIR/models.ini"
fi

run mkdir -p "$AGENTS"
emit "$TEMPLATES/launchd/llm.plist.in" "$AGENTS/$LABEL.llm.plist"
emit "$TEMPLATES/launchd/stt.plist.in" "$AGENTS/$LABEL.stt.plist"

emit "$TEMPLATES/bin/mapio.in" "$PREFIX/bin/mapio" 755

# ---------------------------------------------------------------------------
# Launch agents
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Speech Recognition grant
# ---------------------------------------------------------------------------
# TCC keys this on bundle id, so a freshly built transcriber has no grant no
# matter how long the machine has been running MapIO. Establish it HERE, from
# the terminal the user is sitting at, because the alternative is silent and
# deeply misleading: launchd's copy of the server invokes the same bundle, no
# answerable dialog appears, SFSpeechRecognizer returns nothing, and the CLI
# reports "No speech detected". The user is told their question was not
# understood, and speaking more clearly will never fix it.
PRIME_WAV="$REPO/benchmark/audio/DT-T2.wav"
if [ "$DRY" = 0 ] && [ -f "$PRIME_WAV" ]; then
  say "priming the Speech Recognition permission"
  echo "    A system dialog may appear -- answer it once and the grant persists."
  prime_out="$(mktemp)"
  open -W -n --stdout "$prime_out" --stderr /dev/null \
       "$PREFIX/tools/macos_stt/legacyspeechcli.app" --args "$PRIME_WAV" \
       2>/dev/null || true
  if [ -s "$prime_out" ]; then
    echo "    OK -- the transcriber returned a transcript."
  else
    echo "    WARNING: no transcript came back." >&2
    echo "             If the dialog was declined or never appeared, speech" >&2
    echo "             input will fail as 'No speech detected'. Re-run this" >&2
    echo "             installer, or grant Speech Recognition under" >&2
    echo "             System Settings > Privacy & Security." >&2
  fi
  rm -f "$prime_out"
fi

if [ "$BOOTSTRAP" = 1 ]; then
  say "starting services"
  for unit in llm stt; do
    launchctl bootout "gui/$(id -u)/$LABEL.$unit" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$LABEL.$unit.plist"
  done

  say "waiting for the router"
  for _ in $(seq 1 30); do
    curl -sf --max-time 2 "http://127.0.0.1:$LLM_PORT/v1/models" >/dev/null && break
    sleep 1
  done
else
  say "not starting services (--no-bootstrap)"
  echo "    launchctl bootstrap gui/\$(id -u) $AGENTS/$LABEL.llm.plist"
  echo "    launchctl bootstrap gui/\$(id -u) $AGENTS/$LABEL.stt.plist"
fi

# ---------------------------------------------------------------------------
# Model weights
# ---------------------------------------------------------------------------
# Not bundled: llama.cpp pulls them from Hugging Face on first use. Doing that
# lazily means the user's first spoken question hangs for as long as a ~5 GB
# download takes, with mapio showing only "Waiting for a response..." -- the
# warm-up thread that would report progress is itself queued behind the
# download. Pull them here, where waiting is expected and progress is visible.
if [ "$BOOTSTRAP" = 1 ] && [ "$PULL" = 1 ] && [ "$DRY" = 0 ]; then
  say "downloading model weights (~5 GB, once)"
  cache_size() { du -sm "$HOME/.cache/huggingface" 2>/dev/null | cut -f1 || echo 0; }
  before="$(cache_size)"

  for tier in l1 l3; do
    echo "    $tier ..."
    curl -s --max-time 3600 "http://127.0.0.1:$LLM_PORT/v1/chat/completions" \
      -H 'Content-Type: application/json' \
      -d "{\"model\":\"$tier\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" \
      >/dev/null 2>&1 || true
  done

  after="$(cache_size)"
  echo "    cache $HOME/.cache/huggingface is now ${after} MB (was ${before} MB)"

  ready=0
  curl -s --max-time 10 "http://127.0.0.1:$LLM_PORT/v1/models" 2>/dev/null \
    | grep -q '"l3"' && ready=1
  [ "$ready" = 1 ] || echo "    WARNING: l3 did not report in; check ~/Library/Logs/mapio-llm.log" >&2
fi

cat <<DONE

MapIO installed.

  run it            $PREFIX/bin/mapio --model new_york --prompt res/prompt_en_fixed.yaml --debug
  your data         $HOME_DIR
  logs              ~/Library/Logs/mapio-llm.log, ~/Library/Logs/mapio-stt.log

First run, in order, and each needs you at the machine:

  1. Camera and Microphone prompts, attributed to the terminal you launch from.
     Use the same terminal afterwards or they are asked again.
  2. A Speech Recognition prompt on the first spoken question. Answer once.
  3. The first question per map builds its POI index, and the first question
     overall loads the model. Both are one-time; later questions are warm.

Quit with 'q', not Ctrl-C. 'q' exits the loop, releases the microphone and
saves the chat. Ctrl-C interrupts while the process is usually blocked inside
PortAudio or OpenCV, so cleanup does not finish and the process can survive
holding the camera and microphone -- after which the next run cannot open them.

Model weights live in ~/.cache/huggingface and are shared by every install.
Re-running this installer never downloads them twice.
DONE
