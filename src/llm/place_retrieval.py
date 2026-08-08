"""EmbeddingGemma (L1) retrieval over MapIO PoIs.

Python port of starter/src/lib/placeIndex.js, reduced to what the parity
benchmark needs: build an embedding index over a map's POIs once, then return
the top-k candidates for each utterance. The curated prompt formatter injects
those candidates into the user turn instead of serializing all POIs into the
system prompt (see local-llm-tooling-design.md §4.2, §6.4).

Document format follows §6.4: name + categories + street context +
location_description + accessibility flags — but an accessibility flag is only
embedded if fewer than a third of the map's POIs have it (near-universal
attributes dilute the vector and barely beat chance; measured in §6.4).
EmbeddingGemma's task prefixes are mandatory: they nearly double the relative
separation between top-1 and top-2 (§6.4).
"""

import hashlib
import json
import os
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.config import paths

DOC_PREFIX = "title: {name} | text: {text}"
QUERY_PREFIX = "task: search result | query: {query}"

# §6.4 base-rate rule: attributes held by more than roughly a third of places
# stop earning their tokens. Derived per map, not hard-coded per attribute.
MAX_ATTRIBUTE_PREVALENCE = 1 / 3

RECORD_VERSION = 1  # bump when document() changes, or stale caches rank badly


def document(poi_info: Dict[str, Any], rare_flags: Sequence[str]) -> str:
    """The text that gets embedded for one POI."""
    parts: List[str] = []

    categories = poi_info.get("categories") or []
    if categories:
        parts.append(", ".join(categories))

    street = " ".join(
        str(x) for x in (poi_info.get("street"), poi_info.get("housenumber")) if x
    )
    if street:
        parts.append(street)

    if poi_info.get("location_description"):
        parts.append(poi_info["location_description"])

    accessibility = poi_info.get("accessibility") or {}
    flags = [
        flag.replace("_", " ")
        for flag in rare_flags
        if accessibility.get(flag) is True
    ]
    if flags:
        parts.append("accessibility: " + ", ".join(flags))

    if poi_info.get("opening_hours"):
        parts.append(f"open {poi_info['opening_hours']}")

    return DOC_PREFIX.format(name=poi_info.get("name", ""), text=". ".join(parts))


def rare_accessibility_flags(poi_infos: Sequence[Dict[str, Any]]) -> List[str]:
    counts: Dict[str, int] = {}
    for info in poi_infos:
        for flag, value in (info.get("accessibility") or {}).items():
            if value is True:
                counts[flag] = counts.get(flag, 0) + 1
    n = max(len(poi_infos), 1)
    return sorted(f for f, c in counts.items() if c / n <= MAX_ATTRIBUTE_PREVALENCE)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class PlaceRetrieval:
    def __init__(
        self,
        base_url: str,
        model: str = "l1",
        cache_dir: Optional[str] = None,
        timeout: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        # None rather than a computed default, so $MAPIO_HOME is read at
        # construction time instead of at import time -- the benchmark sets it
        # after this module is already loaded.
        self.cache_dir = cache_dir or paths.cache_dir()
        self.timeout = timeout

        self._docs: List[str] = []
        self._vectors: List[List[float]] = []
        self._poi_indices: List[int] = []

    def _embed(self, texts: List[str]) -> List[List[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = sorted(data["data"], key=lambda d: d["index"])
        return [row["embedding"] for row in rows]

    def build(self, map_id: str, pois: Sequence[Any]) -> None:
        """`pois` are src.graph.PoI objects; index maps utterances to poi.index."""
        infos = [dict(poi.info, name=poi.name, street=poi.street) for poi in pois]
        rare = rare_accessibility_flags(infos)
        self._docs = [document(info, rare) for info in infos]
        self._poi_indices = [poi.index for poi in pois]

        key = hashlib.sha1(
            json.dumps([RECORD_VERSION, self.model, self._docs]).encode("utf-8")
        ).hexdigest()[:12]
        cache_path = os.path.join(self.cache_dir, f"emb_{map_id}_{key}.json")

        # benchmark/.cache is where the index lived before the data-directory
        # split. Read from it when the current location has no entry, so maps
        # already indexed there are not re-embedded through l1; writes always
        # go to the current location.
        for path in (cache_path, os.path.join(paths.LEGACY_CACHE_DIR, os.path.basename(cache_path))):
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self._vectors = json.load(f)
                return

        self._vectors = self._embed(self._docs)
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(self._vectors, f)

    def top_k(self, utterance: str, k: int = 8) -> List[Tuple[int, float]]:
        """Ranked [(poi_index, score)], best first."""
        if not self._vectors:
            raise RuntimeError("PlaceRetrieval.build() must run first")
        qvec = self._embed([QUERY_PREFIX.format(query=utterance)])[0]
        scored = [
            (poi_index, _cosine(qvec, vec))
            for poi_index, vec in zip(self._poi_indices, self._vectors)
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:k]
