#!/usr/bin/env python3
"""Transcription server -- runs on the Mac, next to the llama.cpp router.

mapio needs a camera, a microphone and a display, so it runs on the machine
with the physical map. The Mac stays a pure server: llama.cpp on one port,
this on another. Both reached over the same SSH tunnel.

Nothing is streamed. The client records an utterance locally and POSTs the
whole WAV -- 5 seconds of 16 kHz mono 16-bit is ~160 KB, a few milliseconds on
a LAN against ~0.5 s of recognition.

    python3 stt_server.py --bin ./legacyspeechcli.app --port 11435

Then from the client machine, alongside the existing LLM tunnel:

    ssh -N -L 11435:localhost:11435 <mac>

Endpoints:
    GET  /health      -> {"ok": true, "bin": "..."}
    POST /transcribe  -> {"text": "..."} | {"error": "..."}
        body: {"audio_b64": "<wav bytes>", "hints": ["Cafe China", ...]}

Single-threaded on purpose: one person speaks at a time, and serialising
keeps two recognition processes from competing for the on-device model.
"""

import argparse
import base64
import json
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_BODY = 32 * 1024 * 1024  # 30 s of 16 kHz mono is ~1 MB; this is slack


class Transcriber:
    def __init__(self, binary: str, timeout: int) -> None:
        self.binary = binary
        self.timeout = timeout
        self.is_app = binary.endswith(".app")

    def run(self, wav: bytes, hints):
        """Returns (text, error). Exactly one is non-None."""
        args = ["--hints", ",".join(hints)] if (hints and self.is_app) else []
        out = err = None

        try:
            with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                f.write(wav)
                f.flush()

                if self.is_app:
                    # An .app cannot be exec'd, and its bare binary aborts with a
                    # TCC privacy violation. Going through LaunchServices makes the
                    # bundle its own responsible process, so it holds its own Speech
                    # Recognition grant -- the same reason `open -n` works by hand.
                    out = tempfile.NamedTemporaryFile(suffix=".out", delete=False)
                    err = tempfile.NamedTemporaryFile(suffix=".err", delete=False)
                    out.close()
                    err.close()
                    cmd = [
                        "open", "-W", "-n",
                        "--stdout", out.name,
                        "--stderr", err.name,
                        self.binary, "--args", f.name,
                    ] + args
                else:
                    cmd = [self.binary, f.name] + args

                try:
                    proc = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=self.timeout
                    )
                except subprocess.TimeoutExpired:
                    return None, f"timed out after {self.timeout}s"

            if out is not None:
                with open(out.name) as fh:
                    stdout = fh.read()
                with open(err.name) as fh:
                    stderr = fh.read()
            else:
                stdout, stderr = proc.stdout, proc.stderr

            text = stdout.strip()
            if not text:
                # `open -W` does not reliably propagate the app's exit status, so a
                # non-empty transcript is the success signal. When there is none,
                # the reason is on the app's stderr (the redirect file) *or* on
                # `open`'s own -- a missing or unopenable bundle reports only
                # through the latter. Report both, or the most likely deployment
                # mistake is indistinguishable from a failed recognition.
                parts = [stderr.strip()]
                if out is not None:
                    parts.append(proc.stderr.strip())
                detail = "; ".join(p for p in dict.fromkeys(parts) if p)
                return None, detail or f"no transcript (exit {proc.returncode})"
            return text, None
        finally:
            # delete=False above, so these outlive the launched process; a
            # timeout returns early and would otherwise leak two files per call.
            for handle in (out, err):
                if handle is not None:
                    try:
                        os.unlink(handle.name)
                    except OSError:
                        pass


def make_handler(transcriber: Transcriber):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __reply(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/health":
                return self.__reply(404, {"error": "not found"})
            self.__reply(200, {"ok": True, "bin": transcriber.binary})

        def do_POST(self) -> None:
            if self.path != "/transcribe":
                return self.__reply(404, {"error": "not found"})

            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self.__reply(413, {"error": f"bad body length {length}"})

            try:
                payload = json.loads(self.rfile.read(length))
                wav = base64.b64decode(payload["audio_b64"])
                hints = payload.get("hints") or []
            except Exception as e:
                return self.__reply(400, {"error": f"bad request: {e}"})

            text, error = transcriber.run(wav, hints)
            if error:
                print(f"[stt] error: {error}", flush=True)
                return self.__reply(500, {"error": error})

            print(f"[stt] {len(wav)} bytes -> {text!r}", flush=True)
            self.__reply(200, {"text": text})

        def log_message(self, *_args) -> None:
            pass  # the two prints above are the log

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bin",
        default="./legacyspeechcli.app",
        help="legacyspeechcli.app (accurate, supports hints) or speechcli",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    binary = os.path.abspath(args.bin)
    if not os.path.exists(binary):
        raise SystemExit(f"no such binary: {binary}")

    transcriber = Transcriber(binary, args.timeout)
    server = HTTPServer((args.host, args.port), make_handler(transcriber))

    print(f"[stt] {binary}")
    print(f"[stt] listening on http://{args.host}:{args.port}", flush=True)
    if transcriber.is_app:
        print("[stt] first request may raise a Speech Recognition dialog; "
              "answer it once", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
