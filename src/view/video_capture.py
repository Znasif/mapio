# type: ignore
import os
import subprocess
import sys
from typing import List, Optional, Tuple

import cv2
import numpy as np
import numpy.typing as npt

from src.config import config

# On Windows OpenCV defaults to Media Foundation, which numbers devices in a
# different order than DirectShow -- and DirectShow is what pygrabber reads to
# get the names. Measured on a three-camera machine: OpenCV/MSMF gave
# 0=Anker 1=Camo 2=HUE while DirectShow gave 0=Anker 1=HUE 2=Camo, so devices 1
# and 2 carried each other's labels and selecting by name opened the wrong one.
# Forcing DirectShow makes the enumeration OpenCV uses the same one the names
# come from. Everywhere a device is opened must pass this, or the two disagree
# again.
CAPTURE_API = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY


class VideoCapture:
    HEIGHT = 1080
    WIDTH = 1920

    def __init__(self, capture_index: int) -> None:
        self.capture_index = capture_index
        self.__capture = cv2.VideoCapture(self.capture_index, CAPTURE_API)

        self.__capture.set(cv2.CAP_PROP_FRAME_HEIGHT, VideoCapture.HEIGHT)
        self.__capture.set(cv2.CAP_PROP_FRAME_WIDTH, VideoCapture.WIDTH)
        self.__capture.set(cv2.CAP_PROP_FOCUS, 0)

    def is_opened(self) -> bool:
        return self.__capture.isOpened()

    def read(self) -> Optional[npt.NDArray[np.uint8]]:
        ok, frame = self.__capture.read()
        return frame if ok else None

    def stop(self) -> None:
        self.__capture.release()

    @staticmethod
    def get_capture() -> Optional[cv2.VideoCapture]:
        cam_port = select_camera_port()
        if cam_port is None:
            return None

        return VideoCapture(cam_port)


def select_camera_port() -> Optional[int]:
    ports = get_working_camera_ports()

    if config.camera is not None:
        return resolve_camera(config.camera, ports)

    if len(ports) == 0:
        return None

    if len(ports) == 1:
        return int(ports[0][0])

    # One number, not two. This menu used to print a 1-based position beside a
    # 0-based device index -- "1) Camera 0" -- so typing 1 selected device 0.
    # The number shown here is the device index and the value --camera takes.
    print("\nAvailable cameras:")
    for port, h, w, name in ports:
        print(f"  {port}: {name or '(unnamed)':<28} {int(w)} x {int(h)}")

    if not any(name for *_, name in ports):
        # No index -> name mapping we trust, so the OS list is printed apart
        # from the numbered one: a wrong label is worse than no label.
        detected = system_camera_names()
        if detected:
            print("\nCameras this system reports (order may not match the "
                  "numbers above):")
            for name in detected:
                print(f"   - {name}")
            print("\nFor names beside the right number: uv pip install pygrabber")

    valid = {str(port) for port, *_ in ports}
    while True:
        answer = input(f"Enter the camera number [{', '.join(sorted(valid))}]: ").strip()
        if answer in valid:
            break
        print(f"Invalid selection. Enter one of: {', '.join(sorted(valid))}.")

    chosen = int(answer)
    name = next((n for p, _, _, n in ports if p == chosen), "")
    hint = f'--camera "{name}"' if name else f"--camera {chosen}"
    print(f"Skip this next time with: {hint}")
    return chosen


def resolve_camera(
    requested: str, ports: List[Tuple[int, int, int, str]]
) -> Optional[int]:
    """Accept --camera as either a device index or part of a camera name.

    Indices are not stable: virtual cameras appear and disappear depending on
    whether their app is running, which renumbers everything after them. A name
    survives that, so --camera "HUE" keeps working when --camera 1 quietly
    starts opening something else.
    """

    if requested.isnumeric():
        return int(requested)

    matches = [
        (port, name)
        for port, _, _, name in ports
        if name and requested.lower() in name.lower()
    ]

    if len(matches) == 1:
        port, name = matches[0]
        print(f"Using camera {port} ({name}), matched on {requested!r}.")
        return port

    if not matches:
        print(f"\nNo camera matches {requested!r}. Available:")
    else:
        print(f"\n{requested!r} is ambiguous, it matches several cameras:")
    for port, _, _, name in ports:
        print(f"  {port}: {name or '(unnamed)'}")
    return None


def indexed_camera_names() -> List[str]:
    """Camera names in the same order OpenCV numbers its devices, or [].

    Only sources whose ordering actually matches are used. On Windows that is
    DirectShow, which is what OpenCV enumerates, so pygrabber's list lines up
    index for index; on Linux the /sys/class/video4linux/videoN nodes are
    numbered by the same N that cv2.VideoCapture takes. Anything less certain
    belongs in system_camera_names(), which promises no mapping.
    """

    if sys.platform == "win32":
        try:
            from pygrabber.dshow_graph import FilterGraph  # optional
        except ImportError:
            return []
        try:
            return list(FilterGraph().get_input_devices())
        except Exception:
            return []

    if sys.platform.startswith("linux"):
        names: List[str] = []
        index = 0
        while True:
            path = f"/sys/class/video4linux/video{index}/name"
            if not os.path.exists(path):
                break
            try:
                with open(path) as f:
                    names.append(f.read().strip())
            except OSError:
                names.append("")
            index += 1
        return names

    return []


def system_camera_names() -> List[str]:
    """Camera names the OS knows about, in no particular order."""

    if sys.platform != "win32":
        return []

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PnPEntity | "
             "Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' } | "
             "Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []

    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def get_working_camera_ports(
    max_non_working: int = 3,
) -> List[Tuple[int, int, int, str]]:
    names = indexed_camera_names()

    non_working = 0
    working_ports = list()

    dev_port = 0
    while non_working < max_non_working:
        camera = cv2.VideoCapture(dev_port, CAPTURE_API)

        if not camera.isOpened():
            non_working += 1
        else:
            is_reading, _ = camera.read()
            w = camera.get(3)
            h = camera.get(4)

            if is_reading:
                name = names[dev_port] if dev_port < len(names) else ""
                working_ports.append((dev_port, h, w, name))

        dev_port += 1

    return working_ports
