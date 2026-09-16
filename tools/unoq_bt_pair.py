#!/usr/bin/env python3
"""Pair a Bluetooth headset-class device (e.g. Ray-Ban Meta glasses) from the Uno Q.

Drives a single interactive bluetoothctl session so the scan stays alive and the
pairing agent stays registered for the whole flow. Waits until the target MAC is
actually discovered before issuing pair/trust/connect (the failure mode in the
earlier attempt was `pair` racing ahead of discovery -> "Device ... not available").

Usage: python3 unoq_bt_pair.py 98:59:49:36:6F:D1 [--wait 240] [--forget]
"""
import argparse, os, re, select, subprocess, sys, time

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROMPT = "[bluetoothctl]>"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mac")
    ap.add_argument("--wait", type=int, default=240, help="seconds to wait for discovery")
    ap.add_argument("--forget", action="store_true", help="remove any stale bond first")
    a = ap.parse_args()
    mac = a.mac.upper()

    p = subprocess.Popen(["bluetoothctl", "--agent", "NoInputNoOutput"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, bufsize=0)
    fd = p.stdout.fileno()
    partial = ""

    def send(cmd):
        print(f"[CMD] {cmd}", flush=True)
        p.stdin.write((cmd + "\n").encode()); p.stdin.flush()

    def pump(timeout):
        """Read raw output for up to `timeout` s; return cleaned lines.
        bluetoothctl prints its prompt without a trailing newline, so readline() would hang."""
        nonlocal partial
        end = time.time() + timeout; out = []
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r: continue
            chunk = os.read(fd, 65536)
            if not chunk: break
            partial += chunk.decode("utf-8", "replace")
            pieces = re.split(r"\n|(?<=\[bluetoothctl\]> )", partial)
            partial = pieces.pop()
            for line in pieces:
                line = ANSI.sub("", line).replace(PROMPT, "").strip()
                if line:
                    print(f"  | {line}", flush=True); out.append(line)
        return out

    answered = {}
    def wait_for(pattern, timeout, fail=None):
        end = time.time() + timeout
        while time.time() < end:
            for line in pump(1):
                m = re.search(r"\[agent\] (.*?\(yes/no\)|Authorize service\S*|Accept pairing\S*)", line)
                if m and m.group(1) not in answered:
                    answered[m.group(1)] = True; send("yes")
                if re.search(pattern, line): return True
                if fail and re.search(fail, line): return False
        return False

    for c in ["power on", "default-agent", "pairable on"]:
        send(c); pump(0.5)
    if a.forget:
        send(f"remove {mac}"); pump(1)

    send("scan on")
    print(f"[..] waiting up to {a.wait}s for {mac} — glasses must be in pairing mode (pulsing blue)", flush=True)
    if not wait_for(rf"(NEW|CHG)\] Device {mac}", a.wait):
        send(f"info {mac}"); lines = pump(1)
        if any("not available" in l for l in lines):
            print("[FAIL] device never appeared in scan", flush=True); send("quit"); return 2
    print(f"[OK] {mac} discovered", flush=True)
    pump(2)  # let name/class resolve

    send(f"pair {mac}")
    if not wait_for(r"Pairing successful|Paired: yes|AlreadyExists", 45,
                    fail=r"Failed to pair|AuthenticationFailed|AuthenticationCanceled|not available|ConnectionAttemptFailed"):
        print("[FAIL] pairing", flush=True); send("quit"); return 3
    send(f"trust {mac}"); wait_for(r"trust succeeded|Trusted: yes", 5)
    send("scan off"); pump(0.5)
    send(f"connect {mac}")
    ok = wait_for(r"Connection successful|Connected: yes", 30, fail=r"Failed to connect")
    send(f"info {mac}"); pump(2)
    send("quit")
    try: p.wait(timeout=5)
    except subprocess.TimeoutExpired: p.kill()
    print("[DONE]" if ok else "[WARN] paired but connect failed (audio profiles may need PipeWire running)", flush=True)
    return 0 if ok else 4

if __name__ == "__main__":
    sys.exit(main())
