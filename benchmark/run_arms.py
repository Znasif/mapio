#!/usr/bin/env python3
"""Run the three architectures over the same recorded session and compare.

    arm1_curated_stt    L1 curation + Apple STT transcript      ctx 8192
    arm2_full_stt       whole graph in context + transcript     ctx 16384
    arm3_full_audio     whole graph in context + raw audio      ctx 16384

arm2 - arm1 is what curation costs in accuracy.
arm3 - arm2 is what STT costs.
Run today, those two are confounded; that is the point of the split.

Every arm sees the same wavs, the same turns and the same prompt file, and each
warms the prefix before its first turn so per-turn timings are warm.

    python benchmark/run_arms.py                    # all three
    python benchmark/run_arms.py --arms 1           # just the baseline
    python benchmark/run_arms.py --case DT-SESSION

Results land in benchmark/results/<arm>/parity_<ts>.{json,md}, one folder per
arm, so grading and diffing stay separate.

ctx-size: arm1 runs on the standard 8192 instance. arm2 and arm3 need l3
restarted with --ctx-size 16384 -- at 8192 the server rejects an 11k prompt with
a 400 before allocating anything. Run --arms 1 first, restart, then --arms 2,3.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RUNNER = os.path.join(REPO, "run_parity_benchmark.py")
MANIFEST = os.path.join(HERE, "audio", "manifest.json")
AUDIO_DIR = os.path.join(HERE, "audio")
RESULTS = os.path.join(HERE, "results")

ARMS = {
    "1": {
        "label": "arm1_curated_stt",
        "why": "L1 curation + STT transcript (today's baseline, ctx 8192)",
        "flags": ["--formatter", "curated", "--input", "text"],
    },
    "2": {
        "label": "arm2_full_stt",
        "why": "whole graph in context + STT transcript (ctx 16384)",
        "flags": ["--formatter", "full", "--input", "text"],
    },
    "3": {
        "label": "arm3_full_audio",
        "why": "whole graph in context + raw audio, no STT (ctx 16384)",
        "flags": ["--formatter", "full", "--input", "audio"],
    },
    # One model instead of two. LFM2.5-2.6B is small enough (2.87 GB at Q8_0,
    # against l3's ~4.7 GiB) and long enough in context (128K, against 8192)
    # that it can hold the whole graph itself -- which is what l1 exists to
    # avoid. So --formatter full drops the retrieval step entirely and this arm
    # answers one question: does the pair earn its complexity?
    #
    # Point --server at the l4-only router, not :8081:
    #   ABTC_LLM_PRESETS=~/.config/abtc/models-l4.ini ABTC_LLM_PORT=8082 \
    #       start-ai --no-quiet
    #   python benchmark/run_arms.py --arms 4 --server http://127.0.0.1:8082/v1
    "4": {
        "label": "arm4_lfm_single",
        "why": "LFM2.5-2.6B alone: whole graph in context, no l1, no l3",
        "flags": ["--formatter", "full", "--input", "text", "--model", "l4"],
    },
    # arm5 - arm1 is what the model is worth; arm5 - arm4 is what the context
    # strategy is worth. arm4's first run answered in 26-102s against l3's 74s
    # on the same case, with prefix caching already working -- so on this
    # machine a 2.6B model reading 18.7K tokens is not obviously cheaper than a
    # 5B model reading a curated 8K. Swapping only the model, and leaving the
    # l1 retrieval step in place, separates those two claims.
    "5": {
        "label": "arm5_lfm_curated",
        "why": "LFM2.5-2.6B with L1 curation: today's pipeline, new chat model",
        "flags": ["--formatter", "curated", "--input", "text",
                  "--model", "l4", "--embed-model", "l1"],
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arms", default="1,2,3",
                        help="comma-separated subset, e.g. 2,3")
    # 11434 is the Windows tunnel-side port; the Mac serves on 8081 natively and
    # the l4 router on 8082. Default left alone so existing runs are unchanged,
    # but LLM_BASE_URL now overrides it, matching run_parity_benchmark.py.
    parser.add_argument("--server",
                        default=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"))
    parser.add_argument("--prompt", default=os.path.join(REPO, "res", "prompt_en_fixed.yaml"))
    parser.add_argument("--benchmark", default=MANIFEST)
    parser.add_argument("--case", default=None)
    parser.add_argument("--model", default=None,
                        help="override the arm's chat model, e.g. --model l4nj "
                             "to run the same arm against a differently served "
                             "tier. Pair it with --label-suffix so the two runs "
                             "land in separate folders.")
    parser.add_argument("--map", default=None)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--no-stt-hints", action="store_true",
                        help="drop the POI contextual hints, to A/B the biasing")
    parser.add_argument("--no-audio", action="store_true",
                        help="send each turn's written utterance instead of its "
                             "recording; the manifest still limits the run to "
                             "recorded turns, so it isolates what STT costs")
    parser.add_argument("--label-suffix", default=None,
                        help="override the folder suffix for a comparator run")
    parser.add_argument("--routing", choices=["stub", "local", "google"],
                        default="stub",
                        help="stub keeps guide calls as no-ops, which is how "
                             "every graded run so far worked; local actually "
                             "routes and records the waypoints")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands without running them")
    args = parser.parse_args()

    if not os.path.exists(args.benchmark):
        sys.exit(f"[ERROR] no manifest at {args.benchmark}\n"
                 f"        record some turns first: python benchmark/record_audio.py")

    with open(args.benchmark, encoding="utf-8") as f:
        manifest = json.load(f)
    n_turns = sum(len(c.get("turns") or [c]) for c in manifest["cases"])
    print(f"manifest: {n_turns} recorded turns across {len(manifest['cases'])} cases\n")

    selected = [a.strip() for a in args.arms.split(",") if a.strip()]
    for arm in selected:
        if arm not in ARMS:
            sys.exit(f"[ERROR] unknown arm {arm!r}; pick from {', '.join(ARMS)}")

    # Comparator runs go to their own folder, so an A/B never overwrites the
    # baseline it is being compared against.
    suffix = args.label_suffix
    if suffix is None:
        suffix = (("_text" if args.no_audio else "")
                  + ("_nohints" if args.no_stt_hints else "")
                  + ("" if args.routing == "stub" else f"_{args.routing}routing"))

    for arm in selected:
        spec = ARMS[arm]
        flags = list(spec["flags"])
        if args.model:
            # Drop the arm's own --model pair before appending the override,
            # so the runner does not see the option twice.
            if "--model" in flags:
                i = flags.index("--model")
                del flags[i:i + 2]
            flags += ["--model", args.model]
        cmd = [
            args.python, RUNNER,
            "--server", args.server,
            "--prompt", args.prompt,
            "--benchmark", args.benchmark,
            "--warmup",
            "--out-dir", RESULTS,
            "--label", spec["label"] + suffix,
        ] + flags
        if not args.no_audio:
            cmd += ["--audio-dir", AUDIO_DIR, "--audio-only"]
        if args.no_stt_hints:
            cmd += ["--no-stt-hints"]
        if args.routing != "stub":
            cmd += ["--routing", args.routing]
        if args.case:
            cmd += ["--case", args.case]
        if args.map:
            cmd += ["--map", args.map]

        print(f"\n{'=' * 72}\narm {arm}: {spec['why']}"
              f"{'  [reference text, no STT]' if args.no_audio else ''}"
              f"{'  [no hints]' if args.no_stt_hints else ''}"
              f"\n  -> {RESULTS}/{spec['label'] + suffix}/\n{'=' * 72}")
        if args.dry_run:
            print(" ".join(cmd))
            continue

        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"\n[arm {arm}] exited {result.returncode}.")
            if arm in ("2", "3"):
                print("If this was a 400 on prompt length, l3 is still at "
                      "--ctx-size 8192. Restart it with 16384 and rerun:\n"
                      f"  python benchmark/run_arms.py --arms {arm}")
            sys.exit(result.returncode)

    print("\nDone. Compare with:\n  python benchmark/compare_arms.py")


if __name__ == "__main__":
    main()
