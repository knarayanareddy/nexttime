# NextTime — Low-Fidelity Wireframes

Open [wireframe.html](./wireframe.html). The five phone artboards are responsive static compositions, not an iOS build or clickable production prototype. Bee items, Dutch attempts, and outcomes are fictional and labeled as such.

## Screen inventory

| Anchor | Screen | Purpose | Primary path |
| --- | --- | --- | --- |
| `#today` | Today | Rehearse just before the next relevant real moment | Start a 90-second rehearsal |
| `#review` | Authentic attempt review | Validate deterministic interpretation and evidence | Confirm, edit, or reject proposal |
| `#rehearsal` | Rehearsal turn | Practice one functional move with consented input | Hold/tap to speak or use text |
| `#progress` | Authentic progress | Compare evidence from similar real attempts | Inspect a criterion or attempt |
| `#privacy` | Privacy and sources | Control capture, retention, Bee access, export, and deletion | Change a scope or revoke |

## 1. Today

**Composition:** next real moment, rehearsal readiness, one primary action, last authentic outcome, three-tab navigation.

**Fictional content:** return a jacket at a shop tomorrow at 10:30; a 90-second Dutch rehearsal is ready offline.

**Annotations**

- Calendar/Bee-derived context is labeled with its source; no hidden inference.
- `Practice completed` and `Task confirmed` remain visually distinct.
- Primary action stays one functional rehearsal, not a generic chat.

**State variants:** no upcoming moment; rehearsal generating; offline template ready; audio still downloading; rehearsal expired; recently practiced.

## 2. Authentic attempt review

**Composition:** source/consent header, deterministic outcome, proposed functional breakdown, self excerpt, three correction actions.

**Fictional content:** returning an item, partly completed because the refund question was not understood; low confidence on one criterion.

**Annotations**

- Unknown is not treated as failure.
- Rejecting a proposal is a first-class action and preserves the original attempt.
- Self evidence is shown before any skill summary changes.

**State variants:** processing; uncertain; ready for review; confirmed; edited; rejected; excluded by consent.

## 3. Rehearsal turn

**Composition:** counterpart turn and optional English, turn progress, phrase support, large record control with explicit state, non-speaking alternative.

**Annotations**

- Press-and-hold and tap-to-start/stop have the same accessible name and outcome.
- Recording state persists without animation and always has a stop route.
- Corrections are limited to the current functional target rather than a red-ink transcript.

**State variants:** ready; recording; processing; playback unavailable; template fallback; turn complete; interrupted; microphone denied.

## 4. Authentic progress

**Composition:** functional-skill summary, sample-size/uncertainty note, two comparable authentic attempts, criterion changes with evidence links.

**Fictional content:** two service-counter return attempts; improved `state the request`, still uncertain `ask for clarification`.

**Annotations**

- No CEFR grade, fluency percentage, streak, or leaderboard.
- Rehearsals are excluded from the chart and labeled separately if listed elsewhere.
- Small samples are explained next to the comparison, not buried in help.

**State variants:** no attempts; one baseline only; two comparable attempts; conflicting review; excluded source; all data local.

## 5. Privacy and sources

**Composition:** capture mode, participant consent, Bee bridge state, retention, export/revoke/delete controls.

**Annotations**

- Raw source and derived-item retention are separately explained.
- Revoking Bee does not silently delete unrelated local attempts; deletion scope is explicit.
- Destructive actions use terracotta and require a scoped confirmation.

**State variants:** not configured; bridge offline; syncing; review required; revoked; export ready; deletion pending/completed.

## Flow contract

`Consented real moment → Rehearsal ready → Practice → Authentic attempt → Deterministic review → Confirmed breakdown → Next rehearsal`

A rejected breakdown never advances to a new teaching target as fact. Browser artboards do not record audio, access Bee/calendar data, or save changes.
