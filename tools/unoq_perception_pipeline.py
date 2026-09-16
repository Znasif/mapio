"""
Uno Q On-Device Perception Pipeline & Visual Debug Streamer.
Runs on Qualcomm QRB2210 Linux MPU (Arduino Uno Q).

Pipeline:
1. Ingests video stream from PC over USB-C (port 5000 via ADB reverse).
2. Computes SIFT map homography and locks once confidence threshold is met.
3. Detects hand and index fingertip using MediaPipe.
4. Maps fingertip to tactile map coordinates (with optional temporary remap).
5. Resolves the fingertip with mapio's own graph engine (src/graph + src/position):
   POI / intersection / street with the same thresholds as the laptop app.
6. Annotates video frame with map boundary, fingertip, and telemetry HUD.
7. Serves visual debug stream and web dashboard on port 5001 over USB-C (via ADB forward).
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

import cv2
import mediapipe as mp
import numpy as np
import requests

# The board runs the laptop app's own graph engine: `src/` is pushed next to this file
# (unoq_start.ps1 does it) and is importable because sys.path[0] is the script directory.
from src.config import config
from src.graph import Graph
from src.position import PositionHandler
from src.utils import Coords

# On the Uno Q (aarch64, opencv 5.0) SIFT segfaults after a few frames when OpenCV's
# parallel backend runs alongside MediaPipe's XNNPACK threads. Single-threaded SIFT
# costs ~0.3 s more per detection, which only happens every 5 s once the map is locked.
cv2.setNumThreads(1)


class UnoQCameraReceiver:
    """Consumes MJPEG stream from PC over USB-C without buffering lag."""

    def __init__(self, stream_url: str = "http://127.0.0.1:5000/video_feed"):
        self.stream_url = stream_url
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.current_frame: Optional[np.ndarray] = None
        self.frame_id: int = 0
        self.timestamp: float = 0.0

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)

    def read(self, timeout: float = 1.0) -> Tuple[bool, Optional[np.ndarray]]:
        with self.cond:
            last_id = self.frame_id
            end = time.time() + timeout
            while self.frame_id == last_id and self.running:
                rem = end - time.time()
                if rem <= 0:
                    break
                self.cond.wait(rem)
            if self.current_frame is None:
                return False, None
            return True, self.current_frame.copy()

    def _worker(self):
        while self.running:
            try:
                resp = requests.get(self.stream_url, stream=True, timeout=5.0)
                if resp.status_code != 200:
                    time.sleep(0.5)
                    continue

                buf = b""
                for chunk in resp.iter_content(chunk_size=8192):
                    if not self.running:
                        break
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

                        jpg = buf[start : end + 2]
                        buf = buf[end + 2 :]

                        frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if frame is not None:
                            with self.cond:
                                self.current_frame = frame
                                self.frame_id += 1
                                self.timestamp = time.time()
                                self.cond.notify_all()
            except Exception:
                if self.running:
                    time.sleep(0.5)


class SpeechAnnouncer:
    """Speaks position descriptions through the board's default PipeWire sink (the glasses).

    espeak-ng renders each utterance to a WAV which pw-play sends to the default sink, so
    audio follows whatever `wpctl set-default` points at.
    """

    DETAILED_NODE_DELAY = 1.0   # seconds, same as PositionAnnouncer
    DETAILED_DELAY = 2.0

    def __init__(self, rate_wpm: int = 175):
        self.rate_wpm = rate_wpm
        self.enabled = shutil.which("espeak-ng") is not None and shutil.which("pw-play") is not None
        if not self.enabled:
            print("[Speech] espeak-ng or pw-play missing; speech disabled (sudo apt install -y espeak-ng)", flush=True)

        # pw-play needs the user's PipeWire socket; adb shells don't export XDG_RUNTIME_DIR.
        self.env = dict(os.environ)
        self.env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

        self.current: Optional[str] = None
        self.current_since = 0.0
        self.detailed_done = False
        self.last_spoken: Optional[str] = None
        self.map_announced = False
        self.lock = threading.Lock()
        self.worker: Optional[threading.Thread] = None
        self.pending: Optional[str] = None

    def say(self, text: str) -> None:
        """Queue `text`; a newer utterance replaces any not-yet-started one."""
        if not self.enabled:
            return
        with self.lock:
            self.pending = text
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self.__drain, daemon=True)
                self.worker.start()

    def __drain(self) -> None:
        while True:
            with self.lock:
                text, self.pending = self.pending, None
            if text is None:
                return
            print(f"[Speech] {text}", flush=True)
            wav = os.path.join(tempfile.gettempdir(), "unoq_say.wav")
            try:
                subprocess.run(["espeak-ng", "-s", str(self.rate_wpm), "-w", wav, text], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                subprocess.run(["pw-play", wav], check=True, env=self.env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            except Exception as e:
                print(f"[Speech] playback failed: {e}", flush=True)

    def update(self, meta: Dict) -> None:
        """Feed per-frame perception metadata; decides what (if anything) to say.

        Mirrors mapio's PositionAnnouncer: say the element's short description (street / POI /
        intersection name) when the finger lands on a new element, then its complete
        description once the finger has stayed still on it for DETAILED_DELAY seconds.
        """
        if not self.enabled:
            return
        now = time.time()

        if meta.get("map_detected") and not self.map_announced:
            self.map_announced = True
            self.say("Map detected")

        desc = meta.get("nearest_poi")          # short description, "" / None when off-graph
        if not desc or meta.get("finger_map") is None:
            self.current = None
            self.current_since = 0.0
            self.detailed_done = False
            return

        if desc != self.current:
            self.current = desc
            self.current_since = now
            self.detailed_done = False
            if desc != self.last_spoken:
                self.last_spoken = desc
                print(f"[Speech] {meta.get('element_type')} {desc!r} at {meta.get('poi_dist_feet')} ft", flush=True)
                self.say(desc)
            return

        delay = self.DETAILED_NODE_DELAY if meta.get("element_type") == "node" else self.DETAILED_DELAY
        detail = meta.get("detail") or ""
        if (not self.detailed_done and meta.get("movement") == "none"
                and now - self.current_since >= delay and detail and detail != self.last_spoken):
            self.detailed_done = True
            self.last_spoken = detail
            self.say(detail)



class MapPerception:
    """Handles SIFT homography detection and MediaPipe hand tracking."""

    def __init__(
        self,
        template_path: str,
        model_json_path: Optional[str] = None,
        remap_bart_to_ny: bool = False,
    ):
        self.template_path = template_path
        self.remap = remap_bart_to_ny

        # Load Template
        self.template = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
        if self.template is None:
            raise FileNotFoundError(f"Could not load template image: {template_path}")
        self.tpl_h, self.tpl_w = self.template.shape[:2]
        print(f"[Perception] Loaded template: {template_path} ({self.tpl_w}x{self.tpl_h})")

        # SIFT Detector
        self.sift = cv2.SIFT_create()
        self.kp_tpl, self.des_tpl = self.sift.detectAndCompute(self.template, None)
        self.flann = cv2.DescriptorMatcher_create(cv2.DescriptorMatcher_FLANNBASED)

        self.homography: Optional[np.ndarray] = None
        self.inliers = 0
        self.last_detection_time = 0.0
        self.detection_interval = 5.0  # seconds, matching mapio MapDetector
        self.inliers_thresh = 20  # minimum inliers to update homography

        # MediaPipe Hands
        self.mp_hands = mp.solutions.hands
        self.hands = self.mp_hands.Hands(
            static_image_mode=False,
            max_num_hands=2,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )

        # Graph engine: the same Graph + PositionHandler mapio.py uses on the laptop, so the
        # board resolves the fingertip to a POI / intersection / street with identical rules
        # (0.25 in / 0.15 in / 0.3 in thresholds, gravity, position averaging).
        self.position_handler = None
        if model_json_path and os.path.isfile(model_json_path):
            with open(model_json_path, "r", encoding="utf-8") as f:
                model_data = json.load(f)
            config.load_model(model_data)
            self.graph = Graph(model_data["graph"])
            self.position_handler = PositionHandler()
            print(f"[Perception] Graph loaded: {len(self.graph.nodes)} nodes, {len(self.graph.edges)} edges, "
                  f"{len(self.graph.pois)} POIs, {config.feets_per_pixel:.3f} ft/px", flush=True)
        else:
            print(f"[Perception] Warning: model not found at {model_json_path}; no position announcements", flush=True)

    def update_homography(self, frame: np.ndarray) -> bool:
        """
        Dynamically updates homography every DETECTION_INTERVAL (5s), exactly matching
        mapio's MapDetector. If the map moves, it re-tracks. If inliers drop, it
        retains the last valid homography until the next cycle.
        """
        now = time.time()
        if self.homography is not None and (now - self.last_detection_time < self.detection_interval):
            return True

        self.last_detection_time = now
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        kp_frame, des_frame = self.sift.detectAndCompute(gray, None)

        if des_frame is None or len(des_frame) < 4:
            return self.homography is not None

        try:
            matches = self.flann.knnMatch(des_frame, self.des_tpl, 2)
            good = [m for m, n in matches if m.distance < 0.75 * n.distance]
            if len(good) >= 4:
                src_pts = np.float32([kp_frame[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
                dst_pts = np.float32([self.kp_tpl[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
                H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 8.0)
                inliers = int(np.sum(mask)) if mask is not None else 0

                if inliers >= self.inliers_thresh:
                    self.homography = H
                    self.inliers = inliers
                    return True
                else:
                    # Inliers below threshold: map could be occluded by hands, keep previous H
                    pass
        except Exception:
            pass

        return self.homography is not None

    def process(self, frame: np.ndarray) -> Tuple[np.ndarray, dict]:
        """Runs perception, annotates frame, returns metadata."""
        h_frame, w_frame = frame.shape[:2]
        self.update_homography(frame)

        # 1. Draw Map Boundary if Homography is known
        annotated = frame.copy()
        if self.homography is not None:
            try:
                H_inv = np.linalg.inv(self.homography)
                corners = np.float32([
                    [0, 0],
                    [self.tpl_w, 0],
                    [self.tpl_w, self.tpl_h],
                    [0, self.tpl_h],
                ]).reshape(-1, 1, 2)
                proj = cv2.perspectiveTransform(corners, H_inv)
                pts = np.int32(proj).reshape((-1, 1, 2))
                cv2.polylines(annotated, [pts], isClosed=True, color=(0, 255, 0), thickness=2)
                for pt in pts:
                    cv2.circle(annotated, tuple(pt[0]), 5, (0, 255, 255), -1)
            except Exception:
                pass

        # 2. Hand & Fingertip Tracking
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.hands.process(rgb)

        finger_cam: Optional[Tuple[int, int]] = None
        finger_map: Optional[Tuple[float, float]] = None
        nearest_poi: Optional[str] = None
        poi_dist: float = 0.0
        element_type: Optional[str] = None
        detail: str = ""
        movement: str = "none"

        if results.multi_hand_landmarks:
            best_candidate = None

            for hand_lms in results.multi_hand_landmarks:
                tip = hand_lms.landmark[8]
                fx = int(tip.x * w_frame)
                fy = int(tip.y * h_frame)

                # Draw skeleton lines
                for connection in self.mp_hands.HAND_CONNECTIONS:
                    pt1 = hand_lms.landmark[connection[0]]
                    pt2 = hand_lms.landmark[connection[1]]
                    c1 = (int(pt1.x * w_frame), int(pt1.y * h_frame))
                    c2 = (int(pt2.x * w_frame), int(pt2.y * h_frame))
                    cv2.line(annotated, c1, c2, (255, 200, 0), 1)

                is_on_map = False
                map_coords = None

                if self.homography is not None:
                    try:
                        pt_arr = np.array([[[float(fx), float(fy)]]], dtype=np.float32)
                        map_pt = cv2.perspectiveTransform(pt_arr, self.homography)[0][0]
                        tx, ty = float(map_pt[0]), float(map_pt[1])
                        if 0 <= tx <= self.tpl_w and 0 <= ty <= self.tpl_h:
                            is_on_map = True

                        if self.remap:
                            mx = tx * (1920.0 / 932.0)
                            my = ty * (1824.0 / 1208.0)
                        else:
                            mx, my = tx, ty
                        map_coords = (round(mx, 1), round(my, 1))
                    except Exception:
                        pass

                candidate = {
                    "fx": fx,
                    "fy": fy,
                    "map_coords": map_coords,
                    "is_on_map": is_on_map,
                }

                if best_candidate is None:
                    best_candidate = candidate
                elif candidate["is_on_map"] and not best_candidate["is_on_map"]:
                    best_candidate = candidate

            if best_candidate:
                finger_cam = (best_candidate["fx"], best_candidate["fy"])
                finger_map = best_candidate["map_coords"]

                # Highlight active fingertip
                cv2.circle(annotated, finger_cam, 8, (0, 0, 255), -1)
                cv2.circle(annotated, finger_cam, 12, (0, 255, 255), 2)

                # Resolve fingertip -> graph element exactly like mapio.py's main loop
                if finger_map and self.position_handler is not None:
                    # process_position() converts template pixels -> feet itself
                    self.position_handler.process_position(Coords(*finger_map))
                    position = self.position_handler.get_position_info()
                    if position.graph_element is not None:
                        nearest_poi = position.description
                        poi_dist = round(position.distance, 1)
                        element_type = "poi" if position.is_poi() else "node" if position.is_node() else "edge"
                        detail = position.complete_description
                        movement = position.movement.name.lower()
        elif self.position_handler is not None:
            # No hand in view: drop the position buffer so a stale average can't be announced
            self.position_handler.clear()

        # 3. Render HUD Overlay
        overlay = annotated.copy()
        cv2.rectangle(overlay, (10, 10), (320, 140), (20, 20, 20), -1)
        cv2.addWeighted(overlay, 0.75, annotated, 0.25, 0, annotated)

        # Status text lines
        mode_str = "BART->NY (Remapped)" if self.remap else "New York (native)"
        has_map = self.homography is not None
        map_str = f"TRACKING ({self.inliers} inliers)" if has_map else "SEARCHING MAP..."
        map_col = (0, 255, 0) if has_map else (0, 165, 255)

        cv2.putText(annotated, "UNO Q EDGE PERCEPTION", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        cv2.putText(annotated, f"Mode: {mode_str}", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 220, 220), 1)
        cv2.putText(annotated, f"Map:  {map_str}", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.45, map_col, 1)

        if finger_map:
            cv2.putText(annotated, f"Finger: ({finger_map[0]}, {finger_map[1]})", (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
        else:
            cv2.putText(annotated, "Finger: Not Detected", (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (120, 120, 120), 1)

        if nearest_poi:
            cv2.putText(annotated, f"{(element_type or '').upper()}: {nearest_poi[:18]} ({poi_dist}ft)", (20, 115), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (100, 255, 100), 1)
        else:
            cv2.putText(annotated, "POI: --", (20, 115), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (120, 120, 120), 1)

        meta = {
            "map_detected": has_map,
            "inliers": self.inliers,
            "finger_cam": finger_cam,
            "finger_map": finger_map,
            "nearest_poi": nearest_poi,
            "poi_dist_feet": poi_dist,
            "element_type": element_type,
            "detail": detail,
            "movement": movement,
            "remap": self.remap,
        }
        return annotated, meta


class VisualDebugServer:
    """Serves annotated MJPEG stream and web dashboard on Uno Q port 5001."""

    def __init__(self, host: str = "0.0.0.0", port: int = 5001):
        self.host = host
        self.port = port
        self.latest_jpeg: Optional[bytes] = None
        self.latest_meta: dict = {}
        self.fps: float = 0.0
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.running = True

        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path == "/video_feed":
                    self.send_response(200)
                    self.send_header("Age", "0")
                    self.send_header("Cache-Control", "no-cache, private")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()

                    try:
                        while server_self.running:
                            with server_self.cond:
                                server_self.cond.wait(timeout=1.0)
                                jpeg = server_self.latest_jpeg

                            if jpeg is None:
                                continue

                            header = (
                                b"--frame\r\n"
                                b"Content-Type: image/jpeg\r\n"
                                + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                            )
                            self.wfile.write(header)
                            self.wfile.write(jpeg)
                            self.wfile.write(b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return

                elif self.path == "/status":
                    with server_self.lock:
                        data = {
                            "fps": round(server_self.fps, 1),
                            **server_self.latest_meta,
                        }
                    body = json.dumps(data).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                elif self.path == "/" or self.path == "/index.html":
                    html = """<!DOCTYPE html>
<html>
<head>
    <title>Uno Q Perception Stream</title>
    <style>
        body { font-family: sans-serif; background: #121212; color: #fff; text-align: center; margin: 0; padding: 20px; }
        h1 { color: #00d2ff; margin-bottom: 5px; }
        .sub { color: #888; font-size: 14px; margin-bottom: 20px; }
        .container { display: inline-block; background: #1e1e1e; padding: 15px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.6); }
        img { border-radius: 8px; max-width: 90vw; height: auto; border: 2px solid #333; }
        .telemetry { margin-top: 15px; display: flex; justify-content: space-around; font-size: 15px; }
        .pill { background: #2a2a2a; padding: 8px 16px; border-radius: 20px; border: 1px solid #444; }
        .highlight { color: #00ff88; font-weight: bold; }
    </style>
</head>
<body>
    <h1>Arduino Uno Q Live Perception Feed</h1>
    <div class="sub">Qualcomm QRB2210 Linux MPU &bull; USB-C Direct Stream &bull; Port 5001</div>
    <div class="container">
        <img src="/video_feed" alt="Uno Q Perception Stream" />
        <div class="telemetry">
            <div class="pill">FPS: <span id="fps" class="highlight">--</span></div>
            <div class="pill">Map Status: <span id="map" class="highlight">--</span></div>
            <div class="pill">Nearest POI: <span id="poi" class="highlight">--</span></div>
        </div>
    </div>
    <script>
        setInterval(async () => {
            try {
                const res = await fetch('/status');
                const data = await res.json();
                document.getElementById('fps').innerText = data.fps;
                document.getElementById('map').innerText = data.map_detected ? 'TRACKING (' + data.inliers + ' inliers)' : 'Searching...';
                document.getElementById('poi').innerText = data.nearest_poi ? ((data.element_type || '') + ': ' + data.nearest_poi + ' (' + data.poi_dist_feet + 'ft)') : 'None';
            } catch(e) {}
        }, 500);
    </script>
</body>
</html>"""
                    body = html.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                else:
                    self.send_response(404)
                    self.end_headers()

        class ReusableServer(ThreadingHTTPServer):
            allow_reuse_address = True

        try:
            self.server = ReusableServer((self.host, self.port), Handler)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            print(f"[DebugServer] Visual feed running at http://{self.host}:{self.port}/video_feed", flush=True)
            print(f"[DebugServer] Web dashboard at http://{self.host}:{self.port}/", flush=True)
        except OSError as e:
            if e.errno == 98 or "address already in use" in str(e).lower():
                print(f"[DebugServer] Warning: Port {self.port} is already in use by an existing instance.", flush=True)
                print(f"[DebugServer] Tip: Run 'adb shell pkill -f unoq_perception' to free port {self.port}.", flush=True)
            raise e

    def update(self, frame: np.ndarray, meta: dict, fps: float):
        ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if ok:
            with self.cond:
                self.latest_jpeg = jpeg.tobytes()
                self.latest_meta = meta
                self.fps = fps
                self.cond.notify_all()

    def stop(self):
        self.running = False
        self.server.shutdown()
        self.server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Uno Q Perception Pipeline & Debug Streamer")
    parser.add_argument("--input-url", default="http://127.0.0.1:5000/video_feed", help="Source camera stream URL")
    parser.add_argument("--port", type=int, default=5001, help="Debug server port (default: 5001)")
    parser.add_argument("--template", default="/home/arduino/models/new_york/template.png", help="Template image path for SIFT")
    parser.add_argument("--model", default="/home/arduino/models/new_york/new_york.json", help="Model JSON for topological graph")
    parser.add_argument("--remap", action="store_true", help="Remap BART template coords to New York graph bounds")
    parser.add_argument("--no-speech", action="store_true", help="Disable spoken POI announcements (espeak-ng -> default PipeWire sink)")
    args = parser.parse_args()

    print("\n========================================================")
    print("  Starting Uno Q Edge Perception Engine...")
    print(f"  Input Stream: {args.input_url}")
    print(f"  SIFT Template: {args.template}")
    print(f"  Graph Model:   {args.model}")
    print(f"  Remap Active:  {args.remap}")
    print(f"  Speech:        {'off' if args.no_speech else 'espeak-ng -> default sink'}")
    print("========================================================\n")

    receiver = None
    debug_server = None

    try:
        receiver = UnoQCameraReceiver(args.input_url)
        receiver.start()

        perception = MapPerception(
            template_path=args.template,
            model_json_path=args.model,
            remap_bart_to_ny=args.remap,
        )

        debug_server = VisualDebugServer(port=args.port)

        speech = None if args.no_speech else SpeechAnnouncer()
        if speech and speech.enabled:
            speech.say("Uno Q ready")

        fps_times = []
        fps_measured = 0.0
        frame_count = 0

        print("\n[Uno Q] Perception loop running. Waiting for frames...", flush=True)
        while True:
            ok, frame = receiver.read(timeout=1.0)
            if not ok or frame is None:
                time.sleep(0.05)
                continue

            t0 = time.time()
            annotated_frame, meta = perception.process(frame)
            now = time.time()

            fps_times.append(now)
            if len(fps_times) > 20:
                fps_times.pop(0)
            if len(fps_times) >= 2:
                dur = fps_times[-1] - fps_times[0]
                if dur > 0:
                    fps_measured = (len(fps_times) - 1) / dur

            debug_server.update(annotated_frame, meta, fps_measured)
            if speech:
                speech.update(meta)

            # Log status periodically
            frame_count += 1
            if frame_count % 15 == 0:
                poi_str = f" | POI: {meta['nearest_poi']} ({meta['poi_dist_feet']}ft)" if meta['nearest_poi'] else ""
                finger_str = f" | Finger: {meta['finger_map']}" if meta['finger_map'] else ""
                print(f"[Uno Q] FPS: {fps_measured:4.1f} | Map: {'TRACKING' if meta['map_detected'] else 'SEARCHING'}{finger_str}{poi_str}", flush=True)

    except KeyboardInterrupt:
        print("\nStopping Uno Q Perception Engine...", flush=True)
    except Exception as e:
        import traceback
        print("\n[Uno Q Error] Fatal exception in perception pipeline:", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
    finally:
        if receiver:
            receiver.stop()
        if debug_server:
            debug_server.stop()
        print("Engine stopped.", flush=True)


if __name__ == "__main__":
    main()
