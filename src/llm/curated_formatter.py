"""Curated prompt formatter: MapIO's prompt with the full-graph dump replaced
by a compact skeleton plus per-question L1-retrieved candidates.

Why this exists: the full MapIO system prompt for the new_york model is ~11K
tokens, which does not fit the 8192-token window that an 8 GB M1 can serve
(see starter/docs/local-llm-tooling-design.md §8). The parity experiment is
therefore: GPT-4o + full context (the paper's numbers) vs. local E4B + this
curated context. Same tools, same real Graph execution, same benchmark.

What stays byte-identical to MapIO: header, node-naming rules, units,
base_instructions, few-shot examples, and the per-POI line format (PoI.__str__,
the same fields MapIO's full dump used). What changes:

- nodes/edges dump      -> street census grouped by orientation, in cross-map
                           order, so parallel/relative survey questions stay
                           answerable at ~2% of the tokens
- full POI dump         -> name+category census (existence questions need the
                           full list: "is there a Walmart?" must be deniable)
                           plus top-k retrieved candidates injected per
                           question, in the USER turn (§6.1: putting volatile
                           text in the system prompt destroys the KV prefix)
- per-edge feature dump -> per-feature street aggregates, with full per-segment
                           detail attached to the position update when the user
                           is on a segment
"""

from typing import Any, Dict, List, Optional, Tuple

from openai.types.chat import (
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from src.graph import Graph
from src.graph.edge import Edge, Features as EdgeFeatures, Street
from src.graph.node import Node
from src.position import PositionInfo
from src.utils import str_dict

from .place_retrieval import PlaceRetrieval
from .prompt_formatter import PromptFormatter


class CuratedPromptFormatter(PromptFormatter):
    def __init__(
        self,
        prompt_file: str,
        graph: Graph,
        retrieval: PlaceRetrieval,
        k: int = 8,
    ) -> None:
        super().__init__(prompt_file, graph)
        self.retrieval = retrieval
        self.k = k

    # ------------------------------------------------------------------ #
    # System prompt: skeleton instead of dump                            #
    # ------------------------------------------------------------------ #

    def get_main_prompt(
        self, context: Dict[str, str]
    ) -> ChatCompletionSystemMessageParam:
        main_prompts = self.prompt_components["main"]
        from datetime import datetime

        prompt = main_prompts["header"] + "\n"
        prompt += "###Context###\n\n"

        prompt += self.__street_census() + "\n\n"
        prompt += main_prompts["graph"]["nodes_naming"] + "\n\n"
        prompt += self.__poi_census() + "\n\n"
        prompt += self.__features_summary() + "\n\n"

        prompt += (
            main_prompts["units"].format(
                self.graph.reference_system.north,
                self.graph.reference_system.south,
                self.graph.reference_system.west,
                self.graph.reference_system.east,
            )
            + "\n"
        )

        prompt += main_prompts["context"].format(
            datetime.now().strftime("%A %m-%d-%Y %H:%M:%S"), str_dict(context)
        )

        prompt += "###Instructions###\n\n"
        prompt += (
            main_prompts["instructions"].format(
                self.prompt_components["base_instructions"].strip()
            )
            + "\n\n"
        )

        prompt += "###Examples###\n\n"
        prompt += "\n".join(self.prompt_components["examples"])

        return ChatCompletionSystemMessageParam(content=prompt, role="system")

    def __street_orientation(self, street: Street) -> Tuple[str, float]:
        """('east-west' | 'north-south', cross-axis position for ordering)."""
        first = street.edges[0].node1.coords
        last = street.edges[-1].node2.coords
        direction = last - first
        east = self.graph.reference_system.east
        north = self.graph.reference_system.north

        def dot(a, b) -> float:
            return a[0] * b[0] + a[1] * b[1]

        along_east = abs(dot(direction, east))
        along_north = abs(dot(direction, north))

        mid = (first + last) / 2
        if along_east >= along_north:
            return "east-west", dot(mid, north)
        return "north-south", dot(mid, east)

    def __street_census(self) -> str:
        groups: Dict[str, List[Tuple[float, Street]]] = {
            "east-west": [],
            "north-south": [],
        }
        for street in self.graph.streets.values():
            orientation, cross = self.__street_orientation(street)
            groups[orientation].append((cross, street))

        lines = [
            f"The map shows a road network with {len(self.graph.streets)} streets, "
            f"{len(self.graph.nodes)} intersections (nodes) and {len(self.graph.edges)} street segments (edges). "
            "Node and edge details are provided with your position and by the available functions; "
            "coordinates span from {} to {}.".format(*self.graph.bounds)
        ]

        ew = [s.name for _, s in sorted(groups["east-west"], key=lambda t: -t[0])]
        ns = [s.name for _, s in sorted(groups["north-south"], key=lambda t: t[0])]
        if ew:
            lines.append(
                "Streets running east-west, listed from north to south: " + "; ".join(ew) + "."
            )
        if ns:
            lines.append(
                "Streets running north-south, listed from west to east: " + "; ".join(ns) + "."
            )
        lines.append(
            "Streets in the same list are parallel to each other; streets in different lists cross each other where they share an intersection."
        )
        return "\n".join(lines)

    def __poi_census(self) -> str:
        entries = []
        for poi in self.graph.pois:
            categories = poi.info.get("categories") or []
            entries.append(f"{poi.name} ({categories[0] if categories else 'unknown'})")
        header = (
            f"The map contains exactly these {len(self.graph.pois)} points of interest, listed as name (category). "
            "This list is complete: anything not on it is NOT on the map. "
            "Full details for the points of interest most relevant to each question "
            "(index, street, coords, edge, categories and more) are provided with the question itself; "
            "use get_point_of_interest_details for anything beyond that.\n"
        )
        return header + "; ".join(entries)

    def __features_summary(self) -> str:
        street_sets: Dict[str, set] = {}

        def add(label: str, street: str) -> None:
            street_sets.setdefault(label, set()).add(street)

        for edge in self.graph.edges:
            f = edge.features
            if f.get(EdgeFeatures.ROADWORK):
                add("ongoing roadwork", edge.street)
            if f.get(EdgeFeatures.STAIRS):
                add("stairs", edge.street)
            if f.get(EdgeFeatures.BIKE_LANE):
                add("a bike lane", edge.street)
            surface = f.get(EdgeFeatures.SURFACE)
            if surface and surface != "concrete":
                add(f"a {surface} surface", edge.street)

        lines = [self.prompt_components["main"]["graph"]["accessibility_features"].strip()]
        for label, streets in sorted(street_sets.items()):
            lines.append(
                f"Streets with at least one segment with {label}: "
                + ", ".join(sorted(streets)) + "."
            )

        walk_lights = sum(
            1 for node in self.graph.nodes if node.features.get("walk_light")
        )
        tactile = sum(
            1 for node in self.graph.nodes if node.features.get("tactile_paving")
        )
        lines.append(
            f"{walk_lights} of {len(self.graph.nodes)} intersections have a walklight and "
            f"{tactile} have tactile paving. "
            "Exact per-segment and per-intersection features (slope, surface, walklight and its duration, "
            "crosswalks, street width) are included with your position when you are on that segment or intersection."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # User message: candidates + position (with features) + question     #
    # ------------------------------------------------------------------ #

    def get_user_message(
        self, question: str, position: Optional[PositionInfo]
    ) -> ChatCompletionUserMessageParam:
        components = self.prompt_components["question"]

        candidates_prompt = self.__candidates_prompt(question)

        position_prompt = ""
        if position is not None:
            base = super().get_user_message(question, position)["content"]
            # Reuse the parent's position section verbatim: everything before
            # our own question marker.
            position_prompt = str(base).split("###Question###")[0].rstrip()
            features = self.__position_features(position)
            if features:
                position_prompt += "\n" + features
            position_prompt += "\n"

        question_prompt = "###Question###\n\n" + question + "\n"
        instructions = self.get_instructions_prompt()["content"]

        prompt = f"{candidates_prompt}\n{position_prompt}\n{question_prompt}\n{instructions}"
        return ChatCompletionUserMessageParam(content=prompt, role="user")

    def __candidates_prompt(self, question: str) -> str:
        ranked = self.retrieval.top_k(question, k=self.k)
        by_index = {poi.index: poi for poi in self.graph.pois}
        lines = [
            "###Relevant Points of Interest###",
            "",
            "These are the points of interest most relevant to my next question, "
            "already ranked most relevant first. This ranking was computed for you; "
            "treat it as the resolution of whatever place my question refers to. "
            "The fields are the same as in the map data:",
            "",
        ]
        for poi_index, _score in ranked:
            poi = by_index.get(poi_index)
            if poi is not None:
                lines.append(str(poi))
        lines.append(
            "These are candidates for THIS question only, not the whole map; "
            "the complete list of points of interest is in the system prompt, and "
            "get_nearby_points_of_interest works on all of them."
        )
        return "\n".join(lines)

    def __position_features(self, position: PositionInfo) -> str:
        parts: List[str] = []

        element = position.graph_element
        edge: Optional[Edge] = None
        if isinstance(element, Edge):
            edge = element
        elif element is not None and hasattr(element, "edge"):
            edge = element.edge
        else:
            try:
                edge, _ = self.graph.get_nearest_edge(position.real_pos)
            except Exception:
                edge = None

        if edge is not None:
            parts.append(
                "Features of the street segment at my position:\n"
                + self._PromptFormatter__edge_features_prompt(edge)
            )

        if isinstance(element, Node):
            parts.append(
                "Features of the intersection at my position:\n"
                + self._PromptFormatter__node_features_prompt(element)
            )

        return "\n".join(parts)
