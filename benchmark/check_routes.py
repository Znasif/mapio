#!/usr/bin/env python3
"""Did the guide calls actually produce usable routes?

Grading has always stopped at the tool call: a turn scored `correct` when the
model asked to be guided to the right place, because --routing stub recorded
the request and threw it away. This reads a run made with --routing local and
checks the other half -- that navigation received waypoints it could speak.

    python benchmark/check_routes.py
    python benchmark/check_routes.py --results benchmark/results/arm1_curated_stt_localrouting

A turn passes when every guide call it made is answered by an ON_ROUTE with at
least one waypoint, and street-by-street routes carry spoken instructions.
"""

import argparse
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")


def newest_run(folder):
    files = sorted(glob.glob(os.path.join(folder, "parity_*.json")))
    return files[-1] if files else None


def check_turn(turn):
    """(status, detail). status in {'ok', 'no_route', 'error', 'silent', 'n/a'}."""

    guides = [g for g in turn.get("guide_calls", [])]
    routes = turn.get("routes", [])

    if not guides:
        return "n/a", ""

    on_route = [r for r in routes if r["action"] == "ON_ROUTE"]
    errors = [r for r in routes if r["action"] == "ERROR"]

    if errors and not on_route:
        return "error", f"{len(guides)} guide call(s) -> RouteAction.ERROR"

    if not on_route:
        return "no_route", f"{len(guides)} guide call(s) -> no route callback at all"

    for route in on_route:
        waypoints = route.get("waypoints") or []
        if not waypoints:
            return "no_route", "ON_ROUTE with an empty waypoint list"
        # Fly-me-there legitimately carries one bare waypoint; street-by-street
        # is what the user is meant to hear turn by turn.
        if route["street_by_street"] and not any(w["instructions"] for w in waypoints):
            return "silent", f"{len(waypoints)} waypoints, none with instructions"

    total = sum(len(r.get("waypoints") or []) for r in on_route)
    return "ok", f"{len(guides)} guide call(s) -> {total} waypoints"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", default=None,
                        help="a run folder, or a parity_*.json; default: newest "
                             "folder whose name mentions routing")
    parser.add_argument("--verbose", action="store_true",
                        help="print every waypoint, not just the summary")
    args = parser.parse_args()

    target = args.results
    if target is None:
        folders = sorted(glob.glob(os.path.join(RESULTS, "*routing*")))
        if not folders:
            raise SystemExit(
                "No routing run found. Make one with:\n"
                "  python benchmark/run_arms.py --arms 1 --routing local "
                "--label-suffix _localrouting"
            )
        target = folders[-1]

    path = target if target.endswith(".json") else newest_run(target)
    if not path:
        raise SystemExit(f"No parity_*.json under {target}")

    with open(path, encoding="utf-8") as f:
        run = json.load(f)

    print(f"run:     {os.path.relpath(path, HERE)}")
    print(f"routing: {run.get('routing', 'stub (nothing to check)')}\n")

    tally = {}
    for case in run["results"]:
        for turn in case["turns"]:
            status, detail = check_turn(turn)
            tally[status] = tally.get(status, 0) + 1
            if status == "n/a":
                continue

            mark = {"ok": "  ok  ", "no_route": " FAIL ",
                    "error": " FAIL ", "silent": " WARN "}[status]
            print(f"[{mark}] {turn['id']:10} {detail}")
            print(f"          {turn['utterance'][:70]}")

            for route in turn.get("routes", []):
                if route["action"] != "ON_ROUTE":
                    continue
                for waypoint in route.get("waypoints") or []:
                    if args.verbose or status != "ok":
                        print(f"             - {waypoint['instructions'] or '(no text)'}")

    print(f"\n{'=' * 66}")
    guided = sum(v for k, v in tally.items() if k != "n/a")
    print(f"turns with a guide call: {guided}   (no guidance requested: "
          f"{tally.get('n/a', 0)})")
    for status in ("ok", "silent", "no_route", "error"):
        if tally.get(status):
            print(f"  {status:9} {tally[status]}")

    if guided == 0:
        print("\nNo guide calls in this run -- nothing was exercised.")
    elif tally.get("ok") == guided:
        print("\nEvery guide call produced a route with spoken waypoints.")
    else:
        print("\nSome guide calls did not produce usable navigation. "
              "Those turns may have graded `correct` on the tool call alone.")


if __name__ == "__main__":
    main()
