# MapIO (Qualcomm Snapdragon X Elite Edition)

MapIO is an accessible audio-tactile map system. This branch (`qcom`) is configured for **Windows on ARM64** running on Qualcomm Snapdragon X Elite laptops with on-device LLM inference via **Qualcomm AI Hub GenieX** and native Windows speech and audio.

---

## Environment Setup

### 1. Install `uv` & Python 3.11
MapIO uses `uv` for fast package resolution and virtual environment management on Windows ARM64.

In PowerShell:
```powershell
# Create virtual environment with Python 3.11
uv venv --python 3.11

# Install dependencies
uv pip install -r requirements.txt
```

---

## On-Device LLM Setup (Qualcomm AI Hub GenieX)

MapIO runs locally on the Snapdragon X Elite using **GenieX** to execute quantized Gemma models on the Hexagon NPU and Adreno GPU.

### 1. Pull the Model
```powershell
geniex pull google/gemma-4-E2B-it-qat-q4_0-gguf
```

### 2. Launch the GenieX Server
Run the local OpenAI-compatible server with extended context for map reasoning:
```powershell
geniex serve --nctx 24576
```
The server will listen at `http://127.0.0.1:18181/v1`.

---

## Configuration (`.env`)

Create or edit your `.env` file in the project root:

```env
# Local GenieX LLM
LLM_BASE_URL="http://127.0.0.1:18181/v1"
LLM_MODEL="google/gemma-4-E2B-it-qat-q4_0-gguf:Q4_0"
OPENAI_API_KEY="local"

# Full-graph prompt mode (Option B: runs without external embedding server)
MAPIO_DISABLE_RETRIEVAL="1"
LLM_CTX_SIZE="32768"

# Speech-to-Text: "google_free" (zero-config web speech), "whisper" (local), or "google" (cloud service account)
STT_BACKEND="google_free"
```

---

## Running MapIO

With your camera connected and pointing down at the map:

```powershell
# Run with the New York map
.venv\Scripts\python.exe mapio.py --model new_york --debug

# Or specify a custom camera and microphone
.venv\Scripts\python.exe mapio.py --model new_york --camera 0 --microphone 1 --debug
```

For all command-line options:
```powershell
.venv\Scripts\python.exe mapio.py --help
```

---

## Speech & Audio on Windows

- **Text-to-Speech (TTS)**: Uses Windows **SAPI5** through `pyttsx3`. It operates completely offline with zero latency.
- **Audio Feedback**: Uses `pygame.mixer` with Windows DirectSound/WASAPI to generate spatial panning and navigation earcons.
- **Speech-to-Text (STT)**: 
  - `STT_BACKEND="google_free"`: Uses Chromium Web Speech API (free, requires no Google Cloud credentials).
  - `STT_BACKEND="whisper"`: Runs local offline Whisper transcription.
  - `STT_BACKEND="server"`: Posts audio to a local HTTP STT endpoint (`STT_SERVER="http://localhost:11435"`).

---

## Keyboard Shortcuts

- `q`: Quit the application
- `Space`: Start/Stop LLM question recording
- `Enter`: Stop TTS speech
- `Escape`: Pause/Resume TTS speech
- `n`: Cancel navigation mode
- `d`: Play map description
- `m`: Fix map model detection / homography

---

## Model Creation

A software utility for creating map models is available at:
[MapIO Model Creation Utility](https://github.com/Matteo-3033/MapIO-model-creation-utility)
