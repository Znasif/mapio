import json
import os
from enum import Enum
from typing import Any, Dict, Mapping, Optional


def str_format(v: Any) -> str:
    """
    Convert a value to a string representation and replace underscores with spaces.
    """
    return str(v).replace("_", " ")


def str_dict(d: Mapping[Any, Any], indent: int = 0) -> str:
    """
    Convert a dictionary to a string representation with proper indentation.
    """
    res = ""

    for key, value in d.items():
        res += " " * indent + str_format(key) + ": "
        if isinstance(value, dict):
            res += "\n" + str_dict(value, indent + 4)
        elif isinstance(value, list):
            if len(value) == 0:
                res += "[]\n"
            elif len(value) == 1:
                res += "[ " + str_format(value[0]) + " ]\n"
            else:
                res += "[\n"
                for item in value:
                    res += " " * (indent + 4) + str_format(item) + ",\n"
                res += " " * indent + "]\n"
        else:
            res += str_format(value) + "\n"

    return res


def load_map_parameters(filename: str) -> Optional[Dict[str, Any]]:
    """
    Load map parameters from a JSON file.
    If the file does not exist or is not a valid JSON file, return None.
    """
    if not os.path.exists(filename) or not os.path.isfile(filename):
        return None

    try:
        with open(filename, "r") as f:
            map_params = json.load(f)
    except Exception as e:
        return None

    map_params = dict(map_params)

    template = map_params.get("template_image")
    if template:
        map_params["template_image"] = resolve_template(template, filename)

    return map_params


def resolve_template(template: str, map_file: str) -> str:
    """Locate a map's template image.

    The path stored in the JSON is repo-relative ("models/new_york/template.png"),
    which only resolves when cwd happens to be the repo root. Once maps live in
    a user data directory that is never true, so fall back to looking beside the
    JSON itself -- where every existing model keeps its template anyway.

    The value is returned unchanged when nothing better is found: cv2.imread
    reports a missing template the same way either way, and rewriting it to a
    path that also does not exist would only make the error harder to read.
    """
    if os.path.isabs(template) or os.path.exists(template):
        return template

    map_dir = os.path.dirname(os.path.abspath(map_file))
    for candidate in (
        os.path.join(map_dir, template),
        os.path.join(map_dir, os.path.basename(template)),
    ):
        if os.path.exists(candidate):
            return candidate

    return template


class StrEnum(str, Enum):
    """
    Enum class that inherits from str and Enum.
    This allows for the enum values to be used as strings.
    """

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return str(self)


class CardinalDirection(StrEnum):
    """
    Enum for cardinal directions.
    """

    SOUTH_WEST = "south-west"
    WEST = "west"
    NORTH_WEST = "north-west"
    NORTH = "north"
    NORTH_EAST = "north-east"
    EAST = "east"
    SOUTH_EAST = "south-east"
    SOUTH = "south"
