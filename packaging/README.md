# Packaging MapIO for macOS

The serving stack — the llama.cpp router, the on-device transcription server and
their launchd units — was developed directly in `~/.local/bin` and
`~/.config/abtc` on one machine, hardcoded to one username and one pair of
ports. This directory is that stack, parameterised, so it can be installed on
someone else's Mac.

## Layout

```
packaging/
  build-wheels.sh          pyaudio (portaudio bundled) + the SpeechRecognition fork
  vendor/                  their output; gitignored, rebuild with the script
  templates/
    bin/mapio-llm.in       llama serve, router mode          -> $APP/bin/
    bin/mapio-stt.in       stt_server.py + legacyspeechcli   -> $APP/bin/
    config/models.ini.in   l3 + l1 tier presets              -> $MAPIO_HOME/
    launchd/llm.plist.in   LaunchAgent for the router        -> ~/Library/LaunchAgents/
    launchd/stt.plist.in   LaunchAgent for transcription     -> ~/Library/LaunchAgents/
```

`.in` files carry `@TOKEN@` placeholders. `install.sh` substitutes them and
writes the result; nothing is edited by hand after install.

| token | meaning | default |
|---|---|---|
| `@APP@` | install directory: `mapio.py`, `src/`, `res/`, `bin/`, `runtime/` | where the tarball was unpacked |
| `@HOME_DIR@` | `$MAPIO_HOME` — models, `.env`, cache, chat logs | `~/Library/Application Support/MapIO` |
| `@USER@` | the installing user | `id -un` |
| `@LABEL@` | launchd label prefix | `local.mapio` |
| `@LLM_PORT@` | router port | `8091` |
| `@STT_PORT@` | transcription port | `11445` |
| `@PYTHON@` | interpreter from the locked venv | `@APP@/runtime/venv/bin/python` |
| `@MODELS_MAX@` | tiers resident at once | from `sysctl hw.memsize` |

## Why the defaults differ from the development machine

The dev stack uses labels `local.abtc.*` on ports **8081** (LLM) and **11435**
(STT). The package deliberately uses `local.mapio.*` on **8091** and **11445**
so that installing it on the development machine does not collide with the
running units — same reasoning as `models-l4.ini` getting its own port during
the LFM2.5 evaluation. Override with `--llm-port` / `--stt-port`.

## LaunchAgents, not a LaunchDaemon

The dev machine runs the LLM as a system LaunchDaemon. The package uses a
LaunchAgent for both units, because:

- the daemon's headline benefit does not apply. With FileVault on, `/Users` is
  encrypted until someone authenticates, so a daemon needing `$HOME` for its
  preset and model cache fails at boot and retries until login anyway.
- an agent needs no `sudo`, which matters a great deal for a download-and-run
  package.

The STT unit **must** be an agent regardless: it launches `legacyspeechcli.app`
through LaunchServices (`open -n`), which needs a GUI session, and the Speech
Recognition TCC grant is per-user and established by answering a dialog.

## Things learned the hard way

- **`slot-save-path` must exist before the tier loads.** A missing directory
  fails with a message about the directory, not the model. `mapio-llm` creates
  it.
- **Two large tiers overflow the Metal budget** and the backend does not
  recover — the router has to be restarted. `@MODELS_MAX@` is derived from
  installed RAM rather than hardcoded.
- **Do not set `jinja`.** Measured on LFM2.5, enabling it caused 48 consecutive
  identical tool calls on the navigation turn. See
  `../benchmark/results/lfm2.5-evaluation.md`.
- **`ctx-size` belongs to the formatter that ships**, not the largest case ever
  benchmarked. The curated formatter tops out near 7.9K.

## What the installer does not provide

**The `llama` binary.** `install.sh` looks for it at `packaging/vendor/llama`,
`~/.llama-app/llama` and `~/.local/bin/llama`, and stops with instructions if
none is found. On a machine that has never served models locally, none will be
— so either place a build at `packaging/vendor/llama` before installing, or
pass `--llama-bin /path/to/llama`. The model weights themselves are pulled by
llama.cpp on first use (~5 GB) and cached.

**Build artifacts.** `packaging/vendor/*.whl` and
`tools/macos_stt/legacyspeechcli.app` are gitignored, so a fresh clone has
neither. `install.sh` builds both — the wheels need network, the bundle needs
`swiftc` from the Xcode command line tools.

## Signing

Signing only matters for **Gatekeeper**, and Gatekeeper only applies to files
that arrive with the quarantine attribute — that is, downloaded through a
browser. Installing from a git clone builds the bundle locally with `swiftc`,
and locally built binaries are never quarantined, so an ad-hoc signature is
enough for that path: no "developer cannot be verified" dialog, no `xattr`,
no notarisation.

A Developer ID becomes necessary the day this ships as a downloadable archive
rather than a repository. Its lesser benefit, which applies either way: TCC
keys the Speech Recognition grant to bundle id *and* code hash, so an ad-hoc
signature means a rebuilt bundle is prompted for again, while a Developer ID
identity is stable across rebuilds. In practice `install.sh` only rebuilds the
bundle when it is missing, so upgrades keep the grant.

To sign when a certificate is available:

    SIGN_IDENTITY="Developer ID Application: NAME (TEAMID)" ./packaging/install.sh

## Signing details

`legacyspeechcli.app` ships prebuilt. Its `CFBundleIdentifier` is fixed from
now on: TCC keys the Speech Recognition grant on bundle id *and* signature, so
a changed identity re-prompts every existing user. Developer ID signing and
notarisation are the last step before release; until then the bundle is
ad-hoc signed and first launch needs the Gatekeeper right-click.
