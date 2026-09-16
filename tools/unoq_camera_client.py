"""
Camera Stream Receiver and Benchmark Client for Arduino Uno Q (QRB2210).
Receives video frames streamed over USB-C (ADB reverse) or local network from PC.

Can be run standalone to benchmark FPS, latency, and bandwidth,
or imported as a module (UnoQCameraReceiver) by perception/tracking scripts.
"""

import argparse
import io
import re
import sys
import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np
import requests


class UnoQCameraReceiver:
    """
    Receives MJPEG frames from PC camera stream over USB-C / HTTP.
    Runs a background reading thread so calls to `read()` always return
    the most recent frame without accumulating network latency.
    """

    def __init__(self, stream_url: str = "http://127.0.0.1:5000/video_feed", timeout: float = 5.0):
        self.stream_url = stream_url
        self.timeout = timeout

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.frame_cond = threading.Condition(self.lock)

        self.current_frame: Optional[np.ndarray] = None
        self.current_frame_id: int = -1
        self.current_timestamp: float = 0.0
        self.current_size: int = 0

        # Stats
        self.total_frames_received: int = 0
        self.total_bytes_received: int = 0
        self.fps_measured: float = 0.0

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._stream_worker, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.thread = None

    def read(self, timeout: float = 1.0) -> Tuple[bool, Optional[np.ndarray], dict]:
        """
        Returns (success, frame_bgr, metadata_dict).
        Similar to cv2.VideoCapture.read().
        """
        with self.frame_cond:
            prev_id = self.current_frame_id
            end_time = time.time() + timeout
            while self.current_frame_id == prev_id and self.running:
                remaining = end_time - time.time()
                if remaining <= 0:
                    break
                self.frame_cond.wait(remaining)

            if self.current_frame is None:
                return False, None, {}

            meta = {
                "frame_id": self.current_frame_id,
                "timestamp": self.current_timestamp,
                "size_bytes": self.current_size,
                "fps": self.fps_measured,
            }
            return True, self.current_frame.copy(), meta

    def _stream_worker(self):
        while self.running:
            try:
                # Use stream=True to read chunked/multipart response
                resp = requests.get(self.stream_url, stream=True, timeout=self.timeout)
                if resp.status_code != 200:
                    time.sleep(1.0)
                    continue

                bytes_buffer = b""
                frame_timestamps = []

                for chunk in resp.iter_content(chunk_size=8192):
                    if not self.running:
                        break
                    if not chunk:
                        continue

                    bytes_buffer += chunk
                    self.total_bytes_received += len(chunk)

                    # Extract all complete JPEG frames from buffer
                    while True:
                        start_idx = bytes_buffer.find(b"\xff\xd8")
                        if start_idx == -1:
                            # Discard useless header bytes to avoid unbounded memory growth
                            if len(bytes_buffer) > 65536:
                                bytes_buffer = bytes_buffer[-1024:]
                            break

                        end_idx = bytes_buffer.find(b"\xff\xd9", start_idx)
                        if end_idx == -1:
                            # Incomplete frame, need more chunks
                            if start_idx > 0:
                                bytes_buffer = bytes_buffer[start_idx:]
                            break

                        # We have a full JPEG: start_idx .. end_idx + 2
                        jpeg_data = bytes_buffer[start_idx : end_idx + 2]
                        header_data = bytes_buffer[:start_idx]
                        bytes_buffer = bytes_buffer[end_idx + 2 :]

                        # Parse optional headers from header_data
                        frame_id = -1
                        ts = 0.0
                        try:
                            header_str = header_data.decode("ascii", errors="ignore")
                            id_match = re.search(r"X-Frame-Index:\s*(\d+)", header_str)
                            if id_match:
                                frame_id = int(id_match.group(1))
                            ts_match = re.search(r"X-Timestamp:\s*([\d\.]+)", header_str)
                            if ts_match:
                                ts = float(ts_match.group(1))
                        except Exception:
                            pass

                        # Decode frame
                        nparr = np.frombuffer(jpeg_data, dtype=np.uint8)
                        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

                        if frame is not None:
                            now = time.time()
                            frame_timestamps.append(now)
                            if len(frame_timestamps) > 30:
                                frame_timestamps.pop(0)
                            if len(frame_timestamps) >= 2:
                                dur = frame_timestamps[-1] - frame_timestamps[0]
                                if dur > 0:
                                    self.fps_measured = (len(frame_timestamps) - 1) / dur

                            with self.frame_cond:
                                self.current_frame = frame
                                self.current_frame_id = frame_id if frame_id != -1 else self.total_frames_received
                                self.current_timestamp = ts
                                self.current_size = len(jpeg_data)
                                self.total_frames_received += 1
                                self.frame_cond.notify_all()

            except Exception as e:
                if self.running:
                    time.sleep(1.0)


def benchmark(url: str, max_frames: int = 0, save_snapshot: Optional[str] = None):
    print(f"[Uno Q Client] Connecting to camera stream at {url}...")
    receiver = UnoQCameraReceiver(stream_url=url)
    receiver.start()

    start_time = time.time()
    last_report_time = start_time
    count = 0
    snapshot_saved = False

    try:
        while True:
            ok, frame, meta = receiver.read(timeout=2.0)
            if not ok or frame is None:
                print("[Uno Q Client] Waiting for frame...")
                time.sleep(0.1)
                continue

            count += 1

            if save_snapshot and not snapshot_saved:
                cv2.imwrite(save_snapshot, frame)
                print(f"[Uno Q Client] Saved snapshot to: {save_snapshot} ({frame.shape[1]}x{frame.shape[0]})")
                snapshot_saved = True

            now = time.time()
            if now - last_report_time >= 1.0:
                elapsed = now - start_time
                avg_fps = count / elapsed if elapsed > 0 else 0.0
                mb_received = receiver.total_bytes_received / (1024 * 1024)
                rate_mbps = (mb_received / elapsed) * 8 if elapsed > 0 else 0.0
                kb_per_frame = (meta["size_bytes"] / 1024) if meta.get("size_bytes") else 0

                print(
                    f"[{count:05d}] FPS: {receiver.fps_measured:5.1f} (Avg: {avg_fps:5.1f}) | "
                    f"Frame: {frame.shape[1]}x{frame.shape[0]} | "
                    f"Size: {kb_per_frame:5.1f} KB | "
                    f"Throughput: {rate_mbps:4.2f} Mbps | "
                    f"Total: {mb_received:5.2f} MB"
                )
                last_report_time = now

            if max_frames > 0 and count >= max_frames:
                break

    except KeyboardInterrupt:
        print("\n[Uno Q Client] Interrupted by user.")
    finally:
        receiver.stop()

    total_time = time.time() - start_time
    overall_fps = count / total_time if total_time > 0 else 0.0
    print("\n--- Benchmark Summary ---")
    print(f"Total Frames:    {count}")
    print(f"Total Duration:  {total_time:.2f} s")
    print(f"Average FPS:     {overall_fps:.2f} FPS")
    print(f"Total Data:      {receiver.total_bytes_received / (1024*1024):.2f} MB")
    print("-------------------------\n")


def main():
    parser = argparse.ArgumentParser(description="Uno Q Camera Stream Client & Benchmark")
    parser.add_argument("--url", default="http://127.0.0.1:5000/video_feed", help="MJPEG stream URL (default: http://127.0.0.1:5000/video_feed)")
    parser.add_argument("--frames", type=int, default=100, help="Number of frames to benchmark (0 for unlimited)")
    parser.add_argument("--snapshot", default="unoq_received_frame.jpg", help="Path to save a sample frame snapshot")
    args = parser.parse_args()

    benchmark(url=args.url, max_frames=args.frames, save_snapshot=args.snapshot)


if __name__ == "__main__":
    main()
