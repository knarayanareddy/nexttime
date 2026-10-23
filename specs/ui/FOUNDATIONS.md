# Shared UI/UX Foundations

## 1. Design principles

1. **Domain before decoration.** Evidence status, measurements, readiness, consent, and puzzle state are more important than visual novelty.
2. **One visual hierarchy per surface.** Every screen has one primary task. Secondary metadata and advanced actions recede.
3. **Calm by default.** Disruption, privacy, learning, and physical setup do not benefit from alarmist color, confetti, urgency timers, or manipulative motion.
4. **Explicit provenance.** Fixture, live, stale, inferred, provider-confirmed, and human-confirmed states use words and timestamps, not color alone.
5. **Accessible equivalent, not accessible afterthought.** Remote, keyboard, screen-reader, reduced-motion, non-camera, and non-speech paths share the same content hierarchy.
6. **Platform-native before brand consistency.** A Fire TV scene, Alexa rich card, iOS rehearsal, and operations table should not share one generic SaaS layout.
7. **No fabricated evidence in design.** Wireframes use labeled fixtures and never imply live provider data, actual learners, real trees, or detected players.

## 2. Shared content rules

- Use sentence case for headings and controls.
- Buttons describe outcomes: `Review recovery`, `Confirm 4 coins`, `Approve glean`, not `Submit` or `Continue` when the consequence is specific.
- Status labels pair noun + state + time where useful: `Provider confirmed · 20 Oct, 09:30 UTC`.
- Unknown is written as `Unknown` or `Not yet verified`, never blank or inferred negative.
- Errors contain what happened, preserved work, and the next action.
- Avoid AI anthropomorphism in operational UI: `Evidence extracted` rather than `I understood your trip`.
- Use icons with visible text for consequential status and actions. Decorative icons are hidden from assistive technology.
- Numbers use tabular figures in comparisons, measurements, dates, and operational tables.

## 3. Accessibility floors

| Concern | Baseline |
| --- | --- |
| Normal text contrast | At least 4.5:1 |
| Large text and essential component boundaries | At least 3:1; normal text target remains 4.5:1 |
| Web/mobile pointer target | At least 44×44 CSS px / 44 pt iOS / 48 dp Android |
| Fire TV remote target | At least 64×64 px at 1080p with visible focused/unfocused states |
| Focus indicator | At least 2 CSS px, high contrast, offset from component edge; never remove without replacement |
| Text resize | Web 200%; iOS Dynamic Type; Android font scale; no clipped critical content |
| Motion | No required animation, flashing, parallax, or bounce; honor reduced-motion settings |
| Color | Never the sole carrier of status, correctness, readiness, or selection |
| Charts | Text summary, labeled series/patterns, and tabular alternative |
| Audio/video | Captions/transcript and non-audio equivalent; no autoplay with sound |

## 4. Platform layout rules

### Responsive web — AccessHold and HarvestGrid

- Content max width 1280 px; readable narrative max 68 characters.
- Twelve-column desktop grid, eight-column tablet, four-column mobile.
- Single-column primary flow below 720 px; comparison tables become labeled rows or horizontally scroll with an accessible caption.
- Sticky elements never cover focused content or validation summaries.

### Fire TV — CurioLab and RoomSmith

- Design at 1920×1080 and preserve a 5% title-safe/action-safe inset.
- Body text starts at 28 px, key instructions 32–40 px, and scene dialogue remains within two to four short lines.
- Focus is more than a subtle glow: use scale no greater than 1.03 plus outline, label, or shape change.
- Remote focus order follows visual geography and cannot enter hidden scene layers.
- Captions avoid the bottom-right remote/action region and remain within title-safe bounds.

### iOS — NextTime

- SwiftUI-native stacks, Dynamic Type, VoiceOver ordering, and minimum 44 pt targets.
- Primary action stays reachable without blocking content at large text sizes; no fixed-height text cards.
- Bottom navigation has at most three destinations: Today, Progress, Privacy.
- Recording uses persistent visual and haptic state, not microphone color alone.

### Android companion — CurioLab and RoomSmith

- Setup/calibration is utility UI, visually subordinate to the TV story.
- Minimum 48 dp targets, camera status in text, and a large fallback action.
- Debug contours are limited to adult setup and never presented as a game/science result by themselves.

### Alexa rich card — AccessHold

- One decision per card: status, evidence, recovery comparison, or approval.
- Maximum three recovery options plus human handoff; exact cost/terms are visible before approval.
- Voice summary and card contain equivalent consequential information.

## 5. Tokens and asset policy

- Components consume semantic tokens, not raw hex values. Primitive palette changes must not bypass contrast validation.
- Typography uses platform-native fonts where that improves performance and accessibility. Named production fonts require license review and local bundling; wireframes use system fallbacks and make no external font requests.
- Icons use a consistent outlined SVG set in production. Emoji are not interface icons.
- No external scripts, trackers, stock-photo calls, font CDNs, or third-party iframes in the wireframes.
- Room, child, Bee, Ring, address, and provider imagery is replaced by schematic CSS/SVG representations in design artifacts.

## 6. Review layers

Design quality requires all of these; one does not substitute for another:

1. **Machine checks:** token parsing, contrast ratios, local links, duplicate IDs, HTML structure, and deterministic anti-pattern detection.
2. **Design review:** hierarchy, content clarity, state coverage, platform fit, and anti-generic review against `PRODUCT.md` and `DESIGN.md`.
3. **Rendered inspection:** relevant viewport sizes, zoom/font scaling, keyboard/remote focus, and screen-reader order.
4. **User evaluation:** task completion and comprehension with the intended audience after a project is selected.

Only the first two layers and static browser inspection are claimed by this pack.
