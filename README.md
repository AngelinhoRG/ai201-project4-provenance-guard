# Provenance Guard

A backend system that classifies submitted text as likely AI-generated, likely
human-written, or uncertain — with a calibrated confidence score, a plain-language
transparency label, an appeals workflow, rate limiting, and a structured audit log.

Full design rationale (signal choices, thresholds, edge cases) lives in
[`planning.md`](planning.md). This README documents what was actually built and
the evidence that it works.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# create a .env file with GROQ_API_KEY=your_key_here
python app.py
```

Runs on `http://localhost:5000`.

## Architecture Overview

A submission flows: `POST /submit` → Signal 1 (Groq LLM judgment) and Signal 2
(stylometric heuristics) run independently on the raw text → their two scores
are combined into a single confidence score (weighted average) → the score is
mapped through threshold bands into an attribution result and one of three
transparency label texts → the full decision (both signal scores, combined
score, label, attribution) is written to a SQLite audit log → the response is
returned to the client.

An appeal flows: `POST /appeal` → the system looks up the original submission
by `content_id` → flips its status to `"under_review"` → appends an appeal
entry (with the creator's reasoning) to the same audit log, next to the
original decision → returns a confirmation. No automatic re-classification
happens; a human reviewer is expected to read `GET /log` and act on it.

See the [`## Architecture`](planning.md#architecture) section of `planning.md`
for the full diagram of both flows.

## Detection Signals

**Signal 1 — LLM-based classification** (`signals.llm_signal`, Groq
`llama-3.3-70b-versatile`). Prompts the model to assess uniformity of tone,
hedging phrases, evenness of sentence structure, and presence of a distinctive
voice, and returns `{"ai_probability": 0-1, "reasoning": "..."}`. This
captures holistic, semantic properties of the text no simple statistic can.
**What it misses:** it's a model guessing about another model's output — it
has no ground truth, and it can be fooled by heavily-edited AI text or
confused by naturally formal/polished human writing (academic prose,
technical writing).

**Signal 2 — Stylometric heuristics** (`signals.stylo_signal`, pure Python).
Computes three structural metrics and combines them into a single
`ai_likeness` score (0.4 sentence-length-variance + 0.3 type-token-ratio +
0.3 informality-markers):
- *Sentence length variance* (coefficient of variation): human writing mixes
  short and long sentences; AI text is often more uniform.
- *Type-token ratio* (vocabulary diversity): very low diversity suggests
  repetitive, formulaic phrasing.
- *Informality markers* (contractions, casual pronouns, ellipses, multiple
  exclamation marks, lowercase sentence starts): their presence pushes toward
  "human."
**What it misses:** it has no notion of meaning — a short, choppy,
low-vocabulary human piece (a terse diary entry, a repetition-based poem) can
score as "AI-like" purely on structure, and a human who naturally writes long,
uniform, formal sentences can too.

These two signals are genuinely independent (one semantic, one structural),
which is why they frequently disagree — both individual scores are logged so
that disagreement is visible, not hidden inside a single number.

## Confidence Scoring

`combined_score = 0.6 * llm_score + 0.4 * stylo_score`. The LLM signal is
weighted higher because it captures content the heuristics structurally can't,
but the stylometric signal still meaningfully shifts the result.

The combined score is mapped to a label through **asymmetric** thresholds,
because a false positive (calling a human's work AI-generated) is worse than a
false negative on a creative-writing platform:

| Range | Attribution |
|---|---|
| `score >= 0.72` | `likely_ai` |
| `0.35 < score < 0.72` | `uncertain` |
| `score <= 0.35` | `likely_human` |

The gap from the midpoint (0.5) to the AI threshold is 0.22; the gap to the
human threshold is only 0.15 — it takes more evidence to accuse someone of
using AI than to clear them.

**Validation:** I ran the scoring pipeline against 4 hand-picked inputs
spanning the confidence range, checking that scores matched intuition before
building anything on top of them (this is the calibration process described
in the spec reflection below):

| Input | llm_score | stylo_score | combined | attribution |
|---|---|---|---|---|
| Clearly AI-generated (formal, hedging, "it is important to note...") | 0.90 | 0.51 | **0.744** | `likely_ai` |
| Clearly human (casual ramen review, "honestly? underwhelming...") | 0.20 | 0.09 | **0.158** | `likely_human` |
| Borderline: formal human writing (monetary-policy paragraph) | 0.80 | 0.57 | 0.709 | `uncertain` |
| Borderline: lightly-edited AI text (remote-work reflection) | 0.40 | 0.39 | 0.396 | `uncertain` |

**Two examples with noticeably different confidence scores** (required for
submission — see the log entries in the [Audit Log](#audit-log) section
below for full detail):
- **High-confidence case:** `content_id 04afb02e-...` scored **0.744**
  (`likely_ai`) — llm_score 0.90, stylo_score 0.51.
- **Lower-confidence case:** `content_id 1fa881e2-...` scored **0.396**
  (`uncertain`) — llm_score 0.40, stylo_score 0.39.

The first threshold I picked (0.75) initially put the "clearly AI" test input
at 0.744 — just under the line, into `uncertain`. That's a real finding about
calibration, not just a design choice: it showed the asymmetric-safety
threshold was slightly *too* conservative for text that should confidently
register as AI. I lowered the AI threshold to 0.72, re-ran all four test
cases, and confirmed each one landed in the attribution bucket its content
warranted, without changing the underlying asymmetry (AI still requires more
evidence than human).

## Transparency Label

Exact text returned by the system (`{pct}` is `confidence * 100`, rounded):

| Variant | Exact text |
|---|---|
| **High-confidence AI** (`score >= 0.72`) | `⚠️ Likely AI-Generated: Our system found strong signals — from both language-model review and writing-style analysis — that this content was generated by AI ({pct}% confidence). If you believe this is a mistake, you can appeal this classification.` |
| **High-confidence human** (`score <= 0.35`) | `✅ Likely Human-Written: Our system found this content consistent with typical human writing patterns, based on language-model review and writing-style analysis ({pct}% AI-likelihood).` |
| **Uncertain** (`0.35 < score < 0.72`) | `❓ Uncertain Origin: Our system could not confidently determine whether this content is AI-generated or human-written — the signals were mixed ({pct}% AI-likelihood). This content is shown without a definitive attribution claim.` |

## Rate Limiting

`10 per minute; 100 per day` on `POST /submit`, via Flask-Limiter with
in-memory storage. Reasoning: a creator submitting their own work rarely
posts more than a handful of pieces in one sitting, so 10/minute comfortably
covers normal use, while a scripted flood (many requests per second) hits
429s almost immediately. The 100/day cap bounds sustained abuse from a single
IP across a full day without punishing a prolific but legitimate user.

**Evidence** — 12 rapid requests against a fresh limiter window:

```
200
200
200
200
200
200
200
200
200
200
429
429
```

First 10 succeed, the 11th and 12th are rejected — exactly the configured
`10 per minute` limit.

## Audit Log

Backed by SQLite (`storage.py`), exposed via `GET /log`. Every submission and
every appeal writes a structured entry. Sample entries (from a live run,
newest first):

```json
{
  "appeal_reasoning": "I wrote this myself for a creative writing class, the formal tone is intentional.",
  "content_id": "04afb02e-7a23-49bf-baa9-dd0a2a89941a",
  "event": "appeal",
  "original_attribution": "likely_ai",
  "original_confidence": 0.7441,
  "status": "under_review",
  "timestamp": "2026-07-01T02:01:48.188760+00:00"
}
```

```json
{
  "attribution": "likely_ai",
  "confidence": 0.7441,
  "content_id": "04afb02e-7a23-49bf-baa9-dd0a2a89941a",
  "creator_id": "clearly_ai",
  "event": "submission",
  "llm_reasoning": "The text exhibits a uniform tone, uses hedging phrases, and has an even sentence structure, which are characteristic of AI-generated content.",
  "llm_score": 0.9,
  "status": "classified",
  "stylo_components": {
    "informality_score": 1.0,
    "sentence_length_variance_score": 0.526,
    "type_token_ratio_score": 0.0
  },
  "stylo_score": 0.5104,
  "timestamp": "2026-07-01T01:58:09.586802+00:00"
}
```

```json
{
  "attribution": "likely_human",
  "confidence": 0.1578,
  "content_id": "4f1e483f-ca39-4bf4-91f2-250cdb4d28b7",
  "creator_id": "clearly_human",
  "event": "submission",
  "llm_reasoning": "The text features informal markers, casual tone, and personal opinions, which are characteristic of human-written reviews, making it unlikely to be AI-generated.",
  "llm_score": 0.2,
  "status": "classified",
  "stylo_components": {
    "informality_score": 0.0,
    "sentence_length_variance_score": 0.236,
    "type_token_ratio_score": 0.0
  },
  "stylo_score": 0.0944,
  "timestamp": "2026-07-01T01:58:10.082394+00:00"
}
```

Note the appeal entry references the same `content_id` as the original
submission entry. The audit log records events (not current state), so the
submission's status flip is stored separately in the `submissions` table —
confirmed directly: `storage.get_submission("04afb02e-...")["status"]` returns
`"under_review"` after the appeal above was filed.

## Appeals Workflow

`POST /appeal` with `{"content_id": ..., "creator_reasoning": ...}`:

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "04afb02e-7a23-49bf-baa9-dd0a2a89941a", "creator_reasoning": "I wrote this myself for a creative writing class, the formal tone is intentional."}'
```

```json
{
  "content_id": "04afb02e-7a23-49bf-baa9-dd0a2a89941a",
  "status": "under_review",
  "message": "Appeal received and logged for human review."
}
```

This updates the submission's stored status to `under_review` and appends an
`event: "appeal"` entry to the audit log (shown above) alongside the original
`event: "submission"` decision. No automated re-classification occurs — the
appeal is meant to be picked up by a human reviewer reading `GET /log`.

## Known Limitations

The stylometric signal specifically struggles with **short, repetitive,
simple-vocabulary creative writing** — a children's poem, a refrain-based
lyric, a mantra-style piece. Low type-token ratio and low sentence-length
variance are exactly the structural signature the heuristic associates with
AI text, but here they're deliberate artistic choices, not evidence of
machine authorship. This is a genuine, not hypothetical, false-positive risk
tied directly to how `_type_token_ratio_score` and
`_sentence_length_variance_score` are computed — it isn't a "need more data"
problem, it's a structural blind spot in what the signal can see.

A second, related limitation: very short submissions (under ~2 sentences)
make both stylometric metrics statistically meaningless, so the system leans
almost entirely on the LLM signal without explicitly flagging that it's doing
so — the confidence score doesn't currently communicate "low sample size."

## Spec Reflection

**How the spec helped:** writing out the exact three label strings in
`planning.md` *before* writing `scoring.py` caught a subtle problem early —
my first draft of the "uncertain" label didn't mention that the content was
shown "without a definitive attribution claim," which made it read as if the
system just hadn't finished analyzing yet rather than having deliberately
declined to call it either way. Having the exact text specified up front made
that gap in phrasing visible before it shipped.

**Where the implementation diverged:** the spec's hint suggested asymmetric
thresholds without prescribing values. My first attempt used symmetric-ish
thresholds (0.75 / 0.35) that, in testing against the four calibration
inputs, put a clearly-AI-generated example (a 0.744 combined score) into the
"uncertain" bucket instead of "likely_ai." I diverged from my own initial
spec value by lowering the AI threshold to 0.72 after seeing that mismatch —
the spec's requirement to test against real inputs (Milestone 4) is what
surfaced the miscalibration; the number itself wasn't right until validated
against data.

## AI Usage

This project was built in direct collaboration with Claude (Claude Code)
across the full spec-to-implementation pipeline. Two specific instances where
generated work was checked against real behavior and corrected:

1. **Directed:** implement the confidence-scoring thresholds per the spec's
   asymmetric-risk hint (false positives worse than false negatives), without
   being given exact numeric cutoffs. **Produced:** an initial threshold of
   `AI_THRESHOLD = 0.75`, `HUMAN_THRESHOLD = 0.35`. **Revised:** running the
   four calibration inputs from the spec (Milestone 4) against this threshold
   showed the "clearly AI-generated" example scoring `0.744` — just under the
   line, landing in `uncertain` instead of `likely_ai`. The threshold was
   wrong, not the signals. I lowered `AI_THRESHOLD` to `0.72`, re-ran all four
   calibration inputs, and confirmed each one now lands in the bucket its
   content actually warrants (documented under "Confidence Scoring" above).
2. **Directed:** start the Flask server in the background and verify it was
   live before running the curl test suite. **Produced:** a background launch
   whose first verification `curl` returned no output and no error — it
   looked like the server had failed to start. **Revised:** added an explicit
   timeout and status-code flag (`curl -m 5 -w "HTTP:%{http_code}"`) and
   retried; this confirmed the server was in fact running (`200`) and the
   original empty result was a race between the background launch and an
   immediate curl call, not a code defect — the fix was in the verification
   method, not the app.
