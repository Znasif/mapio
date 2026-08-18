import time

from src.graph import Graph, WayPoint, get_direction
from src.position import PositionInfo

from .navigator import ActionHandler, Navigator


class FlyOverNavigator(Navigator):
    ANNOUNCEMENTS_INTERVAL = 1.25  # seconds

    def __init__(
        self,
        graph: Graph,
        arrived_threshold: float,
        far_threshold: float,
        on_action: ActionHandler,
        destination: WayPoint,
    ) -> None:
        super().__init__(graph, on_action)

        self.arrived_threshold = arrived_threshold
        self.far_threshold = far_threshold

        self.destination = destination
        self.last_announcement_timestamp = 0.0

    def update(self, position: PositionInfo, ignore_not_moving: bool) -> None:
        if not self.is_running():
            return

        distance = position.real_pos.distance_to(self.destination.coords)
        if distance < self.arrived_threshold:
            self._destination_reached(self.destination)
            return

        current_time = position.timestamp
        if (
            current_time - self.last_announcement_timestamp
            < self.ANNOUNCEMENTS_INTERVAL
        ):
            return

        error = self.destination.coords - position.real_pos

        # The dominant-axis rule this used to spell out inline now lives in
        # get_direction, which street-by-street headings also go through. Same
        # four directions, same tie-break; one definition instead of two.
        direction = get_direction(error).value

        if distance > self.far_threshold:
            direction = f"far {direction}"

        self._announce_directions(direction)

    def _announce_directions(self, instructions: str) -> None:
        self.last_announcement_timestamp = time.time()
        return super()._announce_directions(instructions)
