# JS arms — P-eval + 3w-eval

The MapIO parity benchmark run against the **JavaScript** stack in `starter/src/lib`,
landing in this tree beside the Python's arms so `compare_arms.py` and the grading flow
work unchanged. `browser-voice-exploration-plan.md` §6: *"It is the acceptance test for
**P**, and the reason the port should be platform-free."*

Verified: `compare_arms.py`'s `turns_by_id`, `calls`, `summarise` and its WER column all
read these files without modification.

## The arms

| folder | backend | model | words | isolates |
|---|---|---|---|---|
| `js_node_e4b/` | HTTP router | `l3` = Gemma 4 **E4B** | heard | **the baseline** — same model and same words as `arm1_curated_stt`, so any grade delta is the JS port |
| `js_node_e4b_text/` | HTTP router | E4B | clean | compares against `arm1_curated_stt_text`; with the row above, whether a gap is STT-caused |
| `js_browser_e2b/` | wllama in-tab | Gemma 4 **E2B** | heard | the model swap (E4B → E2B) and the runtime swap (llama-server → wllama) |
| `js_browser_e2b_text/` | wllama in-tab | E2B | clean | as above, on clean text |
| `js_node_e2b/` | HTTP router | E2B | heard | optional, and only if a second tier is mounted: separates *"E2B is weaker"* from *"in-tab differs from llama-server"* |

One variable per arm, following `run_arms.py`'s methodology.

## Running them

```sh
# baseline, both passes, one command
node starter/scripts/parity_js.mjs --arm js_node_e4b --input both \
    --server http://localhost:11434/v1

# no server needed: build every prompt, print sizes, call nothing
node starter/scripts/parity_js.mjs --dry-run

# the offline checks, including the parity pins against the recorded Python run
node starter/scripts/test_parity.mjs

# the in-tab arm
cd explore/wllama-spike && npm run serve
#   open http://localhost:8971/parity.html?auto=1&input=heard
node starter/scripts/parity_js.mjs --adopt explore/wllama-spike/results/<file>.json
```

## STT is replayed, not re-executed

Web Speech `SpeechRecognition` captures the default input device and accepts no file, no
`MediaStream` and no `AudioBuffer`, so the recorded WAVs cannot go through browser STT
without OS-level audio loopback. Every arm therefore replays what Apple's on-device
recogniser actually produced, out of
`arm1_curated_stt/parity_20260805_194151.json` — **18 cases / 26 turns**, 50 POI hints:

* `turns[].utterance` → the **heard** pass (real recognition errors intact)
* `turns[].spoken.reference_utterance` → the **clean** pass

The JS `wordErrorRate()` reproduces the Python's score exactly on all 26 turns
(**11/235 = 4.7 %**, zero per-turn disagreements), so the two arms are scored on the same
words by the same metric.

⚠️ The heard text is Apple's, not Chrome's. What WER a real browser session would produce
is measured nowhere yet.

## Read the header before you grade

Each run's `.md` opens with the arm, the backend, the STT provenance, and the
**harness-supplied tools**. Three of the nine tools served are supplied by
`src/lib/parity/harnessTools.js` rather than shipped in `src/lib/tools/`:

* `route_to` — without it `processInstructions` is never called and the acceptance test
  for **P** grades everything except the thing it accepts. It really routes and records
  the waypoints, which is what `--routing local` means on the Python side.
* `get_segment_accessibility`, `get_crossing_info` — `NY-A1`, `NY-A2` and `NY-S4` are
  per-segment and per-node feature reads. The data is in the model files and in the
  ported `Edge`/`Node`; only the handler was missing (M11/M13).

`set_route_preferences` and `stop_navigation` pass capability filtering, have no handler,
and are correctly **withheld** — no benchmark turn exercises them.

## Grades are post-hoc

Every turn is written with `"grade": null`. Grade the `.md` with the paper's six classes
and write `parity_<ts>_graded.md` beside it, exactly as for the Python arms. Nothing in
this pipeline reports an accuracy number.

GPT-4o bar: 94.74 % correct + 5.26 % correct_not_optimal (paper Table 4, iteration 8,
New York map).
