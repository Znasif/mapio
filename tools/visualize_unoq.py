"""
PC Visualizer for Uno Q Live Perception Feed.
Displays the real-time annotated perception stream from the Arduino Uno Q.

Usage:
    python tools/visualize_unoq.py [--port 5001]
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np
import requests

DEFAULT_ADB_PATHS = [
    r"C:\Users\qc_de\AppData\Local\Arduino15\packages\arduino\tools\adb\32.0.0\adb.exe",
    "adb",
]


def find_adb():
    for path in DEFAULT_ADB_PATHS:
        if os.path.isabs(path) and os.path.isfile(path):
            return path
        found = shutil.which(path)
        if found:
            return found
    return None


def setup_adb_forward(port: int):
    adb = find_adb()
    if adb:
        try:
            cmd = [adb, "forward", f"tcp:{port}", f"tcp:{port}"]
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            print(f"[ADB] Forwarded port {port} -> Uno Q:{port}")
        except Exception as e:
            print(f"[ADB] Warning forwarding port {port}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Visualize Uno Q Live Perception Feed")
    parser.add_argument("--port", type=int, default=5001, help="Port forwarded from Uno Q (default: 5001)")
    parser.add_argument("--url", type=str, default=None, help="Custom stream URL")
    args = parser.parse_args()

    setup_adb_forward(args.port)

    stream_url = args.url or f"http://127.0.0.1:{args.port}/video_feed"
    web_url = f"http://127.0.0.1:{args.port}/"

    print("\n========================================================")
    print("  MapIO Uno Q Visualizer Running!")
    print(f"  Connecting to stream: {stream_url}")
    print(f"  Web browser dashboard: {web_url}")
    print("  Press 'q' or 'ESC' in the window to quit.")
    print("  Press 's' to save a debug frame screenshot.")
    print("========================================================\n")

    window_name = "Arduino Uno Q - Live Perception Feed (QRB2210)"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, 960, 720)

    try:
        resp = requests.get(stream_url, stream=True, timeout=10.0)
        if resp.status_code != 200:
            print(f"Error connecting: HTTP {resp.status_code}")
            return

        buf = b""
        fps_times = []
        fps = 0.0

        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                continue
            buf += chunk

            while True:
                start = buf.find(b"\xff\xd8")
                if start == -1:
                    if len(buf) > 65536:
                        buf = buf[-1024:]
                    break
                end = buf.find(b"\xff\xd9", start)
                if end == -1:
                    if start > 0:
                        buf = buf[start:]
                    break

                jpeg = buf[start : end + 2]
                buf = buf[end + 2 :]

                frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    now = time.time()
                    fps_times.append(now)
                    if len(fps_times) > 20:
                        fps_times.pop(0)
                    if len(fps_times) >= 2:
                        dur = fps_times[-1] - fps_times[0]
                        if dur > 0:
                            fps = (len(fps_times) - 1) / dur

                    cv2.putText(
                        frame,
                        f"PC Recv: {fps:.1f} FPS",
                        (frame.shape[1] - 160, 30),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 255, 0),
                        1,
                    )

                    cv2.imshow(window_name, frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q") or key == 27:
                        return
                    elif key == ord("s"):
                        filename = f"unoq_debug_{int(time.time())}.jpg"
                        cv2.imwrite(filename, frame)
                        print(f"Saved screenshot: {filename}")

    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Stream error: {e}")
    finally:
        cv2.destroyAllWindows()
        print("Visualizer closed.")


if __name__ == "__main__":
    main()
