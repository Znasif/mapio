import os

from dotenv import load_dotenv

from src.config.paths import env_file

# $MAPIO_HOME/.env, which is the repo's own .env until MAPIO_HOME says otherwise.
# The bare load_dotenv() fallback keeps the upstream behaviour -- search cwd and
# upwards -- for a checkout run from a subdirectory.
if os.path.isfile(env_file()):
    load_dotenv(env_file())
else:
    load_dotenv()
os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "hide"

import sys
import threading as th
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from src.command_controller import CommandController
from src.config import config, get_args, paths
from src.frame_processing import GestureRecognizer, GestureResult, Hand, MapDetector
from src.graph import Graph, RouteAction, WayPoint
from src.llm import LLM, CuratedPromptFormatter, PlaceRetrieval, PromptFormatter
from src.modules_repository import ModulesRepository
from src.navigation import NavigationAction, NavigationController
from src.position import PositionHandler
from src.utils import Coords, load_map_parameters
from src.view import (
    KeyboardManager,
    UserAction,
    VideoCapture,
    ViewManager,
    ignore_action_end,
)
from src.view.audio import STT, Announcement, AudioManager, MapIOTTS

repository = ModulesRepository()
""" Repository for accessing all the modules in the application. """


def build_prompt_formatter(
    prompt_file: str, graph: Graph, map_id: str
) -> Optional[PromptFormatter]:
    """Curated context when serving locally, MapIO's full-graph dump otherwise.

    The full dump is ~29K tokens for new_york, which the 8192-token window an
    8 GB M1 serves rejects with a 400 before allocating anything. Every local
    number in benchmark/results/ was measured with CuratedPromptFormatter and
    its L1 retrieval step, so the app has to use the same pair or it is not
    running what was tested.

    Returning None leaves LLM's own default in place -- the unmodified
    full-graph formatter against OpenAI -- so nothing changes when
    LLM_BASE_URL is unset.
    """

    base_url = os.environ.get("LLM_BASE_URL")
    if not base_url or os.environ.get("MAPIO_DISABLE_RETRIEVAL", "0").lower() in ("1", "true", "yes"):
        return None

    retrieval = PlaceRetrieval(base_url, model=os.environ.get("LLM_EMBED_MODEL", "l1"))

    # map_id keys the embedding cache, and the benchmark keyed it by the model's
    # directory name. Matching it means a map already benchmarked starts from
    # the cache instead of re-embedding every POI through l1.
    print(f"Indexing {len(graph.pois)} points of interest for '{map_id}'...")
    try:
        retrieval.build(map_id, graph.pois)
    except Exception as e:
        # Exit rather than fall back to the full graph: that path 400s on every
        # single question, which looks like a broken app instead of a missing
        # server. The cache makes this a first-run-per-map requirement only.
        raise SystemExit(
            f"\nCould not reach the embedding server at {base_url}: {e}\n"
            f"The place index has to be built once per map before MapIO can "
            f"run locally.\nStart the local stack, or unset LLM_BASE_URL to "
            f"use OpenAI."
        )

    k = int(os.environ.get("LLM_CANDIDATES_K", "8"))
    return CuratedPromptFormatter(prompt_file, graph, retrieval, k=k)


class MapIOController:
    """
    Main controller for the MapIO application.
    """

    def __init__(self, model: Dict[str, Any], map_id: str) -> None:
        self.description = model["context"].get("description", None)

        # Model
        self.graph = Graph(model["graph"], self.__on_route)
        self.position_handler = PositionHandler()

        prompt_file = config.prompt_file or paths.resource(f"prompt_{config.lang}.yaml")
        self.llm = LLM(
            prompt_file,
            model["context"],
            temperature=config.temperature,
            formatter=build_prompt_formatter(prompt_file, self.graph, map_id),
        )

        self.model_detector = MapDetector()
        self.gesture_recognizer = GestureRecognizer()
        self.hand_status = GestureResult.Status.NOT_FOUND

        # View
        self.view = ViewManager(self.graph.pois)
        self.tts = MapIOTTS(
            paths.resource(f"strings_{config.lang}.json"), rate=config.tts_rate
        )
        self.stt = STT()
        self.audio_manager = AudioManager(paths.resource("sounds.json"))

        # User interaction
        self.navigation_controller = NavigationController(
            repository, self.__on_navigation_action
        )
        self.__action_listeners = self.__get_action_listeners()
        self.command_controller = CommandController(
            repository, paths.resource("voice_commands.json"), self.__on_user_action
        )
        self.keyboard = KeyboardManager(
            paths.resource("shortcuts.json"), self.__on_user_action
        )

        self.running = False

    def main_loop(self) -> None:
        """
        Main loop of the application.
        """

        video_capture = VideoCapture.get_capture()
        if video_capture is None:
            print("No camera found.")
            return

        frame = video_capture.read()
        if frame is None:
            print("No camera image returned.")
            return

        self.stt.calibrate()
        self.tts.start()

        self.keyboard.init_shortcuts()

        # Prime the model's prefix cache synchronously before opening windows,
        # so all subsequent voice questions are instantaneous and share the warm KV cache.
        if config.llm_enabled:
            print("\nPriming model KV cache with map graph in GenieX (one-time prefill)...", flush=True)
            self.__warm_up_llm()

        self.tts.welcome()
        self.tts.instructions()
        if self.description is not None:
            self.tts.map_description(self.description)

        self.audio_manager.start()
        last_hand_side: Optional[Hand.Side] = None

        self.running = True
        while self.running and video_capture.is_opened():
            self.view.update(frame, self.position_handler.last_info)

            frame = video_capture.read()
            if frame is None:
                print("No camera image returned.")
                break

            homography, frame = self.model_detector.detect(frame)

            if homography is None:
                self.audio_manager.hand_feedback(GestureResult.Status.NOT_FOUND)
                continue

            hand, frame = self.gesture_recognizer.detect(frame, homography)
            self.hand_status = hand.status

            self.audio_manager.hand_feedback(hand.status)

            # if hand.status == GestureResult.Status.MORE_THAN_ONE_HAND:
            #    self.tts.more_than_one_hand()

            if hand.position is None or hand.status != GestureResult.Status.POINTING:
                self.position_handler.clear()
                continue

            if hand.side != last_hand_side:
                last_hand_side = hand.side
                self.position_handler.clear()
                if hand.side is not None and not self.is_handling_user_input():
                    self.tts.hand_side(str(hand.side))

            self.position_handler.process_position(hand.position)
            position = self.position_handler.get_position_info()

            if self.navigation_controller.is_navigation_running():
                self.navigation_controller.update(
                    position,
                    ignore_not_moving=self.is_handling_user_input()
                    or self.tts.is_speaking(),
                )
            elif not self.is_handling_user_input():
                self.tts.position(position)
                self.audio_manager.position_feedback(position)

        self.audio_manager.stop()
        video_capture.stop()

        self.keyboard.disable_shortcuts()
        self.view.close()

        self.stop_interaction()
        self.position_handler.clear()

        self.tts.goodbye()
        time.sleep(2)
        self.tts.stop()

    def __warm_up_llm(self) -> None:
        elapsed = self.llm.warm_up()
        if elapsed is not None:
            print(f"Model prefix warm in {elapsed}s! KV cache primed. Opening debug windows...", flush=True)
        else:
            print("Warning: LLM warm-up did not complete cleanly.", flush=True)

    def is_handling_user_input(self) -> bool:
        return self.command_controller.is_handling_command()

    def stop(self) -> None:
        self.running = False
        # The audio source is held open for the whole session now, so releasing
        # it is this method's job -- otherwise the microphone-in-use indicator
        # outlives the app.
        self.stt.release_microphone()

    def save_chat(self, filename: str) -> None:
        self.llm.save_chat(filename)

    def stop_interaction(self) -> None:
        self.tts.stop_speaking()

        if self.is_handling_user_input():
            self.command_controller.stop_handling_command()

        if self.stt.is_recording():
            self.stt.end_recording()

    def say_map_description(self) -> None:
        self.stop_interaction()

        if self.description is not None:
            self.tts.map_description(self.description)
        else:
            self.tts.no_map_description()

    def __on_route(
        self,
        action: RouteAction,
        start: Coords,
        street_by_street: bool,
        waypoints: Optional[List[WayPoint]],
    ) -> None:
        if action == RouteAction.CALCULATING_ROUTE:
            return self.tts.start_calculating_route_loop()

        if action == RouteAction.ERROR or waypoints is None:
            # Unfreeze whatever asked for this route before saying anything: a
            # navigator that requested a reroute pauses until one arrives, and
            # nothing else ever clears that.
            self.navigation_controller.route_failed()
            self.tts.stop_calculating_route_loop()
            return self.tts.navigation_error()

        # New route
        self.tts.stop_calculating_route_loop()

        self.view.clear_waypoints()

        self.view.add_waypoint(start)
        for waypoint in waypoints:
            self.view.add_waypoint(waypoint.coords)

        if street_by_street:
            started = self.navigation_controller.navigate_street_by_street(waypoints)
        else:
            started = self.navigation_controller.navigate(waypoints[0])

        if not started:
            self.tts.navigation_error()

    def __on_command(self, ended: bool) -> None:
        if ended:
            if self.stt.is_recording():
                self.stt.end_recording(add_final_silence=True)

        elif self.llm.is_waiting_for_response() or self.stt.is_processing_audio():
            self.tts.waiting()

        else:
            self.stop_interaction()
            self.command_controller.handle_command()

    def __on_navigation_action(self, action: NavigationAction, **kwargs) -> None:
        if self.is_handling_user_input():
            return

        if action == NavigationAction.NEW_ROUTE:
            th.Thread(
                target=self.graph.guide_to_destination,
                args=(kwargs["start"], kwargs["destination"], True),
            ).start()

        elif action == NavigationAction.WAYPOINT_REACHED:
            self.audio_manager.play_waypoint_reached()

        elif action == NavigationAction.WRONG_DIRECTION:
            if not self.is_handling_user_input():
                self.tts.wrong_direction()

        elif action == NavigationAction.DESTINATION_REACHED:
            waypoint = kwargs["waypoint"]
            if waypoint == WayPoint.NONE:
                return

            self.tts.destination_reached()
            self.tts.add_pause(2.0)
            self.audio_manager.play_destination_reached()
            self.view.clear_waypoints()

        elif action == NavigationAction.ANNOUNCE_DIRECTION:
            if self.is_handling_user_input():
                return

            instructions: str = kwargs["instructions"]
            self.tts.stop_and_say(
                instructions,
                category=Announcement.Category.NAVIGATION,
                priority=Announcement.Priority.MEDIUM,
            )

    def __get_action_listeners(self) -> Dict[UserAction, Callable[[bool], None]]:
        listeners = {
            UserAction.STOP_INTERACTION: ignore_action_end(self.stop_interaction),
            UserAction.SAY_MAP_DESCRIPTION: ignore_action_end(self.say_map_description),
            UserAction.TOGGLE_TTS: ignore_action_end(self.tts.toggle_pause),
            UserAction.STOP: ignore_action_end(self.stop),
            UserAction.COMMAND: self.__on_command,
            UserAction.STOP_NAVIGATION: ignore_action_end(self.__stop_navigation),
            UserAction.DISABLE_POSITION_TTS: ignore_action_end(
                self.__disable_position_tts
            ),
            UserAction.ENABLE_POSITION_TTS: ignore_action_end(
                self.__enable_position_tts
            ),
            UserAction.FIX_MODEL: ignore_action_end(self.model_detector.fix_model),
        }

        if not config.llm_enabled:
            del listeners[UserAction.COMMAND]

        return listeners

    def __on_user_action(self, action: UserAction, started: bool = True) -> None:
        if action in self.__action_listeners:
            self.__action_listeners[action](not started)

    def __disable_position_tts(self) -> None:
        self.tts.disable_category(Announcement.Category.GRAPH)
        self.tts.position_paused()

    def __enable_position_tts(self) -> None:
        self.tts.enable_category(Announcement.Category.GRAPH)
        self.tts.position_resumed()

    def __stop_navigation(self) -> None:
        self.tts.stop_calculating_route_loop()
        self.navigation_controller.clear()
        self.view.clear_waypoints()


if __name__ == "__main__":
    args = get_args()
    config.load_args(args)

    # Created rather than demanded: out/ is gitignored, so a fresh checkout has
    # never had it, and a data directory starts empty by definition.
    out_file = args.out or paths.data("out", "last_chat.txt")
    out_dir = os.path.dirname(os.path.abspath(out_file))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print(f"\nCannot create chat directory {out_dir}: {e}")
        sys.exit(0)

    map_file = paths.resolve_map(args.model)
    model = load_map_parameters(map_file) if map_file else None
    if model is None:
        print(f"\nModel file {args.model} not found.")
        available = paths.available_maps()
        if available:
            print(f"Maps in {paths.models_dir()}: {', '.join(available)}")
        sys.exit(0)

    config.load_model(model)
    print(f"\nLoaded map: {model.get('name', 'Unknown')}\n")

    # The directory name, not the file name: models/new_york/new_york.json ->
    # "new_york", which is the id the benchmark cached its embeddings under.
    map_id = os.path.basename(os.path.dirname(os.path.abspath(map_file)))

    mapio: Optional[MapIOController] = None
    try:
        # Start the main controller and run the application
        mapio = MapIOController(model, map_id)
        mapio.main_loop()

    except KeyboardInterrupt:
        # Keyboard interrupt stops the application
        pass

    except Exception:
        # All other exceptions are caught and printed
        print(f"\nAn error occurred:\n")
        print(traceback.format_exc())

    if mapio is not None:
        # Save the chat log to a file
        mapio.stop()
        mapio.save_chat(out_file)
        print(f"\nChat saved to {out_file}")
