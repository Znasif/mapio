import argparse
from enum import Enum
from typing import Any, Dict, Optional

from . import paths


class Lang(Enum):
    """
    Supported languages.
    """

    EN = "en"
    IT = "it"


class Config:
    """
    Configuration singleton class for the application.
    It can be accessed from any module bysimply importing it.
    """

    __instance = None

    def __new__(cls) -> "Config":
        if cls.__instance is None:
            cls.__instance = super().__new__(cls)
        return cls.__instance

    def __init__(self) -> None:
        """
        Initialize configuration attributes with default values.
        """

        self.name = "MapIO"
        "Name of the application."

        self.debug: bool = False
        "Enable a debug window with additional information on the map and finger tracking. Defaults to False."

        self.lang: Lang = Lang.EN.value
        "Language used for the speech recognition and synthesis. Defaults to English."

        self.llm_enabled: bool = True
        "Enable the llm module for questions and answers. Defaults to True."
        self.stt_enabled: bool = True
        "Enable the stt module for speech-to-text. If disabled, the text input is used. Defaults to True"
        self.tts_rate: int = 200
        "Rate of the text-to-speech voice. Defaults to 200."

        self.feets_per_pixel: float = 1.0
        "Conversion factor from pixels to feet. Defaults to 1.0."
        self.feets_per_inch: float = 1.0
        "Conversion factor from feet to inches. Defaults to 1.0."
        self.template_path: str = ""
        "Path to the template image used for the map."

        self.temperature: float = 0.0
        "Temperature of the LLM module. Defaults to 0.0."

        self.prompt_file: str = ""
        "Path to the prompt yaml. Empty means res/prompt_<lang>.yaml."

        self.camera: Optional[str] = None
        "Camera device number or name fragment. None means ask."

        self.microphone: Optional[str] = None
        "Microphone device number or name fragment. None means system default."

    @property
    def inches_per_feet(self) -> float:
        """
        Conversion factor from feet to inches.
        """
        return 1 / self.feets_per_inch

    @property
    def pixels_per_feet(self) -> float:
        """
        Conversion factor from feet to pixels.
        """
        return 1 / self.feets_per_pixel

    def load_args(self, args: argparse.Namespace) -> None:
        """
        Load configuration attributes from the command line arguments.
        """
        self.debug = args.debug
        self.llm_enabled = not args.no_llm
        self.stt_enabled = not args.no_stt
        self.lang = args.lang.value
        self.tts_rate = args.tts_rate
        # Resolved against the app directory when it is not found as given, so
        # --prompt res/prompt_en_fixed.yaml works from any working directory.
        self.prompt_file = paths.resolve_resource(args.prompt) if args.prompt else ""
        self.camera = args.camera
        self.microphone = args.microphone

    def load_model(self, model: Dict[str, Any]) -> None:
        """
        Load configuration attributes from the model.
        """
        self.feets_per_pixel = model["feets_per_pixel"]
        self.feets_per_inch = model["feets_per_inch"]
        self.template_path = model["template_image"]


config = Config()
"Singleton instance of the configuration class."
