import argparse

from .config import Lang

mapio_parser = argparse.ArgumentParser(description="MapIO, with LLM integration")

mapio_parser.add_argument("--model", help="Path to model json file.", required=True)
mapio_parser.add_argument(
    "--out",
    help="Path to chat save file.",
    default="out/last_chat.txt",
)

mapio_parser.add_argument(
    "--lang", help="System language", type=Lang, choices=list(Lang), default=Lang.EN
)
mapio_parser.add_argument(
    "--prompt",
    help="Path to the prompt yaml. Defaults to res/prompt_<lang>.yaml, which is "
    "upstream MapIO's. Local runs should point this at the prompt the benchmark "
    "was run with, e.g. res/prompt_en_fixed.yaml.",
    default=None,
)
mapio_parser.add_argument(
    "--tts-rate",
    help="TTS speed rate (words per minute).",
    type=int,
    default=200,
)

mapio_parser.add_argument(
    "--no-llm",
    help="Disable llm interaction.",
    action="store_true",
    default=False,
)
mapio_parser.add_argument(
    "--no-stt",
    help="Replace STT with keyboard input.",
    action="store_true",
    default=False,
)

mapio_parser.add_argument(
    "--camera",
    help='Camera to use, skipping the selection prompt: either a device number '
    '(--camera 1) or part of its name (--camera "HUE"). Prefer the name -- '
    "device numbers shift when a virtual camera starts or stops. The prompt "
    "prints the right value once you have found a working camera.",
    default=None,
)
mapio_parser.add_argument(
    "--microphone",
    help='Microphone to record from: a device number (--microphone 2) or part '
    'of its name (--microphone "Anker"). Defaults to the system input device, '
    "which is often not the one pointed at you.",
    default=None,
)
mapio_parser.add_argument(
    "--debug",
    help="Enable debug mode.",
    action="store_true",
    default=False,
)

get_args = mapio_parser.parse_args
"""
Get the arguments from the command line. These should be passed to the Config class to load the configuration.
"""
