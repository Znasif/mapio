# LFM2.5-2.6B as a replacement for l3 — evaluation

**Verdict: no.** After tuning, it lands at 3.3x l3's wall clock and 20.5x its
answer length, with two distinct repetition loops and one intermittent defect
that crashes the app. The serving-level fixes were real and large; what remains
is not a setting.

Date: 2026-08-08. Machine: M1 Mac mini, 8 GB, `iogpu.wired_limit_mb=6144`.

---

## What was measured

`LiquidAI/LFM2.5-2.6B-GGUF:Q8_0` (2.87 GB) against `l3`
(`unsloth/gemma-4-e4b-it-qat-GGUF:UD-Q4_K_XL`, ~4.7 GiB), over the **DT session:
9 turns, detroit_conant**, `res/prompt_en_fixed.yaml`, curated formatter,
`--routing stub`, audio input through the on-device STT server.

Baseline is `arm1_curated_stt/`, restricted to the same 9 turn ids. l4 runs used
`LLM_MAX_TOKENS=2048`; l3's runs used the default 768, which it never approached
(its longest turn is 127 tokens).

Served from a separate router on :8082 with its own preset
(`~/.config/abtc/models-l4.ini`), so the live daemon on :8081 was never touched
and `l3` could not be woken by a stray request.

**Nothing here is graded.** No answer was scored against the rubric. Every
conclusion below is about latency, verbosity and robustness. Correctness is
unknown, and the verbosity finding is severe enough that it did not seem worth
grading first.

## Result

| configuration | wall (9 turns) | vs l3 | tokens | notes |
|---|---|---|---|---|
| l3 baseline | **217 s** | 1.0x | **609** | |
| l4, full graph, jinja, cap 768 | ~620 s | 2.9x | — | 5/9 answers empty |
| l4, full graph, jinja, cap 2048 | 1371 s | 6.3x | — | DT-T10: 18 rounds |
| l4, curated, jinja, cap 2048 | 1500 s | 6.9x | 9915 | DT-T10: 49 rounds, 1004 s |
| l4, curated, **no jinja** | 641 s | 3.0x | 9915 | loop gone |
| l4, curated, no jinja, **no reasoning**, ctx 12288 | 725 s | 3.3x | 7831 | DT-T7: 14 rounds |

## Findings

### 1. `--jinja` caused a 16x loop on the navigation turn

With the model's own chat template, DT-T10 issued **48 consecutive
`guide_to_point_of_interest` calls** across 49 rounds, cycling 4 argument
variants that differed only cosmetically (`2750.265722155371` -> `2750.27`,
`street_by_street` true -> false). Removing `jinja` — falling back to
llama.cpp's generic tool handler, which is what `models.ini` has always used for
l3 — reduced that turn to **2 rounds, 61.6 s**, and the session from 1500 s to
641 s.

The tool result was never at fault. `guide_to_poi` returns
"Navigation mode is now enabled." by design: the call hands control to
`NavigationController`, which delivers waypoints through TTS as the user's
finger moves. That is a complete acknowledgement, and `prompt_formatter.py:223`
returns it in canonical OpenAI form (`role`/`tool_call_id`/`content`). The model
simply could not see it through its own template.

**Carry-over:** do not enable `jinja` for l3 without measuring. It cost 16x on
the single most safety-relevant interaction in the app.

### 2. LFM2.5 is a reasoning model, and disabling it only relocates the tokens

llama.cpp returns its chain-of-thought in `message.reasoning_content`. On one
probe: **2,477 characters of thinking for a 319-character answer**, 579
completion tokens. Setting `reasoning = off` and `reasoning-budget = 0` cut that
probe to **21 tokens and 7.5 s** — a 27x reduction.

Over a real session it did not hold. Tool rounds did become lean (DT-T7 opens
23, 37, 42 tokens), but total tokens fell only 9915 -> 7831 and **wall clock
rose, 641 s -> 725 s**. The model moved its deliberation out of
`reasoning_content` and into the answer, where the user hears it.

This is the same trap `llm.py:68` documents for Gemma, which is why `[l3]` has
its CoT off-switch. l4 had simply never had that pass.

### 3. Answer verbosity is the disqualifying finding

Same prompt, same turns:

| turn | l3 | LFM2.5 | as speech @150 wpm |
|---|---|---|---|
| DT-T2 | 59 chars | 1,237 | 5 s -> **99 s** |
| DT-T6 | 43 chars | 1,446 | 3 s -> **116 s** |
| DT-T8 | 90 chars | 3,588 | 7 s -> **287 s** |
| DT-T7 | 149 chars | 4,778 | 12 s -> **382 s** |
| **total** | **840** | **17,208 (20.5x)** | |

DT-T7 asks the distance between two places. LFM2.5 answers with **six and a half
minutes of synthesized speech**, in markdown, with bold markers and raw
coordinates read aloud.

MapIO is a TTS instrument for blind users standing at a tactile map. A six-minute
answer is not slow, it is unusable — and the prompt already tells the model what
to do in four worked examples ("Then, tell me you have enabled street-by-street
navigation mode"). l3 obeys that in 43-149 characters. This is an
instruction-following gap, not a configuration one.

### 4. A second loop survives both fixes

DT-T7 ran 14 rounds with completion tokens `391, 391, 391, 391, 391, 391` —
six identical outputs consecutively, with jinja off and reasoning off.

### 5. Intermittent malformed tool-call JSON

One session died on `json.JSONDecodeError` parsing `tool_call.function.arguments`
(`Expecting ',' delimiter: line 1 column 47`). The re-run produced 0/9. Eight
targeted probes produced 8 valid calls. Roughly one session in two.

`prompt_formatter.py:152` parses arguments above the `try` that guards the rest
of the dispatch, so this ends a live session outright. **That line was left
unchanged deliberately**: across six `arm1_curated_stt*` runs, ~160 turns, l3 has
never tripped it. Hardening it would convert a model defect into a handled edge
case and let a model that cannot reliably emit parseable JSON look shippable.
The runner records it instead (`malformed_tool_call` per turn, with the raw
argument string) so the rate is measurable.

## Measurements worth keeping

**l1 costs nothing per turn.** One query embedding, warm: **21 ms** (126 ms on
the first call after load), against turns of 15-60 s — 0.03-0.14%. The index is
built once and cached; `top_k` embeds a single short string.

So dropping l1 for a full-graph prompt *spends* time rather than saving it:
2.5-4x the context (5.5-7.9K -> 18.7K detroit, ~29K new_york) charged against
attention on every generated token, to avoid 21 ms and 318 MB. The case for
removing l1 is architectural simplicity — one fewer model to install and
supervise — never throughput.

**Decode rate favours LFM2.5 heavily.** 15.5 tok/s against l3's 2.8 tok/s
effective (5.5x). It loses on wall clock purely on volume. A less verbose model
of this size and speed would be genuinely attractive.

**Full-graph prompt sizes:** detroit_conant **18,717 tokens** — above the 16384
that arms 2 and 3 were run at. new_york is ~29K.

## Operational notes

- **Two tiers at `ctx-size 32768` overflow the Metal budget**
  (`kIOGPUCommandBufferCallbackErrorOutOfMemory`), and the backend does not
  recover — the router needs restarting. Pass `--models-max 1` when A/B-ing
  tiers on this machine; `start-ai` sets 2 and the later value wins.
- **`slot-save-path` must already exist.** `start-ai` creates only its own
  (`~/.cache/abtc-kv`), so a new tier pointing elsewhere fails to load with a
  message about the directory, not the model. Worth folding into the packaged
  launcher.
- `ctx-size 12288` is right for the curated arm — prompts top out near 7.9K.
  32768 was sized for the full-graph arm and allocated KV that went unused.

## What this left behind

Useful regardless of the verdict:

- **arm 5** (`arm5_lfm_curated`) — curated formatter with the chat model swapped.
  arm5 - arm1 is what a model is worth; arm5 - arm4 is what the context strategy
  is worth. Arms 1-4 could not separate those.
- **`--model` override** in `run_arms.py` — run any arm against a differently
  served tier without duplicating the arm definition.
- **`malformed_tool_call`** recording in `run_parity_benchmark.py`.
- **`~/.config/abtc/models-l4.ini`** — tiers `l4`, `l4nj`, `l4nr`, `l1` on a
  separate port, with no `l3` alias so an evaluation cannot wake it.

## If someone picks this up again

The one untried lever that could change the verdict is a **brevity-constrained
prompt variant** — the machinery exists (`prompt_en_v2`/`v3` and their result
folders). It would need l3 re-run and re-graded against the same variant to stay
comparable, and it is testing whether a model can be prompted out of ignoring
instructions it has already been given four times.

**Q4_K_M is untested.** It should improve decode rate proportionally (1.67 GB
against 2.87 GB, decode being bandwidth-bound) and free ~1.2 GB of the Metal
budget, which may be a prerequisite for the full-graph cell running at all on
8 GB. It cannot shorten a 4,778-character answer.
