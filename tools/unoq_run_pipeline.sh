#!/bin/bash
# Board-side helper (lives at /home/arduino/unoq_run_pipeline.sh on the Uno Q).
#   start [pipeline args...]  restart the perception pipeline in the background
#   stop                      stop it
#   log                       show recent pipeline log
# Speech goes to the default PipeWire sink (the Ray-Ban Meta glasses).
export XDG_RUNTIME_DIR=/run/user/1000
PY=/home/arduino/mapio_venv/bin/python
LOG=/home/arduino/pipeline.log

stop() { for p in $(pgrep -u arduino -f "mapio_venv/bin/python"); do kill "$p" 2>/dev/null; done; }

case "${1:-start}" in
  stop) stop; echo "[board] pipeline stopped" ;;
  log)  grep -vE '^\s*$|WARNING|absl|TensorFlow|inference_feedback|XNNPACK|W0000|I0000|SymbolDatabase|warnings.warn' "$LOG" | tail -${2:-20} ;;
  start)
    shift; stop; sleep 1
    bluetoothctl connect 98:59:49:36:6F:D1 >/dev/null 2>&1   # no-op if already connected
    # Extra args pass through, e.g. `start --remap` when the BART tactile map is under the camera.
    nohup "$PY" -u /home/arduino/unoq_perception_pipeline.py "$@" > "$LOG" 2>&1 &
    pid=$!; sleep 4
    if kill -0 "$pid" 2>/dev/null; then echo "[board] pipeline pid $pid"; else echo "[board] PIPELINE FAILED TO START:"; tail -8 "$LOG"; exit 1; fi
    ;;
esac
