# Uno Q + Ray-Ban Meta glasses: audio setup

Standalone-ish demo topology (the Uno Q's single USB-C port is in *device* mode for adb, so it
cannot host the webcam; video always comes from the laptop):

```
HUE HD Pro ─USB─► laptop ──camera_stream_server.py──► adb reverse :5000 ──► Uno Q
                                                                              │ unoq_perception_pipeline.py
                                                                              ▼
                                                              PipeWire ─► BlueZ A2DP ─► RB Meta glasses
```

Day-to-day: `.\tools\unoq_start.ps1` / `.\tools\unoq_stop.ps1` (see comments in those files).

## One-time board setup (already done on SCL-UNOQ14, 2026-09-16)

All commands run on the board (`adb shell`). `sudo` needs the `arduino` password; `adb root` is refused.

1. **Pair the glasses** (bonds persist across reboots). Phone Bluetooth **off**, glasses in the
   case with the lid open, hold the case button until the LED pulses blue, then:
   ```
   python3 /home/arduino/unoq_bt_pair.py 98:59:49:36:6F:D1 --forget
   ```
   (`tools/unoq_bt_pair.py`). It scans until the device is actually discovered before pairing —
   issuing `pair` early gives "Device not available" — and auto-confirms the passkey prompt.

2. **Give Bluetooth audio a single owner.** The lightdm greeter runs its own PipeWire as user
   `lightdm`, which grabs BlueZ first (`RegisterProfile() failed: NotPermitted` in our log).
   The board is headless here, so:
   ```
   sudo systemctl disable --now lightdm
   sudo loginctl enable-linger arduino        # PipeWire/WirePlumber start at boot for arduino
   ```

3. **Stop WirePlumber gating Bluetooth on an active seat.** A lingering session has no seat, and
   the default profile refuses to start the BlueZ monitor ("Seat state changed: lingering").
   Install `tools/unoq_config/50-bt-headless.conf` to
   `~/.config/wireplumber/wireplumber.conf.d/50-bt-headless.conf`, then
   `systemctl --user restart wireplumber`.

4. **TTS engine:** `sudo apt install -y espeak-ng`.

5. Verify: `bluetoothctl connect 98:59:49:36:6F:D1`, then
   `XDG_RUNTIME_DIR=/run/user/1000 wpctl status` should show `* RB Meta 0118` under Sinks.
   `espeak-ng -w /tmp/t.wav "hello" && XDG_RUNTIME_DIR=/run/user/1000 pw-play /tmp/t.wav` → heard in glasses.

`tools/unoq_audio_up.sh` is a rootless fallback that starts PipeWire under a private runtime dir
if the systemd user session is ever unavailable.

## Gotchas
- Any process on the board that plays audio must see `XDG_RUNTIME_DIR=/run/user/1000`
  (adb shells don't set it). The pipeline sets it itself.
- The glasses are single-host: if the phone's Bluetooth is on they reconnect to it and refuse
  the board. Turn the phone's BT off for demos.
- Power: the Uno Q wants 5 V / 3 A. On an unpowered USB-A hub shared with the webcam it
  brownout-rebooted once. Plug it straight into a laptop USB-C port when possible.
- `opencv 5.0` on aarch64: SIFT segfaults with OpenCV's thread pool alongside MediaPipe;
  the pipeline pins `cv2.setNumThreads(1)`.
- With `--remap` the BART tactile map is projected onto the New York graph, so the "nearest POI"
  is routinely hundreds of feet away; the announcer therefore speaks the nearest POI with no
  distance gate by default (`--poi-radius-ft 0`).
