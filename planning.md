# Provenance Guard — Planning

## Architecture

```
                         SUBMISSION FLOW
                         ───────────────
   client
     │  POST /submit {text, creator_id}
     ▼
 ┌─────────────┐
 │ Flask route │
 │  /submit    │
 └──────┬──────┘
        │ raw text
        ▼
 ┌─────────────────────┐        ┌──────────────────────────┐
 │ Signal 1: Groq LLM   │        │ Signal 2: Stylometric     │
 │ classification       │        │ heuristics (pure Python)  │
 │ → ai_probability 0-1 │        │ → ai_likeness 0-1         │
 └──────────┬───────────┘        └────────────┬─────────────┘
            │ llm_score                        │ stylo_score
            └───────────────┬──────────────────┘
                             ▼
                   ┌────────────────────┐
                   │ Confidence Scoring │
                   │ 0.6*llm + 0.4*stylo│
                   │ → combined_score   │
                   └──────────┬─────────┘
                              │ combined_score (0-1)
                              ▼
                   ┌────────────────────┐
                   │ Label Generation   │
                   │ threshold bands    │
                   │ → attribution +    │
                   │   label text       │
                   └──────────┬─────────┘
                              │ content_id, attribution,
                              │ confidence, label, signals
                              ▼
                   ┌────────────────────┐
                   │  Audit Log (SQLite)│  ← writes structured entry
                   └──────────┬─────────┘
                              ▼
                        JSON response to client


                          APPEAL FLOW
                          ───────────
   client
     │ POST /appeal {content_id, creator_reasoning}
     ▼
 ┌─────────────┐
 │ Flask route │
 │  /appeal    │
 └──────┬──────┘
        │ look up content_id in store
        ▼
 ┌─────────────────────────────┐
 │ Update submission status    │
 │  → "under_review"           │
 └──────────────┬──────────────┘
                │ appeal record (content_id, reasoning, timestamp)
                ▼
 ┌─────────────────────────────┐
 │ Audit Log (SQLite)          │  ← appends appeal entry alongside
 │                              │     original decision
 └──────────────┬──────────────┘
                ▼
          JSON confirmation to client
```

**Submission flow narrative:** a submitted text is sent to two independent
signal functions — a Groq LLM judgment (semantic/holistic) and a stylometric
heuristics function (structural/statistical). Their outputs are combined into
a single 0–1 confidence score, which is mapped through threshold bands into
an attribution result and one of three transparency label texts. Every step
of this is written to a structured SQLite audit log before the response is
returned to the client.

**Appeal flow narrative:** a creator submits a `content_id` and their
reasoning; the system looks up the original submission, flips its status to
`"under_review"`, and appends an appeal entry to the same audit log so the
appeal sits alongside the original decision. No re-classification happens
automatically — a human reviewer is expected to read the appeal queue
(`GET /log`) and act on it manually.

## API Surface

- `POST /submit` — body: `{"text": str, "creator_id": str}` → returns
  `{content_id, creator_id, attribution, confidence, llm_score, stylo_score, label, status}`
- `GET /log` — returns `{"entries": [...]}`, most recent first
- `POST /appeal` — body: `{"content_id": str, "creator_reasoning": str}` →
  returns `{content_id, status: "under_review", message}`

## Detection Signals

**Signal 1 — LLM-based classification (Groq `llama-3.3-70b-versatile`).**
Measures: holistic semantic/stylistic coherence — does the text read like
something an LLM would produce (uniform tone, hedging phrases like "it is
important to note," even-handed listing of tradeoffs, absence of a
distinctive voice)? The model is prompted to return a JSON object
`{"ai_probability": 0.0-1.0, "reasoning": "..."}`. Output: a float 0–1,
`ai_probability`, taken directly as `llm_score`.
Blind spot: an AI model judging AI-ness is itself pattern-matching on
surface style; it can be fooled by heavily-edited AI text or confused by
human writing that happens to be very polished/formal (e.g. academic,
technical writing), and it has no ground truth — it's another guess, not
verification.

**Signal 2 — Stylometric heuristics (pure Python, no external libraries).**
Measures three structural/statistical properties known to differ between
human and AI writing:
1. *Sentence length variance* (coefficient of variation of sentence lengths
   in words). Human writing tends to mix short and long sentences; AI text
   is often more uniform. Lower variance → more AI-like.
2. *Type-token ratio* (unique words / total words). Very low diversity for
   the text's length suggests repetitive, formulaic phrasing common in AI
   output.
3. *Informality markers* (contractions, first-person casual pronouns,
   ellipses, multiple exclamation marks, lowercase sentence starts). Their
   presence pushes toward "human"; their absence toward "AI."
Each metric is normalized to 0–1 and combined with fixed weights
(0.4 sentence-variance, 0.3 type-token ratio, 0.3 informality) into a single
`stylo_score` (0–1, where 1 = most AI-like).
Blind spot: pure statistics have no notion of meaning — a short, choppy,
low-vocabulary human piece (a terse diary entry, a poem built on repetition)
can score as "AI-like" on structure alone, and a careful human writer who
naturally writes in long, uniform, formal sentences can too.

**Combining signals:** `combined_score = 0.6 * llm_score + 0.4 * stylo_score`.
The LLM signal is weighted higher because it captures semantic content the
heuristics structurally cannot, but the heuristic signal still meaningfully
shifts the score — the two frequently disagree, and that disagreement is
itself useful (logged as both individual scores).

## Uncertainty Representation

`combined_score` (0–1) *is* the confidence score returned to the client: it
represents "how strongly the evidence points to AI authorship." Because a
**false positive (calling a human AI-generated) is worse than a false
negative** on a creative-writing platform, the thresholds are deliberately
asymmetric — it takes more evidence to call something AI than to call it
human:

| Range | Attribution | Label |
|---|---|---|
| `score >= 0.72` | `likely_ai` | High-confidence AI |
| `0.35 < score < 0.72` | `uncertain` | Uncertain |
| `score <= 0.35` | `likely_human` | High-confidence human |

This produces a 0.37-wide "uncertain" band biased toward *not* accusing a
human of using AI — the gap from the midpoint (0.5) to the AI threshold
(0.22) is wider than the gap to the human threshold (0.15), so it takes
more evidence to call something AI than to call it human. A 0.73 and a
0.95 both say "likely AI" but the label text always echoes the raw
percentage so the two are visibly different in strength, and anything
under 0.72 falls back to "uncertain" rather than a weak AI accusation.

## Transparency Label Design

Exact text returned by the system (`{pct}` is the confidence score as a
whole-number percentage, e.g. `82`):

- **High-confidence AI** (`score >= 0.72`):
  `"⚠️ Likely AI-Generated: Our system found strong signals — from both language-model review and writing-style analysis — that this content was generated by AI ({pct}% confidence). If you believe this is a mistake, you can appeal this classification."`
- **High-confidence human** (`score <= 0.35`):
  `"✅ Likely Human-Written: Our system found this content consistent with typical human writing patterns, based on language-model review and writing-style analysis ({pct}% AI-likelihood)."`
- **Uncertain** (`0.35 < score < 0.72`):
  `"❓ Uncertain Origin: Our system could not confidently determine whether this content is AI-generated or human-written — the signals were mixed ({pct}% AI-likelihood). This content is shown without a definitive attribution claim."`

## Appeals Workflow

- Any creator can submit an appeal on their own `content_id`.
- They provide: `content_id` and `creator_reasoning` (free text explaining
  why they believe the classification is wrong).
- On receipt, the system: looks up the submission by `content_id`, sets its
  status to `"under_review"`, and writes a new audit-log entry recording the
  appeal (`content_id`, `creator_reasoning`, timestamp, and a reference back
  to the original decision).
- No automated re-classification occurs.
- A human reviewer opening the appeal queue (`GET /log`, filtered to entries
  with `event: "appeal"` or status `under_review`) would see: the original
  text's `content_id`, the original attribution/confidence/signal scores,
  the creator's reasoning, and when the appeal was filed — enough context to
  manually re-evaluate without re-reading the whole audit log.

## Anticipated Edge Cases

1. **Very short text (under ~2 sentences).** Sentence-length variance and
   type-token ratio are statistically meaningless with so little data — the
   stylometric signal effectively becomes noise, and the system leans almost
   entirely on the LLM signal without flagging that it's doing so.
2. **Repetitive, simple-vocabulary creative writing** (e.g. a children's
   poem, a mantra-style piece, song lyrics built on refrains). Low
   type-token ratio and low sentence-length variance are hallmarks of the
   *stylometric* "AI-like" signature, but here they're deliberate artistic
   choices — this is a known false-positive risk for signal 2 specifically.
3. **Non-native or ESL human writing.** Simpler sentence structure and more
   uniform phrasing (from writing in a non-native language) can resemble the
   structural patterns the stylometric signal associates with AI text.

## AI Tool Plan

- **M3 (submission endpoint + first signal):** Provide the AI tool the
  "Detection Signals" (signal 1 only) section + the architecture diagram.
  Ask for: a Flask app skeleton with a `POST /submit` stub, and the
  `llm_signal(text)` function calling Groq and returning `{ai_probability,
  reasoning}`. Verify by calling `llm_signal()` directly on 2–3 hand-picked
  strings and checking the returned float is in `[0,1]` and reasoning is
  sane, before wiring it into the route.
- **M4 (second signal + confidence scoring):** Provide "Detection Signals"
  (signal 2) + "Uncertainty Representation" + the diagram. Ask for: the
  `stylo_signal(text)` function and a `combine_scores(llm_score,
  stylo_score)` function. Verify the combine function's output matches the
  documented `0.6/0.4` weighting and the threshold table exactly (print
  scores for the 4 test paragraphs in the spec and confirm clearly-AI text
  scores high, clearly-human text scores low).
- **M5 (production layer):** Provide "Transparency Label Design" +
  "Appeals Workflow" + the diagram. Ask for: a `generate_label(score)`
  function and the `POST /appeal` route + SQLite storage helpers. Verify by
  calling `generate_label()` at 0.10, 0.50, 0.90 and diffing the output
  against the exact strings written above (not paraphrases), then curling
  `/appeal` and confirming `GET /log` shows `status: under_review`.

## Rate Limiting (chosen values, documented further in README)

`10 per minute; 100 per day` on `POST /submit` — a real creator submitting
their own work rarely posts more than a handful of pieces in a sitting, so
10/minute comfortably covers normal use while making a scripted flood
(dozens of requests/second) immediately hit 429s. The 100/day cap bounds
sustained abuse from a single IP across a whole day without punishing a
prolific but legitimate user.
