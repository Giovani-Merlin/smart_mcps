# Asking the human — shared format for brainstorm, plan, and deepen

The IDs (`R2`, `U3`, `g4`) are the documents' currency: the parser keys on
them and the verifier traces them. They are **never** a question's only
handle. The human answers from memory, at a glance, without reopening the
plan; every rule below exists to make that possible at near-zero token cost.

## 1. Handles — the document's own title line, verbatim, every time

A unit's handle **is its plan heading**, copied character for character:

```
U14. rerender-acceptance — six chapters once into artifacts-0911, three EPUBs, acceptance report with the judges' measures
```

Not `U14`, not `U14 rerender-acceptance`, not `U14 · rerender-acceptance ·
<your paraphrase>`. The heading already carries the id, the slug, and the
one-line goal the human wrote or approved; a paraphrase costs the same
tokens and makes the human translate. The same rule holds for every kind:

| kind                          | handle = the document's own line, verbatim                                                       |
| ----------------------------- | ------------------------------------------------------------------------------------------------ |
| requirement                   | the R-ID line as written: `R2. bundle-v2 — export writes a self-contained ingest/ package`         |
| unit                          | the `### U<N>. <slug> — <goal>` heading, minus the `###`                                          |
| group                         | `g4` **followed by the full heading of every member unit**, one per line — never `g4` alone        |
| decision (brainstorm, no IDs) | `Decision · <bold title>`                                                                          |

**There is no short form.** Every stem, every legend row, every "decisions
so far" entry, every summary line spells the full heading. Length is the
point: the human reads it once and knows exactly which unit is meant.
Only the header chip (§3) abbreviates, because the tool caps it.

Slugs are chosen once at plan time and reused verbatim in every later
phase — a unit that mainly implements an R-ID reuses that R-ID's tag as
its slug — so the heading a deepen question quotes is the heading the
plan shows.

## 2. Legend per batch, not per question

Immediately before each `AskUserQuestion` call, print in chat one line per
ID the batch mentions — the full heading, verbatim, as a list:

```
g4 — 2 units
- U3. package-writer — v2 export writes <run_dir>/ingest/ as a self-contained package
- U4. contract-doc — the Run Bundle v2 specification every consumer reads
```

Stems then **repeat the full heading** of the unit they are about. The
legend is for the group's shape, not a lookup table the stem points into.

## 3. Question shape

- **Header chip** (≤ 12 chars): ID + slug, truncated to fit — `U14 rerend`,
  `R2 bundle`, `Decision`. This is the only place an abbreviation is
  allowed, and only because the tool caps it. Never `Q2` or `Question`.
- **Stem**: `<full heading> · if wrong: <stakes>. <the question>` — the
  heading verbatim, then one clause naming what goes wrong if answered
  wrong ("if wrong: workers never see the file"). Add a one-line concrete
  scenario when the abstract version is hard to picture ("Scenario: a
  resumed run whose plan changed underneath").
- **Options**: label = the stance in ≤ 5 words; description = main effect
  plus the key downside, one line. Exactly one `(Recommended)` when we hold a
  view, listed first. An explicit `Either is fine` option whenever both
  readings are genuinely acceptable — recording it frees the constraint.
- **Batch by handle, order by stakes**: one unit's or one decision's
  questions travel together (the heading repeats, the context stays warm);
  highest blocking risk first, preferences last.

## 3b. Hard questions get a card first — say what we think, and why

A question is **hard** when its blocking risk is 2 or more, when the two
readings differ in *how a mechanism works* rather than in a parameter, or
when the stem needs a term the plan does not already use. The tool's
one-line option descriptions cannot carry that; a terse hard question is
the one the human cannot decode. So a hard question is preceded, **in chat,
immediately before the call**, by a card:

```
**U14. rerender-acceptance — six chapters once into artifacts-0911, three EPUBs, acceptance report with the judges' measures · hard**
Today: the acceptance report reads whatever EPUBs sit in artifacts-0911/ — it never checks they came from this run (render/accept.py:88).
The fork: A — re-render first, then score: always fresh, costs one full render (~40 min) even when nothing changed.
           B — score what is there and stamp the render's commit into the report: fast, but a stale EPUB scores silently if the stamp is ignored.
Our view: B. The plan's own U12 already writes the stamp, and the judges compare measures across runs, so a fresh render per acceptance buys nothing they use. Confidence: medium — we did not find who reads the stamp.
```

Four lines, each with a job:

- **Today** — what the code does *now*, one or two sentences, with the
  file:line it was read from. This is the ground the human stands on; a
  question with no "today" floats.
- **The fork** — each reading in plain words with **one concrete
  consequence** (a number, a file, a user-visible effect). Never "reading A
  is stricter": say what gets stricter and what it costs.
- **Our view** — which reading we favor and the evidence for it, naming the
  plan unit, code, or convention that tipped it. Then a confidence word
  (low / medium / high) and the one thing that would change our mind.
- Words: only terms the plan already uses or a first-year engineer knows.
  Every abstract noun ("the lifecycle", "the contract") gets one concrete
  instance in the same sentence.

**Self-check before sending**: could someone who has not opened the code
restate the fork in one sentence from the card alone? If not, rewrite the
card, not the question. The stem that follows the card is then short — the
card did the explaining — and the options' labels match the card's A / B
names so the human maps them at a glance.

Easy questions (a toggle, a name, a default value) skip the card; padding
them with one is the opposite failure.

## 4. Running "decisions so far"

Between question rounds, keep a one-line-per-decision list in chat, each
line opening with the full heading of the unit or requirement it settles
(`U3. package-writer — v2 export writes <run_dir>/ingest/ … → exponential,
3 attempts`; brainstorm: `Decision · retry policy → exponential, 3
attempts`). Later questions refer to entries by that heading. Brainstorm
has no IDs yet, so this list *is* its vocabulary; plan and deepen carry it
into the Decisions section.

## 5. Visuals — only when the question is relational

Decision rule:

- **Text only** when the question spans ≤ 2 handles or a single dimension
  (a toggle, a priority, a name, a parameter).
- **Preview box** (the tool's monospace markdown box — Mermaid does *not*
  render there) with a tiny table or ASCII flow when the question spans 3+
  handles and hinges on order, coverage, or lifecycle. Keep it ≤ 6 rows;
  inside the box the `U<N>. <slug>` prefix of the heading is enough, since
  the full headings sit in the legend just above.
- **Mechanism diagram** when the question is about *how a mechanism flows*
  and a wrong mental model on either side is likely: a Mermaid flowchart,
  sequence, or state diagram of the thing under discussion (a retry loop,
  a file's lifecycle, a request path). One relation type per diagram, ≤ 6
  nodes, always paired with one stakes sentence in text. Render it as an
  HTML page through the harness's artifact tool when one exists; otherwise
  write `.orchestrator/diagrams/<topic>.html` (Mermaid via CDN) and give the
  path. Keep **one page per topic or plan** and republish it with a new
  section as the discussion moves — never one page per question.
- **Never diagram the unit/group DAG.** The orchestrator and observatory
  already render it; a hand-drawn copy costs tokens and drifts.

A heavier review surface (an annotatable page the human redraws and sends
back) is for the case where the human needs to *correct our model*, not to
answer a question. Offer it, do not default to it.
