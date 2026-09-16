"""
PC Camera Streaming Server for MapIO on Qualcomm Snapdragon / Uno Q.
Streams laptop camera frames over USB-C (via ADB reverse) or local network to Arduino Uno Q.

Usage:
    python tools/camera_stream_server.py [--port 5000] [--camera 0] [--width 640] [--height 480] [--fps 30] [--quality 75]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

import cv2
import numpy as np

DEFAULT_ADB_PATHS = [
    r"C:\Users\qc_de\AppData\Local\Arduino15\packages\arduino\tools\adb\32.0.0\adb.exe",
    "adb",
]


def find_adb() -> Optional[str]:
    for path in DEFAULT_ADB_PATHS:
        if os.path.isabs(path) and os.path.isfile(path):
            return path
        found = shutil.which(path)
        if found:
            return found
    return None


def setup_adb_reverse(port: int) -> bool:
    adb = find_adb()
    if not adb:
        print("[ADB] adb binary not found, skipping automatic adb reverse.")
        return False
    try:
        cmd = [adb, "reverse", f"tcp:{port}", f"tcp:{port}"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"[ADB] Reversed port {port} -> {res.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ADB] Failed to reverse port {port}: {e.stderr.strip()}")
        return False
    except Exception as e:
        print(f"[ADB] Error configuring adb reverse: {e}")
        return False


class CameraStreamer:
    """Continuously captures frames from camera in a dedicated thread to ensure zero lag."""

    def __init__(
        self,
        camera_index: int = 0,
        width: int = 640,
        height: int = 480,
        target_fps: int = 30,
        jpeg_quality: int = 75,
    ):
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.target_fps = target_fps
        self.jpeg_quality = jpeg_quality

        api_preference = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(self.camera_index, api_preference)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open camera {self.camera_index}")

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.target_fps)

        # Actual properties negotiated with driver
        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[Camera] Opened index {camera_index} ({actual_w}x{actual_h} @ target {target_fps} FPS)")

        self.running = True
        self.lock = threading.Lock()
        self.new_frame_event = threading.Condition(self.lock)

        self.latest_jpeg: Optional[bytes] = None
        self.latest_timestamp: float = 0.0
        self.frame_index: int = 0
        self.fps_measured: float = 0.0

        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

    def _capture_loop(self):
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        frame_times = []

        while self.running:
            ret, frame = self.cap.read()
            now = time.time()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            ok, jpeg = cv2.imencode(".jpg", frame, encode_params)
            if not ok:
                continue

            frame_bytes = jpeg.tobytes()

            with self.new_frame_event:
                self.latest_jpeg = frame_bytes
                self.latest_timestamp = now
                self.frame_index += 1
                self.new_frame_event.notify_all()

            frame_times.append(now)
            if len(frame_times) > 30:
                frame_times.pop(0)
            if len(frame_times) >= 2:
                elapsed = frame_times[-1] - frame_times[0]
                if elapsed > 0:
                    self.fps_measured = (len(frame_times) - 1) / elapsed

    def get_latest_frame(self) -> Tuple[Optional[bytes], float, int]:
        with self.lock:
            return self.latest_jpeg, self.latest_timestamp, self.frame_index

    def wait_for_next_frame(self, last_index: int, timeout: float = 1.0) -> Tuple[Optional[bytes], float, int]:
        with self.new_frame_event:
            end_time = time.time() + timeout
            while self.frame_index == last_index and self.running:
                remaining = end_time - time.time()
                if remaining <= 0:
                    break
                self.new_frame_event.wait(remaining)
            return self.latest_jpeg, self.latest_timestamp, self.frame_index

    def stop(self):
        self.running = False
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)
        self.cap.release()
        print("[Camera] Capture thread stopped and camera released.")


class StreamRequestHandler(BaseHTTPRequestHandler):
    streamer: CameraStreamer

    def log_message(self, format, *args):
        # Silence standard HTTP access logs to keep console clean
        pass

    def do_GET(self):
        if self.path == "/video_feed" or self.path == "/stream":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()

            last_index = -1
            try:
                while self.server.streamer.running:
                    jpeg, timestamp, frame_idx = self.server.streamer.wait_for_next_frame(last_index, timeout=1.0)
                    if jpeg is None or frame_idx == last_index:
                        continue
                    last_index = frame_idx

                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(jpeg)}\r\n".encode("ascii")
                        + f"X-Timestamp: {timestamp:.6f}\r\n".encode("ascii")
                        + f"X-Frame-Index: {frame_idx}\r\n\r\n".encode("ascii")
                    )
                    self.wfile.write(header)
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return

        elif self.path == "/snapshot":
            jpeg, timestamp, frame_idx = self.server.streamer.get_latest_frame()
            if jpeg is None:
                self.send_response(503)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.send_header("X-Timestamp", f"{timestamp:.6f}")
            self.send_header("X-Frame-Index", str(frame_idx))
            self.end_headers()
            self.wfile.write(jpeg)
            return

        elif self.path == "/status" or self.path == "/health":
            status_data = {
                "status": "ok",
                "fps": round(self.server.streamer.fps_measured, 2),
                "frame_count": self.server.streamer.frame_index,
                "latest_timestamp": self.server.streamer.latest_timestamp,
                "width": self.server.streamer.width,
                "height": self.server.streamer.height,
            }
            body = json.dumps(status_data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")


class StreamServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, streamer: CameraStreamer):
        super().__init__(server_address, RequestHandlerClass)
        self.streamer = streamer


def main():
    parser = argparse.ArgumentParser(description="MapIO PC Camera Streamer over USB-C / Network")
    parser.add_argument("--host", default="0.0.0.0", help="Host address to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    parser.add_argument("--camera", type=int, default=0, help="Camera device index (default: 0)")
    parser.add_argument("--width", type=int, default=640, help="Frame width (default: 640)")
    parser.add_argument("--height", type=int, default=480, help="Frame height (default: 480)")
    parser.add_argument("--fps", type=int, default=30, help="Target FPS (default: 30)")
    parser.add_argument("--quality", type=int, default=75, help="JPEG quality 1-100 (default: 75)")
    parser.add_argument("--no-reverse", action="store_true", help="Skip automatic adb reverse")
    args = parser.parse_args()

    if not args.no_reverse:
        setup_adb_reverse(args.port)

    streamer = CameraStreamer(
        camera_index=args.camera,
        width=args.width,
        height=args.height,
        target_fps=args.fps,
        jpeg_quality=args.quality,
    )

    server = StreamServer((args.host, args.port), StreamRequestHandler, streamer)
    print(f"\n========================================================")
    print(f"  MapIO Camera Streamer Running!")
    print(f"  Listening on: http://{args.host}:{args.port}")
    print(f"  Uno Q USB endpoint: http://127.0.0.1:{args.port}/video_feed")
    print(f"  Snapshot: http://127.0.0.1:{args.port}/snapshot")
    print(f"  Status:   http://127.0.0.1:{args.port}/status")
    print(f"========================================================\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.shutdown()
        server.server_close()
        streamer.stop()
        print("Server stopped.")


if __name__ == "__main__":
    main()
