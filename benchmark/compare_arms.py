#!/usr/bin/env python3
"""Put the three arms side by side, turn by turn.

Reads the newest run in each benchmark/results/<arm>/ folder and prints a table
of what each architecture did with the same utterance: the tool calls it made,
how long it took, and how much of its prompt was cached.

    python benchmark/compare_arms.py
    python benchmark/compare_arms.py --field answer     # answers instead of calls

Tool calls are the column to trust. Answer text agrees far more often than the
map actions do -- a run can say "yes, it's on the map" while highlighting the
wrong building.
"""

import argparse
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
ARM_ORDER = ["arm1_curated_stt", "arm2_full_stt", "arm3_full_audio"]


def latest(arm: str) -> dict | None:
    files = sorted(glob.glob(os.path.join(RESULTS, arm, "parity_*.json")))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        run = json.load(f)
    run["_path"] = files[-1]
    return run


def turns_by_id(run: dict) -> dict:
    return {t["id"]: t for c in run["results"] for t in c["turns"]}


def calls(turn: dict) -> str:
    names = [
        f"{tc['name']}({tc['arguments']})"
        for m in turn.get("transcript", []) for tc in m.get("tool_calls", [])
    ]
    return "; ".join(names) if names else "(none)"


def summarise(turn: dict) -> str:
    rounds = turn.get("rounds") or []
    cached = rounds[0].get("cached_tokens") if rounds else None
    prompt = rounds[0].get("prompt_tokens") if rounds else None
    bits = [f"{turn['elapsed_sec']}s"]
    if prompt is not None:
        bits.append(f"{cached}/{prompt} cached")
    return "  ".join(bits)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--field", choices=["calls", "answer"], default="calls")
    parser.add_argument("--results", default=RESULTS)
    args = parser.parse_args()

    runs = {}
    for arm in ARM_ORDER:
        run = latest(arm)
        if run:
            runs[arm] = run

    if not runs:
        raise SystemExit(f"No runs found under {args.results}/<arm>/. "
                         "Run benchmark/run_arms.py first.")

    print("runs compared:")
    for arm, run in runs.items():
        warm = f", warmup {run.get('warmup_sec')}s" if run.get("warmup_sec") else ""
        print(f"  {arm:18} {os.path.basename(run['_path'])}  "
              f"({run.get('formatter')}/{run.get('input')}{warm})")

    tables = {arm: turns_by_id(run) for arm, run in runs.items()}
    ids = []
    for arm in runs:
        for tid in tables[arm]:
            if tid not in ids:
                ids.append(tid)

    total_wer = {}
    for tid in ids:
        print(f"\n{'-' * 72}\n{tid}")
        reference = None
        for arm in runs:
            turn = tables[arm].get(tid)
            if not turn:
                print(f"  {arm:18} —")
                continue

            spoken = turn.get("spoken") or {}
            reference = reference or spoken.get("reference_utterance") or turn["utterance"]
            if spoken.get("sent_as") == "transcript":
                total_wer.setdefault(arm, [0, 0])
                total_wer[arm][0] += spoken["wer_errors"]
                total_wer[arm][1] += spoken["wer_words"]

            value = calls(turn) if args.field == "calls" else (turn.get("answer") or "")
            print(f"  {arm:18} {summarise(turn)}")
            print(f"  {'':18} {value}")
        if reference:
            print(f"  {'reference':18} {reference}")

    print(f"\n{'=' * 72}")
    for arm, run in runs.items():
        turns = list(tables[arm].values())
        elapsed = sum(t["elapsed_sec"] for t in turns)
        line = (f"{arm:18} {len(turns):2} turns  {elapsed:7.1f}s total  "
                f"{elapsed / max(len(turns), 1):5.1f}s/turn")
        if arm in total_wer:
            e, w = total_wer[arm]
            line += f"  WER {e}/{w} = {100 * e / w:.1f}%"
        print(line)

    print("\nGrade each turn in the per-arm .md files, then compare grades. "
          "Tool calls are the reliable signal; answer text agrees far more often "
          "than the map actions do.")


if __name__ == "__main__":
    main()
