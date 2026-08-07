Done. The Mac serves both models; mapio stays where the camera is.

  tools/macos_stt/stt_server.py (new)

  Stdlib only — no new dependency on the Mac. GET /health, POST /transcribe with {"audio_b64": ..., "hints": [...]} → {"text": ...}. Single-threaded deliberately: one person speaks at a time, and serialising keeps two
  recognitions from competing for the on-device model.

  It carries the same .app-vs-plain-binary logic, so open -W -n and the exit-status workaround live server-side now.

  src/view/audio/stt.py

  __recognize_apple splits on STT_SERVER:

  - set → __transcribe_remote, POSTs the WAV via urllib (same stdlib approach place_retrieval.py already uses — no requests added to requirements)
  - unset → __transcribe_local, the subprocess path, kept for if mapio ever runs on the Mac

  STT_BACKEND still defaults to google.

  Verified from here

  Ran the server against a stub binary:

  /health          -> {"ok": true, "bin": "..."}
  /nope            -> 404
  malformed body   -> 400 bad request: Expecting value...
  transcribe       -> 200, server logged "160044 bytes" (exact WAV size)
  payload          -> 156 KB wav -> 208 KB json

  Command construction, asserted directly:

  /x/legacyspeechcli.app -> open -W -n ... --args <wav> --hints Cafe China,Solle Spa
  /x/speechcli           -> /x/speechcli <wav>          (no hints)

  That second row is worth knowing: the stub run initially looked like hints were being dropped, and they were — correctly. Hints are gated on .app because that's the only build where they measurably bias anything.

  Not verified: the open invocation and TCC. Those need the Mac.

  This resolves your responsible-process worry

  You flagged it as the one thing that could block integration. The topology sidesteps it: stt_server.py is started by hand from a Terminal on the Mac, so the Speech Recognition grant is established there, once, on first
  request. mapio never touches TCC — it only sees an HTTP endpoint. The process tree question stops mattering.

  To bring up 

  # Mac
  cd tools/macos_stt && python3 stt_server.py --bin ./legacyspeechcli.app --port 11435

  # client
  ssh -N -L 11435:localhost:11435 <mac>
  curl localhost:11435/health
  export STT_BACKEND=apple STT_SERVER=http://localhost:11435

  Two things to watch on first run: the Speech Recognition dialog blocks the request until answered, and pyaudio/portaudio needs to work under Windows-native Python for sr.Microphone — that's the piece of this that's had no
  testing at all, on either machine.

  Topology and STT config saved to memory.

✻ Crunched for 3m 6s

❯ currently i am exposing only 11434, ssh -L 11434:127.0.0.1:8081 nasifzaman@172.20.220.188 can i do two at once?                                                                                                                

● Yes — -L repeats. One connection, two forwards:
  
  ssh -N \
    -L 11434:127.0.0.1:8081 \
    -L 11435:127.0.0.1:11435 \
    nasifzaman@172.20.220.188

  -N means "no remote command" — you're only forwarding, so it won't open a shell. Drop it if you want a shell in the same session (handy for starting stt_server.py), but then the tunnels die when that shell exits.

  Two flags worth adding:

  ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -L 11434:127.0.0.1:8081 \
    -L 11435:127.0.0.1:11435 \
    nasifzaman@172.20.220.188

  ExitOnForwardFailure makes it fail loudly if a local port is already taken, instead of silently connecting with one forward missing — which would look exactly like "the STT server is broken." ServerAliveInterval keeps it
  from dying quietly on an idle Wi-Fi link mid-session.

  The catch: which OS runs the tunnel

  You just decided mapio runs Windows-native, not WSL. So the tunnel has to be on Windows too — a tunnel started in WSL puts the listener on WSL's localhost, and while WSL2's localhostForwarding usually bridges that back,
  it's an extra failure mode you don't need.

  Windows has OpenSSH built in, so the same command works from PowerShell:

  ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 `
    -L 11434:127.0.0.1:8081 `
    -L 11435:127.0.0.1:11435 `
    nasifzaman@172.20.220.188

  Keep your WSL tunnel as-is for benchmark runs — run_parity_benchmark.py is headless and doesn't care. They're separate concerns, and both can be up at once as long as they're not both binding 11434 on the same host.

  Worth doing once

  You have no ~/.ssh/config. Adding one turns all of the above into ssh -N mac:

  Host mac
      HostName 172.20.220.188
      User nasifzaman
      LocalForward 11434 127.0.0.1:8081
      LocalForward 11435 127.0.0.1:11435
      ServerAliveInterval 30
      ExitOnForwardFailure yes

  Want me to write that to ~/.ssh/config on the WSL side? You'd need the Windows equivalent at C:\Users\<you>\.ssh\config separately — I can write that too if you tell me the path, since I can reach the Windows drive.

  One note on the remote side: 11435:127.0.0.1:11435 assumes you start the server with its default --port 11435 bound to 127.0.0.1. That's the default in stt_server.py, so no flags needed — but it does mean the Mac's port
  must be free. If 11435 is taken there, change both the --port and the right-hand side of the forward, not just one.
