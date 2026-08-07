import base64
import json
import os
import subprocess
import tempfile
import threading as th
import urllib.error
import urllib.request
from typing import Optional, Set

import speech_recognition as sr

from src.modules_repository import Module
from google.cloud import speech  # unused, but needed for pre-loading the module

# STT_BACKEND selects the recogniser. "google" is the upstream MapIO behaviour
# (cloud, needs GOOGLE_SPEECH_CLOUD_KEY_FILE). "apple" uses Apple's on-device
# model -- no cloud, no key, and no RAM taken from the llama.cpp budget, since
# recognition happens in a system service rather than a process we host.
#
# The "apple" backend has two shapes, because mapio runs where the camera is:
#
#   STT_SERVER=http://localhost:11435   POST the WAV to tools/macos_stt/
#                                       stt_server.py on the Mac. This is the
#                                       normal setup -- mapio on the machine
#                                       with the map and camera, Mac serving
#                                       both the LLM and transcription.
#   unset                               shell out to a local binary, for when
#                                       mapio itself runs on the Mac.
#
# STT_APPLE_BIN names the local binary: the `legacyspeechcli.app` bundle or the
# plain `speechcli`. A path ending in .app switches to a LaunchServices
# invocation, the only way the legacy recogniser can hold a TCC grant.
# legacyspeechcli is the accurate one -- it is the only build where contextual
# hints actually bias POI names. See tools/macos_stt/README.md.
BACKEND = os.getenv("STT_BACKEND", "google").lower()
APPLE_BIN = os.getenv("STT_APPLE_BIN", "tools/macos_stt/legacyspeechcli.app")
STT_SERVER = os.getenv("STT_SERVER", "").rstrip("/")


class STT(Module):
    TIMEOUT = 20
    PHRASE_TIME_LIMIT = 30
    FINAL_SILENCE_DURATION = 3.0
    END_RECORDING_DELAY = 1.0
    APPLE_TIMEOUT = 60

    def __init__(self) -> None:
        super().__init__()

        self.recognizer = sr.Recognizer()
        self.recognizer.pause_threshold = STT.FINAL_SILENCE_DURATION

        self.microphone = sr.Microphone()
        self.commands: Set[str] = set()

        self.__recording_audio = False
        self.__processing_audio = False

    def is_recording(self) -> bool:
        return self.__recording_audio

    def is_processing_audio(self) -> bool:
        return self.__processing_audio

    def calibrate(self) -> None:
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source)

    def add_command(self, command: str) -> None:
        self.commands.add(command)

    def remove_command(self, command: str) -> None:
        self.commands.discard(command)

    def start_recording(self) -> Optional[sr.AudioData]:
        if self.__recording_audio:
            return None

        self.__recording_audio = True

        try:
            with self.microphone as source:
                audio = self.recognizer.listen(
                    source,
                    timeout=STT.TIMEOUT,
                    phrase_time_limit=STT.PHRASE_TIME_LIMIT,
                )
        except Exception:
            return None
        finally:
            self.__recording_audio = False

        return audio

    def end_recording(self, add_final_silence: bool = False) -> None:
        if add_final_silence:
            timer = th.Timer(STT.END_RECORDING_DELAY, self.recognizer.stop_listening)
            timer.start()
        else:
            self.recognizer.stop_listening()

    def audio_to_text(self, audio: sr.AudioData) -> Optional[str]:
        if self.__processing_audio:
            return None

        try:
            self.__processing_audio = True

            if BACKEND == "apple":
                return self.__recognize_apple(audio)
            return self.__recognize_google(audio)
        except Exception as e:
            print(f"STT error: {e}")
            return None
        finally:
            self.__processing_audio = False

    def __recognize_google(self, audio: sr.AudioData) -> Optional[str]:
        result = self.recognizer.recognize_google_cloud(
            audio,
            os.getenv("GOOGLE_SPEECH_CLOUD_KEY_FILE"),
            model="latest_short",
            preferred_phrases=list(self.commands),
        )

        return str(result).strip()

    def __recognize_apple(self, audio: sr.AudioData) -> Optional[str]:
        # 16 kHz mono 16-bit: what the on-device model wants, and it keeps the
        # payload small -- 5 s is ~160 KB, so there is no reason to stream.
        # AVAudioFile would resample anyway, but doing it here means one less
        # thing to debug on the Swift side.
        wav = audio.get_wav_data(convert_rate=16000, convert_width=2)

        if STT_SERVER:
            return self.__transcribe_remote(wav)
        return self.__transcribe_local(wav)

    def __transcribe_remote(self, wav: bytes) -> Optional[str]:
        payload = json.dumps(
            {
                "audio_b64": base64.b64encode(wav).decode(),
                "hints": sorted(self.commands),
            }
        ).encode()

        request = urllib.request.Request(
            f"{STT_SERVER}/transcribe",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=STT.APPLE_TIMEOUT) as resp:
                return (json.load(resp).get("text") or "").strip() or None
        except urllib.error.HTTPError as e:
            try:
                detail = json.load(e).get("error", "")
            except Exception:
                detail = e.reason
            print(f"STT error (apple/remote): {detail}")
            return None
        except Exception as e:
            print(f"STT error (apple/remote): {e}")
            return None

    def __transcribe_local(self, wav: bytes) -> Optional[str]:
        args = []
        # Hints only bias SFSpeechRecognizer. SpeechTranscriber accepts them
        # through AnalysisContext.contextualStrings and then ignores them --
        # measured byte-identical output with and without -- so passing them to
        # the plain binary would only make its logs misleading.
        if self.commands and APPLE_BIN.endswith(".app"):
            args += ["--hints", ",".join(sorted(self.commands))]

        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(wav)
            f.flush()

            if APPLE_BIN.endswith(".app"):
                # An .app cannot be exec'd directly, and its bare binary aborts
                # with a TCC privacy violation rather than a catchable error.
                # Going through LaunchServices makes the app its own responsible
                # process, so it carries its own Speech Recognition grant.
                # -W waits for exit; stdout/stderr have to be redirected to
                # files because the app is not our child.
                out = tempfile.NamedTemporaryFile(suffix=".out", delete=False)
                err = tempfile.NamedTemporaryFile(suffix=".err", delete=False)
                out.close()
                err.close()

                cmd = [
                    "open", "-W", "-n",
                    "--stdout", out.name,
                    "--stderr", err.name,
                    APPLE_BIN, "--args", f.name,
                ] + args
            else:
                out = err = None
                cmd = [APPLE_BIN, f.name] + args

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=STT.APPLE_TIMEOUT,
            )

        if out is not None:
            # `open -W` does not reliably propagate the app's exit status, so
            # treat a non-empty transcript as the success signal.
            try:
                with open(out.name) as fh:
                    stdout = fh.read()
                with open(err.name) as fh:
                    stderr = fh.read()
            finally:
                for path in (out.name, err.name):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        else:
            stdout, stderr = proc.stdout, proc.stderr

        transcript = stdout.strip()
        if not transcript:
            print(f"STT error (apple): {stderr.strip() or 'no transcript'}")
            return None

        return transcript
