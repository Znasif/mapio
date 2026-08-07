# Recorded utterances

Real microphone audio for testing the STT path (and, end to end, STT -> L1 -> L3).

## Format

**16 kHz mono 16-bit PCM WAV.** Not MP3.

That is exactly what `STT.audio_to_text` sends —
`get_wav_data(convert_rate=16000, convert_width=2)` — so a file recorded this
way is byte-for-byte what mapio would POST. No resample step sits between the
test and the thing being tested, and no lossy codec artifacts confound an
accuracy comparison. Size is not a reason to compress: 5 s is 160 KB.

```bash
# macOS
ffmpeg -f avfoundation -i ":default" -ar 16000 -ac 1 -c:a pcm_s16le -t 6 NY-R4.wav -y

# Windows (list devices first: ffmpeg -list_devices true -f dshow -i dummy)
ffmpeg -f dshow -i audio="Microphone (Realtek Audio)" -ar 16000 -ac 1 -c:a pcm_s16le -t 6 NY-R4.wav -y
```

If something is already recorded at another rate, convert rather than re-record:

```bash
ffmpeg -i whatever.m4a -ar 16000 -ac 1 -c:a pcm_s16le NY-R4.wav -y
```

## Naming

**Use the turn IDs from `../mapio_benchmark.json`.** They already exist, they
already encode map and sequence, and matching them means a test script can join
audio to the reference utterance and to `grading_notes` with no extra bookkeeping.

```
NY-S1.wav  NY-S2.wav  NY-S3.wav  NY-S4.wav  NY-P1.wav
NY-R1.wav  NY-R2.wav  NY-R3.wav  NY-R4.wav  NY-R5.wav
NY-L1.wav ... NY-L6.wav   NY-A1.wav  NY-A2.wav  NY-N1.wav
DT-T2.wav ... DT-T10.wav
```

The initial/follow-up distinction you wanted is already there: the 19 `NY-*`
cases are single-turn, and `DT-SESSION` is one nine-turn conversation whose
turns run `DT-T2` through `DT-T10` in order. Recording those nine in sequence
gives you the multi-turn case.

Extra takes of the same utterance — different pace, distance, background noise:

```
NY-R4.wav          take 1, the default
NY-R4.take2.wav
NY-R4.quiet.wav    free-form suffix; anything after the first dot is a label
```

Utterances that are not in the benchmark go in `extra/` with a sidecar
transcript, since there is nothing to join against:

```
extra/poi-names-1.wav
extra/poi-names-1.txt    <- exact reference transcript, one line
```

## Why the IDs matter

For any file named after a benchmark turn, the reference transcript is
`turn["utterance"]` in `mapio_benchmark.json`. That makes two things automatic:

- **word error rate** against the reference, per file, with and without `--hints`
- **end-to-end runs** — feed the transcript straight into the existing pipeline
  and compare answers to a text-input run of the same turn, which isolates how
  much accuracy is lost to STT rather than to the model

Neither needs a manifest as long as the filenames match.

## Priority

If you are only recording a few, these carry the most signal:

- `NY-L5`, `NY-R3` — "nearest restaurant/bank", where hint biasing matters least
- `NY-R4`, `DT-T10` — "guide me" / "navigate me", where a misheard verb changes
  the tool call, not just the text
- `NY-L2` (Cafe China), `NY-N1` — POI names, where `--hints` should show up
- anything in `extra/` with hard names: Gammeeok, Stavros Niarchos, Solle Spa
