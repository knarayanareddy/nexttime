# NextTime — Visual and Component Design

**Direction:** quiet pocket coach
**Tokens:** [tokens.json](./tokens.json)
**Screens:** [WIREFRAMES.md](./WIREFRAMES.md) and [browser wireframes](./wireframe.html)

## 1. Design thesis

NextTime should feel like a private, well-made notebook opened just before a real conversation. Warm paper, deep moss, and restrained terracotta create an adult, low-pressure character. There are no cartoon tutors, score rings, flames, or grammar-error walls.

The generated Comic Neue/Baloo direction and testimonial carousel are rejected. They conflict with an adult privacy-sensitive product and do not support the core next-moment workflow.

## 2. iOS layout and navigation

- Single-column SwiftUI stacks with 20 pt page inset and 28 pt section spacing.
- Three bottom tabs: `Today`, `Progress`, `Privacy`. Bee connection lives under Privacy/Settings, not as a fourth tab.
- Today order: next real moment → rehearsal readiness → one primary action → recent authentic outcome.
- Attempt review order: context/consent → outcome rubric → proposed breakdown → self evidence → correction actions.
- No fixed-height content cards; Dynamic Type reflows controls and turns side-by-side actions into vertical stacks at accessibility sizes.

## 3. Typography and language presentation

- SF Pro Display/Text with system fallbacks; no custom font requirement.
- 34 pt large title, 22 pt section title, 17 pt body, 15 pt minimum supporting text at default size.
- Dutch phrases are primary; English support is visually subordinate but remains at readable size and can be hidden/shown.
- Self excerpts use quotation styling, not code blocks or correction markup. Redaction appears as `[redacted]` with an accessible explanation.
- Outcome numbers use tabular figures but are never shown as a fluency percentage.

## 4. Color and state grammar

- Moss primary for rehearsal/resume actions.
- Terracotta accent for privacy-sensitive/destructive actions, not general decoration.
- Success is a check + `Task confirmed`; partial is a half-circle + `Partly completed`; unknown is a question + `Needs review`.
- Rehearsal completion uses a neutral `Practice completed` treatment and cannot borrow the authentic-success icon.
- Recording uses a filled record icon + `Recording` + elapsed time + persistent stop control. It is not encoded only by red.

## 5. Component contracts

| Component | Anatomy and behavior | Accessibility/content requirement |
| --- | --- | --- |
| `NT-NextMoment` | Situation, real event time, rehearsal time/duration, readiness, primary action | Event source is visible; no inferred sensitive event title |
| `NT-RehearsalReady` | Target function, 90-second estimate, audio/text options, offline state | Does not claim real-world improvement before it happens |
| `NT-AttemptReview` | Situation, consent/source, deterministic rubric, confidence, self evidence | Unknown and low-confidence states are prominent and correctable |
| `NT-BreakdownProposal` | One functional target, self excerpt, confirm/edit/reject | Rejection is equal in size to confirmation; no shame-oriented copy |
| `NT-RehearsalPlayer` | Counterpart caption, optional English, turn progress, large hold-to-talk, phrase support | VoiceOver/Switch Control alternative and non-speaking path included |
| `NT-RecordingControl` | Record icon, recording state, elapsed time, release/stop behavior | Haptic + visual announcement; no hidden background capture |
| `NT-OutcomeComparison` | Prior/next authentic attempts, criterion deltas, evidence links | Labeled `Authentic attempts`, not app score or CEFR level |
| `NT-SkillSummary` | Functional skill, attempt count, evidence strength, last attempt | No percentage without uncertainty and sample-size context |
| `NT-ConsentRow` | Capture mode, participants, policy version, retention, revoke action | Plain-language scope and deletion consequence |
| `NT-EmptyProgress` | Explanation that authentic attempts appear after review, link to Today | No fake zero-score chart or motivational pressure |

## 6. Rehearsal interaction

The hold-to-talk control is 88 pt at default size but expands/reflows under accessibility text. Press-and-hold has a tap-to-start/stop alternative. While recording, counterpart text remains visible and the app does not trigger haptic patterns repeatedly. After release, status becomes `Processing speech` with cancel/retry; it never immediately shows a negative correction.

Phrase support opens inline and preserves the current turn. Completing with tap/text is labeled `Non-speaking practice`, without treating it as lesser.

## 7. Motion, audio, and haptics

- 120–200 ms fades and small state transitions only; no confetti, waveforms as decoration, streak animations, or pulsing record controls.
- A static recording indicator replaces pulsing under reduced motion.
- Audio never autoplays on app open. Captions are always available.
- Haptics are supplementary and respect system settings.

## 8. Privacy presentation

Privacy is a first-class tab with capture modes, Bee bridge status, source/derived retention, export, revoke, and delete. It uses plain-language summaries followed by expandable technical detail. Deletion confirmation lists categories rather than using a vague `Delete everything` button.

No full-screen waveform, transcript wall, or “living memory” visualization is used.

## 9. Required visual states

Bee: not configured, bridge offline, syncing, review required, revoked. Rehearsal: not ready, generating, ready, downloading audio, offline/template, in progress, completed, expired. Outcome: processing, uncertain, confirmed, corrected, excluded. Each state preserves the next real event and a useful action.

## 10. Implementation handoff

SwiftUI views consume semantic color/font tokens through an asset catalog and native text styles. Authentic outcome and rehearsal outcome are separate view models and icon sets to prevent accidental visual equivalence. VoiceOver labels are defined alongside component contracts, not added after layout completion.
