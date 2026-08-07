#!/usr/bin/env python3
"""MapIO parity benchmark runner: local LLM stack vs. the paper's GPT-4o bar.

Drives the REAL MapIO conversation loop (src.llm.LLM.ask: multi-round tool
calling, instruction re-injection, history) and the REAL Graph tool execution,
headless -- no camera, no STT/TTS, no cloud. The one difference from the paper's
setup is the prompt: the full ~11K-token graph dump does not fit the 8192-token
window an 8 GB M1 serves, so CuratedPromptFormatter supplies a skeleton plus
per-question L1-retrieved candidates (design doc §8: the ceiling is the reason
the L1->L3 architecture exists).

Google Routes is stubbed: guide_to_* only ever surfaces "Navigation mode is now
enabled." to the LLM, so a no-op stub preserves LLM-visible behavior while
keeping the run local-only.

Usage:
  ./run_parity_benchmark.py                          # auto-resolves the server
  ./run_parity_benchmark.py --server http://127.0.0.1:11434/v1 --case NY-S3
  ./run_parity_benchmark.py --map new_york

Real speech instead of the written utterances -- transcribes
benchmark/audio/<turn_id>.wav through tools/macos_stt/stt_server.py, then runs
the transcript through the same L1/L3 path, and reports WER against the
reference. --audio-only restricts the run to turns you have actually recorded:

  ./run_parity_benchmark.py --audio-dir benchmark/audio --audio-only
  ./run_parity_benchmark.py --audio-dir benchmark/audio --audio-only --no-stt-hints

Outputs benchmark/results/parity_<ts>.json (machine) and .md (for manual
grading with the paper's 6 classes).
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

GRADES = [
    "deceptively_wrong",
    "not_replying",
    "blatantly_wrong",
    "partial_incomplete",
    "correct_not_optimal",
    "correct",
]

EMBEDDING_MODEL_HINTS = ("l1", "embed", "embedding")

STT_TIMEOUT = 120


def resolve_working_server(server_url):
    """Probe the given URL, then WSL gateway / localhost fallbacks."""
    from urllib.parse import urlparse

    candidates = [server_url]
    parsed = urlparse(server_url)
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/v1"
    scheme = parsed.scheme or "http"

    if os.path.exists("/proc/version"):
        try:
            with open("/proc/version") as f:
                is_wsl = "microsoft" in f.read().lower()
            if is_wsl:
                with open("/proc/net/route") as rf:
                    for line in rf:
                        fields = line.strip().split()
                        if len(fields) > 2 and fields[1] == "00000000":
                            import socket
                            import struct

                            gw = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
                            candidates.append(f"{scheme}://{gw}{port}{path}")
                            break
                candidates.append(f"{scheme}://127.0.0.1{port}{path}")
        except Exception:
            pass

    for url in candidates:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/models", timeout=3) as resp:
                models = [m.get("id") for m in json.loads(resp.read())["data"]]
                print(f"[OK] LLM server at {url}. Models: {models}")
                return url, models
        except Exception:
            continue
    return None, []


def resolve_position(graph, spec):
    """Benchmark position spec -> PositionInfo. See $position_spec in the JSON."""
    from src.position import PositionInfo

    if not spec:
        return None

    if "poi" in spec:
        poi = next(p for p in graph.pois if p.name == spec["poi"])
        return PositionInfo(poi.coords, poi, poi.name, max_life=float("inf"))

    if "street" in spec:
        street = graph.streets[spec["street"]]
        edge = street.edges[len(street.edges) // 2]
        mid = (edge.node1.coords + edge.node2.coords) / 2
        return PositionInfo(mid, edge, edge.street, max_life=float("inf"))

    if "intersection" in spec:
        wanted = set(spec["intersection"])
        node = next(
            n for n in graph.nodes if wanted.issubset(set(n.adjacents_streets))
        )
        return PositionInfo(
            node.coords, node, node.get_llm_description(), max_life=float("inf")
        )

    if "node_index" in spec:
        node = graph.nodes[spec["node_index"]]
        return PositionInfo(
            node.coords, node, node.get_llm_description(), max_life=float("inf")
        )

    raise ValueError(f"Unknown position spec: {spec}")


def stub_guides(graph, recorded):
    """Replace Google-Routes-backed guidance with local no-op recorders."""

    def guide_to_poi(start, poi_index, street_by_street=True, route_index=0):
        recorded.append(
            {"call": "guide_to_poi", "poi_index": poi_index,
             "street_by_street": street_by_street}
        )

    def guide_to_destination(start, destination, street_by_street=True, route_index=0):
        recorded.append(
            {"call": "guide_to_destination", "destination": str(destination),
             "street_by_street": street_by_street}
        )

    graph.guide_to_poi = guide_to_poi
    graph.guide_to_destination = guide_to_destination


def extract_transcript(history, start_index):
    """Serializable view of the conversation appended since start_index."""
    out = []
    for msg in history[start_index:]:
        entry = {"role": msg.get("role")}
        content = msg.get("content")
        if content:
            entry["content"] = content
        for tc in msg.get("tool_calls") or []:
            out_tc = tc["function"] if "function" in tc else tc
            entry.setdefault("tool_calls", []).append(
                {"name": out_tc["name"], "arguments": out_tc["arguments"]}
            )
        out.append(entry)
    return out


def approx_tokens(text):
    return len(text) // 4


AUDIO_PLACEHOLDER = "(spoken -- listen to the attached audio)"


def ask_with_audio(llm, formatter, position, wav_b64):
    """Run one turn with the wav in place of the question text.

    Everything else is untouched: same system prompt, same instructions block,
    same tools, same multi-round loop. Only the ###Question### body becomes an
    input_audio part appended to the user turn.
    """
    original = formatter.get_user_message

    def with_audio(question, pos):
        message = original(AUDIO_PLACEHOLDER, pos)
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": str(message["content"])},
                {"type": "input_audio", "input_audio": {"data": wav_b64, "format": "wav"}},
            ],
        }

    formatter.get_user_message = with_audio
    try:
        return llm.ask(AUDIO_PLACEHOLDER, position)
    finally:
        formatter.get_user_message = original


def transcribe(stt_server, wav_path, hints):
    """POST a WAV to tools/macos_stt/stt_server.py; return (text, seconds)."""
    with open(wav_path, "rb") as f:
        wav = f.read()

    payload = json.dumps({
        "audio_b64": base64.b64encode(wav).decode(),
        "hints": hints,
    }).encode()
    request = urllib.request.Request(
        f"{stt_server}/transcribe",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(request, timeout=STT_TIMEOUT) as resp:
            text = (json.load(resp).get("text") or "").strip()
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error", "")
        except Exception:
            detail = e.reason
        raise RuntimeError(f"STT failed on {os.path.basename(wav_path)}: {detail}")

    return text, round(time.time() - t0, 2)


def word_error_rate(reference, hypothesis):
    """(errors, reference_word_count), ignoring case and punctuation.

    Levenshtein over words. Case is ignored because hints change capitalisation
    without changing what the LLM receives.
    """
    # [\w'] rather than [a-z0-9']: the ASCII-only class split "café" into "caf"
    # and scored a diacritic as a substitution -- on POI names, which is exactly
    # where the metric is supposed to be trustworthy.
    words = lambda s: re.findall(r"[\w']+", s.lower())  # noqa: E731
    r, h = words(reference), words(hypothesis)

    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(
                d[i - 1][j] + 1,
                d[i][j - 1] + 1,
                d[i - 1][j - 1] + (r[i - 1] != h[j - 1]),
            )

    return d[-1][-1], len(r)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.environ.get("LLM_BASE_URL", "http://localhost:8081/v1"))
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "l3"))
    parser.add_argument("--embed-model", default="l1")
    parser.add_argument("--benchmark", default=os.path.join(SCRIPT_DIR, "benchmark", "mapio_benchmark.json"))
    parser.add_argument("--prompt", default=os.path.join(SCRIPT_DIR, "res", "prompt_en.yaml"))
    parser.add_argument("--out-dir", default=os.path.join(SCRIPT_DIR, "benchmark", "results"))
    parser.add_argument("--k", type=int, default=8, help="candidates per question")
    parser.add_argument("--map", default=None, help="run only cases for this map")
    parser.add_argument("--case", default=None, help="run only this case id")
    parser.add_argument("--dry-run", action="store_true", help="build prompts, print sizes, no LLM calls")
    parser.add_argument("--audio-dir", default=None,
                        help="transcribe <turn_id>.wav from here instead of using "
                             "the written utterance; turns with no wav stay text")
    parser.add_argument("--audio-only", action="store_true",
                        help="with --audio-dir, skip turns that have no recording")
    parser.add_argument("--stt-server", default=os.environ.get("STT_SERVER", "http://localhost:11435"))
    parser.add_argument("--no-stt-hints", action="store_true",
                        help="omit POI names as contextual hints, to A/B the biasing")
    parser.add_argument("--formatter", choices=["curated", "full"], default="curated",
                        help="curated = L1 skeleton + top-k candidates (fits 8192); "
                             "full = MapIO's whole graph dump (needs ctx-size 16384)")
    parser.add_argument("--input", choices=["text", "audio"], default="text",
                        help="audio sends the wav to l3 instead of a transcript; "
                             "requires --audio-dir")
    parser.add_argument("--warmup", action="store_true",
                        help="prime the system-prompt prefix before the first turn, "
                             "so per-turn timings are warm")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="cap generation. Default is LLM's own: 768 locally, "
                             "1.4x the longest completion ever measured (551 on "
                             "NY-S2). At 29k context l3 generates ~2.1 tok/s, so "
                             "--formatter full wants this lower still")
    parser.add_argument("--label", default=None,
                        help="write results to <out-dir>/<label>/ instead of <out-dir>/")
    args = parser.parse_args()

    if args.input == "audio" and not args.audio_dir:
        sys.exit("[ERROR] --input audio needs --audio-dir")
    if args.input == "audio" and args.formatter == "curated":
        print("[WARN] curated + audio: L1 has no text to rank on, so candidates "
              "will be near-random. Measured: right answer, wrong POI index.")

    if args.audio_dir:
        args.stt_server = args.stt_server.rstrip("/")
        try:
            with urllib.request.urlopen(f"{args.stt_server}/health", timeout=10) as resp:
                print(f"[OK] STT server at {args.stt_server}: {json.load(resp)}")
        except Exception as e:
            sys.exit(f"[ERROR] STT server unreachable at {args.stt_server}: {e}")

    if any(h in args.model.lower() for h in EMBEDDING_MODEL_HINTS):
        sys.exit(f"[ERROR] '{args.model}' is an embeddings tier; use --model l3.")

    server, models = resolve_working_server(args.server)
    if server is None:
        sys.exit(f"[ERROR] No LLM server reachable from {args.server}")
    if models and args.model not in models:
        print(f"[WARN] '{args.model}' not in {models}; sending anyway.")

    os.environ["LLM_BASE_URL"] = server
    os.environ["LLM_MODEL"] = args.model

    with open(args.benchmark, encoding="utf-8") as f:
        bench = json.load(f)

    cases = bench["cases"]
    if args.map:
        cases = [c for c in cases if c["map"] == args.map]
    if args.case:
        cases = [
            c for c in cases
            if c["id"] == args.case
            or any(t["id"] == args.case for t in c.get("turns", []))
        ]
    if not cases:
        sys.exit("[ERROR] No cases match the filter.")

    from src.graph import Graph
    from src.llm.llm import LLM
    from src.llm.curated_formatter import CuratedPromptFormatter
    from src.llm.place_retrieval import PlaceRetrieval
    from src.llm.prompt_formatter import PromptFormatter

    results = []
    warmup_sec = None
    by_map = {}
    for case in cases:
        by_map.setdefault(case["map"], []).append(case)

    for map_name, map_cases in by_map.items():
        model_path = os.path.join(SCRIPT_DIR, "models", map_name, f"{map_name}.json")
        with open(model_path, encoding="utf-8") as f:
            map_data = json.load(f)

        graph = Graph(map_data["graph"], lambda *a, **k: None)
        guide_calls = []
        stub_guides(graph, guide_calls)

        # Contextual hints for the recogniser: the map's own POI names, which is
        # what mapio biases on. Only SFSpeechRecognizer acts on them.
        # Read these off graph.pois, not map_data -- Graph() pops "name" out of
        # the source dicts as it builds, leaving them present but nameless.
        stt_hints = [] if args.no_stt_hints else sorted(
            {poi.name for poi in graph.pois if poi.name}
        )

        if args.formatter == "full":
            # MapIO's original formatter: the whole graph in the system prompt,
            # no L1 step at all. Every POI index is in context, so retrieval
            # cannot miss -- but the prompt is ~11k and needs ctx-size 16384.
            formatter = PromptFormatter(args.prompt, graph)
        else:
            retrieval = PlaceRetrieval(server, model=args.embed_model)
            print(f"[{map_name}] building L1 index over {len(graph.pois)} POIs...")
            retrieval.build(map_name, graph.pois)
            formatter = CuratedPromptFormatter(args.prompt, graph, retrieval, k=args.k)

        context = {"name": map_data.get("name", map_name)}
        if isinstance(map_data.get("context"), dict):
            context.update({k: str(v) for k, v in map_data["context"].items()})

        # The system message used to be frozen here, by monkeypatching the
        # formatter, so that llm.reset() could not restamp datetime.now() into
        # the middle of it between cases. LLM does that itself now whenever it
        # is serving locally (LLM.freeze_system_prompt), which is the same
        # behaviour and is also what mapio.py gets. Rendered here only to size
        # it for the notes below.
        sys_tokens = approx_tokens(str(formatter.get_main_prompt(context)["content"]))
        print(f"[{map_name}] {args.formatter} system prompt ~{sys_tokens} tokens")
        if args.formatter == "full" and sys_tokens > 7000:
            print("[NOTE] this needs --ctx-size 16384 on the l3 instance; at 8192 "
                  "the server rejects the request with a 400 before allocating.")
        elif sys_tokens > 5000:
            print(f"[WARN] system prompt is large; little room for conversation")

        if args.dry_run:
            for case in map_cases:
                turns = case.get("turns") or [case]
                for turn in turns:
                    pos = resolve_position(graph, turn.get("position"))
                    msg = formatter.get_user_message(turn["utterance"], pos)
                    print(f"  {turn.get('id', case['id'])}: user turn ~{approx_tokens(str(msg['content']))} tokens")
            continue

        llm = LLM(args.prompt, context, formatter=formatter,
                  max_tokens=args.max_tokens)
        print(f"[{map_name}] max_tokens {llm.max_tokens}, "
              f"system prompt {'frozen' if llm.freeze_system_prompt else 'per-session'}")

        warmup_pending = args.warmup

        def warm_prefix():
            print(f"[{map_name}] warming the prefix...", flush=True)
            elapsed = llm.warm_up()
            print(f"[{map_name}] prefix warm in {elapsed}s")
            return elapsed

        for case in map_cases:
            llm.reset()
            # Warm AFTER reset(), never before. reset() rebuilds history from
            # the system message, and a prefix warmed against a message the
            # session then discards shares nothing with the turns that follow
            # (measured: 4205 tokens primed, cached=0 on the next turn).
            if warmup_pending:
                warmup_sec = warm_prefix()
                warmup_pending = False
            turns = case.get("turns") or [case]
            case_result = {"id": case["id"], "map": map_name, "turns": []}

            for turn in turns:
                turn_id = turn.get("id", case["id"])

                spoken = None
                wav_b64 = None
                utterance = turn["utterance"]
                if args.audio_dir:
                    wav_path = os.path.join(args.audio_dir, f"{turn_id}.wav")
                    if os.path.exists(wav_path):
                        if args.input == "audio":
                            with open(wav_path, "rb") as fh:
                                wav_bytes = fh.read()
                            wav_b64 = base64.b64encode(wav_bytes).decode()
                            spoken = {
                                "wav": os.path.relpath(wav_path, SCRIPT_DIR),
                                "reference_utterance": utterance,
                                "sent_as": "input_audio",
                                "seconds": round(len(wav_bytes) / 32000, 2),
                            }
                        else:
                            text, stt_sec = transcribe(args.stt_server, wav_path, stt_hints)
                            errors, length = word_error_rate(utterance, text)
                            spoken = {
                                "wav": os.path.relpath(wav_path, SCRIPT_DIR),
                                "reference_utterance": utterance,
                                "sent_as": "transcript",
                                "stt_sec": stt_sec,
                                "hints": len(stt_hints),
                                "wer_errors": errors,
                                "wer_words": length,
                                "wer_pct": round(100 * errors / length, 1) if length else None,
                            }
                            utterance = text
                    elif args.audio_only:
                        print(f"\n=== [{turn_id}] skipped, no recording")
                        continue

                pos = resolve_position(graph, turn.get("position"))
                guide_calls.clear()
                history_start = len(llm.history)

                if spoken and spoken["sent_as"] == "input_audio":
                    print(f"\n=== [{turn_id}] sending {spoken['seconds']}s of audio")
                    print(f"    reference: {spoken['reference_utterance']}")
                elif spoken:
                    print(f"\n=== [{turn_id}] heard ({spoken['stt_sec']}s, "
                          f"WER {spoken['wer_errors']}/{spoken['wer_words']}): {utterance}")
                    print(f"    reference: {spoken['reference_utterance']}")
                else:
                    print(f"\n=== [{turn_id}] {utterance}")

                usage_start = len(llm.usage)
                t0 = time.time()
                if wav_b64 is None:
                    answer = llm.ask(utterance, pos)
                else:
                    answer = ask_with_audio(llm, formatter, pos, wav_b64)
                elapsed = time.time() - t0

                if answer is None:
                    # Roll the failed turn back out of the history so one
                    # error (e.g. context overflow) doesn't cascade through
                    # the rest of a multi-turn session.
                    del llm.history[history_start:]

                usage = None
                if llm.usage and llm.usage[-1] is not None:
                    try:
                        usage = llm.usage[-1].model_dump()
                    except Exception:
                        usage = str(llm.usage[-1])

                print(f"--- answer ({elapsed:.1f}s): {answer}")
                case_result["turns"].append({
                    "id": turn_id,
                    "utterance": utterance,
                    "spoken": spoken,
                    "category": turn.get("category", case.get("category")),
                    "position": turn.get("position"),
                    "answer": answer,
                    "elapsed_sec": round(elapsed, 2),
                    "guide_calls": list(guide_calls),
                    "usage_last_round": usage,
                    "rounds": [
                        {
                            "prompt_tokens": u.prompt_tokens,
                            "completion_tokens": u.completion_tokens,
                            "cached_tokens": getattr(
                                getattr(u, "prompt_tokens_details", None),
                                "cached_tokens", None),
                        }
                        for u in llm.usage[usage_start:] if u is not None
                    ],
                    "transcript": extract_transcript(llm.history, history_start),
                    "grading_notes": turn.get("grading_notes", ""),
                    "grade": None,
                })

            results.append(case_result)

    if args.dry_run:
        return

    out_dir = os.path.join(args.out_dir, args.label) if args.label else args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(out_dir, f"parity_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": ts,
            "label": args.label,
            "server": server,
            "model": args.model,
            "k": args.k,
            "formatter": args.formatter,
            "input": args.input,
            "prompt": os.path.basename(args.prompt),
            "benchmark": os.path.basename(args.benchmark),
            "warmup_sec": warmup_sec,
            "stt_hints": not args.no_stt_hints,
            "grades": GRADES,
            "gpt4o_bar": "94.74% correct + 5.26% correct_not_optimal (paper Table 4, iteration 8, New York map)",
            "results": results,
        }, f, indent=2, default=str)

    md_path = os.path.join(out_dir, f"parity_{ts}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# MapIO parity run {ts}\n\nModel: `{args.model}` at `{server}`, k={args.k}\n\n"
                f"Grade each answer with one of: {', '.join(GRADES)}\n"
                f"(GPT-4o bar: 94.74% correct + 5.26% correct_not_optimal)\n")
        for case in results:
            for turn in case["turns"]:
                f.write(f"\n---\n\n## {turn['id']}  ({turn['category']}, {case['map']})\n\n")
                f.write(f"**Q:** {turn['utterance']}\n\n")
                if turn.get("spoken"):
                    s = turn["spoken"]
                    if s["sent_as"] == "input_audio":
                        f.write(f"**Sent** `{s['wav']}` ({s['seconds']}s) as audio, no STT\n\n")
                    else:
                        f.write(f"**Heard from** `{s['wav']}` in {s['stt_sec']}s, "
                                f"WER {s['wer_errors']}/{s['wer_words']} = {s['wer_pct']}% "
                                f"({s['hints']} hints)\n\n")
                    f.write(f"**Reference:** {s['reference_utterance']}\n\n")
                if turn["position"]:
                    f.write(f"**Position:** `{json.dumps(turn['position'])}`\n\n")
                calls = [
                    f"`{tc['name']}({tc['arguments']})`"
                    for m in turn["transcript"] for tc in m.get("tool_calls", [])
                ]
                if calls:
                    f.write("**Tool calls:** " + ", ".join(calls) + "\n\n")
                f.write(f"**A ({turn['elapsed_sec']}s):** {turn['answer']}\n\n")
                f.write(f"**Notes:** {turn['grading_notes']}\n\n")
                f.write("**Grade:** \n")

    spoken_turns = [
        t for c in results for t in c["turns"]
        if t.get("spoken") and t["spoken"]["sent_as"] == "transcript"
    ]
    if spoken_turns:
        errors = sum(t["spoken"]["wer_errors"] for t in spoken_turns)
        words = sum(t["spoken"]["wer_words"] for t in spoken_turns)
        stt_sec = sum(t["spoken"]["stt_sec"] for t in spoken_turns)
        print(f"\nSTT: {len(spoken_turns)} turns, WER {errors}/{words} = "
              f"{100 * errors / words:.1f}%, {stt_sec:.1f}s transcribing "
              f"({'no hints' if args.no_stt_hints else 'POI hints'})")
        for t in spoken_turns:
            if t["spoken"]["wer_errors"]:
                print(f"  {t['id']}: {t['spoken']['reference_utterance']}")
                print(f"  {' ' * len(t['id'])}  -> {t['utterance']}")

    print(f"\nSaved {json_path}\nSaved {md_path}")


if __name__ == "__main__":
    main()
