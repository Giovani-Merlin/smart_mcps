# Strategy Interview

Loaded by `SKILL.md` at the start of Phase 1 and revisited per-section in
Phase 2. Every section below maps one-to-one to a section in
`strategy-template.md`.

For each section: ask the opening question, evaluate the answer against the
quality bar, push back when it falls into a named anti-pattern, and capture
the final answer in the user's own language.

## Overall Rules

1. **Ask, don't prescribe.** Do not offer menu options for open answers
   (problem, approach, persona). Use free-form responses. Reserve
   single-select for routing decisions.
2. **Batch what doesn't depend on itself.** Group opening questions (and
   pushback follow-ups) that don't need each other's answer into one
   `AskUserQuestion` call, up to 4 per call. Serialize only when a later
   question's framing genuinely needs an earlier answer. Every question
   carries a short explanation of what it's for or what it trades off — not
   just the bare question stem.
3. **Push back once, maybe twice.** If the first answer is weak, name the
   specific issue and ask a sharper question. If the second answer is still
   weak, capture what the user has given and note in the draft that the
   section is worth revisiting. Do not let the interview spiral.
4. **Quote the user back at them.** When challenging an answer, use the
   user's own words verbatim. Paraphrasing softens the challenge and is
   easier to dismiss.
5. **Keep each answer to 1-3 sentences.** Longer answers are usually hiding
   something vague. If the user writes a paragraph, ask them to pick the
   sentence that matters most.
6. **Don't leak the anti-pattern names.** The user does not need to hear
   "that's a vanity metric" — just ask the sharper question that follows.

______________________________________________________________________

## 1. Target Problem

**Opening question:** "What's the core problem this solves — and what makes
that problem hard?"

Strong answers name a specific situation the target user (or downstream
consumer) is in, identify what makes the situation hard *right now* (a crux,
a constraint, something that isn't easy to route around), and are falsifiable
— you could imagine the problem being absent and know the difference.

**Anti-patterns and pushback:**

- **Goal stated as problem** ("the problem is we need more adoption") -> "That's
  a goal, not a problem. What's in the world that's making that goal hard to
  achieve? Whose situation are you changing?"
- **Vague wish** ("people need better tooling for X") -> "Whose situation
  specifically? Doing what? What do they try today, and why doesn't it work?"
- **Symptom, not cause** ("users stop after the first session") -> "That's a
  symptom. What's happening in their world that makes them stop caring?
  What's the underlying condition?"
- **Too broad** ("developer productivity is broken") -> "That's a
  civilization-scale problem. Narrow it to a situation you can actually
  affect — which users, doing what, when does it hurt most?"
- **Feature-shaped** ("there's no good way to do [specific workflow]") ->
  "That's a missing feature, not the underlying problem. What outcome does
  the user want that the feature would give them?"

**Capture:** One or two sentences naming the user's (or consumer's) situation
and the crux. No solution language.

______________________________________________________________________

## 2. Our Approach

**Opening question:** "Given that problem, what's your approach — the
commitment or principle that makes it tractable?"

This is the guiding choice: how the thing competes or operates, so that many
downstream decisions become easier. It is not the thing itself and it is not
a feature list.

Strong answers are a choice (implying alternatives explicitly *not*
pursued), are general enough to direct many decisions but specific enough to
rule things out, and sound more like "we win by [doing X differently]" than
"we do [a list of things]".

**Anti-patterns and pushback:**

- **Fluff / values** ("we're developer-obsessed and move fast") -> "Those are
  values, not an approach. What are you doing *differently* from the other
  ways this problem could be solved? If the answer applies to anything, it's
  not your approach."
- **Feature list** ("we're building X, Y, and Z") -> "That's a feature list.
  What's the underlying bet that makes you pick those over others? What
  principle is guiding what you ship?"
- **Description as approach** ("we generate a plan from a prompt") -> "That's
  what it does, but what's the *choice* inside it? Every alternative could
  say the same thing. What are you betting on that they're not?"
- **Goal restated** ("our approach is to become the default choice") -> "That's
  still the goal. How does it win? What choice are you making that the
  obvious alternative isn't?"
- **Multiple approaches at once** ("we're going deep on speed, breadth, and
  polish") -> "Pick one as the guiding approach. The others may still get
  work, but one of them organizes the rest. Which is it?"
- **Doesn't connect to the problem** (problem: "users can't trust generated
  output"; approach: "build a fast, beautiful UI") -> "How does that approach
  solve the problem you named? If there's no line between them, one of the
  two is wrong."

**Capture:** One or two sentences. Ideally ends with or implies "...so that
[outcome tied to the problem]".

______________________________________________________________________

## 3. Who It's For

**Opening question:** "Who does this serve, and what job are they hiring it
to do — as a set of capabilities, and/or as something a downstream user or
repo consumes?"

Two things can both be true and both belong here: the thing's own functional
capabilities (what it actually does, for the person using it directly) and
its downstream consumers (other repos, teams, or personas that install or
depend on it). A plugin or library read by other repos has no
end-customer in the traditional sense — its "who it's for" is the
capabilities it offers plus the kinds of consumers that adopt it, not a
market segment.

Jobs-to-be-done framing — the user isn't a demographic, they're someone (or
some repo) in a situation trying to make progress.

Strong answers name one primary persona or consumer type (additional ones
allowed but secondary), identify them by role, situation, or integration
point rather than demographic, and state a concrete job as a verb phrase.

**Anti-patterns and pushback:**

- **Too many primary personas** ("it's for founders, PMs, engineers, and
  designers") -> "If it's for everyone, it's for no one. Who matters most?
  The others can still benefit, but one of them drives the decisions."
- **Demographic framing** ("25-45 year old professionals") -> "That's a
  demographic, not a user. What are they trying to do that makes them reach
  for this?"
- **Role without situation** ("engineers") -> "Engineers doing what? Debugging
  a live incident? Reviewing an unfamiliar codebase? Planning a large
  change? The situation is where it matters."
- **Generic job** ("they want to be more productive") -> "Productive at what
  specifically? They're hiring this to do *what*? The more specific, the
  better the decisions downstream."
- **Capabilities described with no consumer** ("it does X, Y, and Z") -> "Who
  reaches for X, Y, and Z, and in what situation? Capabilities without a
  consumer in mind drift."

**Capture:** Persona/consumer name plus JTBD sentence, and — if this is a
plugin, library, or internal tool rather than an end-user product — a short
line naming the functional capability it provides to whoever installs or
depends on it. Example: "Repos that install this plugin. They're hiring it
to turn a vague idea into a grounded, orchestrator-ready plan without
hand-writing a task map."

______________________________________________________________________

## 4. Key Metrics

**Opening question:** "What 3-5 metrics will tell you whether the approach is
working?"

Metrics are the feedback loop. Bad metrics create the illusion of progress
while the thing gets worse.

Strong answers stay at 3-5 (not 10), mix leading and lagging (something that
moves weekly and something that moves quarterly), and could plausibly
regress if things got worse.

**Anti-patterns and pushback:**

- **Vanity metrics** ("total installs, total pageviews, cumulative runs") ->
  "Those can all go up while things get worse. What moves when the thing
  actually delivers value?"
- **Too many** ("here are 12 metrics we watch") -> "A dashboard isn't a
  strategy. Pick the 3-5 you'd stake the quarter on. What are the others
  telling you that those don't?"
- **Outputs, not outcomes** ("commits merged, PRs opened") -> "Those measure
  activity, not value. If activity doubled but nothing got better, would you
  call it a win?"
- **Can only go up** ("cumulative time saved") -> "A metric that can only go
  up doesn't tell you much. What's the rate, the ratio, or the thing that can
  regress?"
- **Unmeasurable** ("developer delight") -> "How specifically? If you can't
  define how you'd check it on a Tuesday, it's aspirational, not a metric."

**Capture:** A list of 3-5. Each with a one-line definition. Note where each
is measured (analytics, logs, DB, qualitative, etc.) if known. If measurement
is undefined, ask: "Where does this metric live today? If nowhere, is this
something you can start measuring?"

______________________________________________________________________

## 5. Tracks

**Opening question:** "What are the 2-4 tracks of work you're investing in to
execute the approach?"

Tracks are the coherent-actions half of the strategy kernel — concrete areas
of investment that flow from the approach. They are not feature lists and
not personal todo items. Each track is a named *domain of work*.

Strong answers stay at 2-4 (not 8, not 1), connect clearly back to the
approach, and are broad enough that multiple features live inside each one.

**Anti-patterns and pushback:**

- **Feature list in disguise** ("track 1: new CLI flag; track 2: a skill;
  track 3: a bugfix") -> "Those are features. What's the *investment area*
  each one lives inside? 'Planning workflow' might be one track, with the
  skill and the flag as candidates inside it."
- **Too many tracks** ("we have 7 tracks this quarter") -> "With 7 tracks,
  every track is starved for attention. Which 3 are load-bearing? The others
  either fold in or drop."
- **Doesn't connect to approach** (approach: "win by being deterministic";
  track: "add a chat UI") -> "How does that track serve the approach? If
  it's a separate bet, name it as one. If it's load-bearing, explain the
  link."
- **Too vague** ("improve the plugin") -> "Every track is 'improve the
  plugin.' What's the specific investment area that's different from the
  others?"
- **One track only** -> "With one track, there's no real choice being made.
  What are the 2-3 things this needs to be good at, and how are they
  different?"

**Capture:** 2-4 tracks. For each: a name, a one-line purpose, and a short
note on why this serves the approach.

______________________________________________________________________

## 6. Milestones (optional)

**Opening question:** "Are there any dated milestones worth anchoring — a
release, a version bump, a deprecation deadline, a conference? Skip if none
apply."

Only capture externally visible, real milestones. Avoid turning this into an
internal schedule.

Default is to skip. Do not push the user to invent milestones. If they name
some, capture them verbatim with dates.

______________________________________________________________________

## 7. Not Working On (optional)

**Opening question:** "Is there anything you've explicitly decided *not* to
do right now that's worth naming? This is for things you keep being tempted
by."

Clarity tool, not a blocker list. Skip by default. If the user names items,
one sentence each. Do not encourage a long list.

______________________________________________________________________

## 8. Marketing (optional)

**Opening question:** "How would you describe what this does, in one line,
to someone deciding whether to install or adopt it? Skip if not yet."

For a plugin, library, or internal tool, this is a clear functionality
pitch, not a customer-acquisition message — the line a README or plugin
listing would use, not ad copy. Skip by default. Keep to 2-3 lines if
present.

______________________________________________________________________

## After the Interview

Once sections 1-5 are captured (and any optional sections the user engaged
with), read `strategy-template.md` and fill it in. Present the full draft in
chat before writing. Offer one edit round. Then write to `STRATEGY.md`.
