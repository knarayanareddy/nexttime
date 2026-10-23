# UI Tooling, Provenance, and Reproducible Validation

## 1. Scope

This document separates exploratory design input from deterministic validation. Neither tool is a source of product truth, and neither can prove that an application works on Fire TV, iOS, Android, Alexa, or with intended users.

The browser artifacts have **no external runtime dependencies**. They use local HTML/CSS/JavaScript and system font fallbacks. The npm dependencies below are development-only validation tools.

## 2. Exploratory input: UI UX Pro Max

- Repository: `nextlevelbuilder/ui-ux-pro-max-skill`
- Inspected commit: `d14efff8ed1faa4187f004f79926f428c8074ec7`
- Inspected package version: `2.5.0`
- Use: five local `search.py … --design-system … -f markdown` explorations, one per product

The generated outputs were temporary working notes, not committed specifications. Recommendations were filtered through each `PRODUCT.md`, the implementation specification, platform constraints, and accessibility requirements.

| Project | Generic output rejected | Approved direction |
| --- | --- | --- |
| AccessHold | Claymorphism, purple palette, travel-marketplace/assistant patterns | Calm evidence desk with explicit provenance and uncertainty |
| CurioLab | Avoid-dark-mode advice and childlike typography everywhere | Dark cinematic TV scene; Fredoka only for short display text, Atkinson Hyperlegible for reading |
| NextTime | Comic Neue/Baloo, cartoon coaching, testimonial/marketing patterns | Adult native-iOS pocket coach with private evidence and no scores/streaks |
| HarvestGrid | Mint/teal gradients, violet shadows, pill-heavy landing-page patterns | Solid-color field ledger with dense operational evidence and privacy partitioning |
| RoomSmith | Violet glow and generic dark-game dashboard styling | Midnight-lighthouse nautical scene with deterministic verification states |

Useful prompts about TV focus, companion density, state coverage, and semantic tokens were retained only after they agreed with the domain contracts. No generated palette, font, layout, or claim was adopted automatically.

## 3. Deterministic tooling

| Tool | Purpose | Report |
| --- | --- | --- |
| Python 3 standard library (`validate.py`) | Five workspace layouts, canonical JSON syntax/duplicate keys, 284 traceability IDs, token structure, theme/token agreement, 50 contrast pairs, HTML structure, local links/fragments, static controls, repository Markdown links | [`audit/static.json`](./audit/static.json) |
| Playwright + Chromium (`validate-browser.cjs`) | Real rendering at 1440×1000 and 390×844, reduced motion on mobile-width runs, overflow, 44 px controls, runtime errors, external requests | [`audit/browser.json`](./audit/browser.json) |
| axe-core 4.14.0 | Automated WCAG-tagged accessibility rules on all 12 page/viewport combinations | Included in `audit/browser.json` |
| Impeccable 4.7.0 | Deterministic source/HTML/CSS anti-pattern and quality detector | [`audit/impeccable.json`](./audit/impeccable.json) and [`audit/README.md`](./audit/README.md) |

A clean automated result is not a WCAG conformance certification, screen-reader evaluation, device test, or usability result. See the audit for manual review and remaining limits.

## 4. Reproduce from a clean checkout

Tested with Node.js 22.22.3, npm 10.9.8, and Python 3. Use Node 22 for the pinned browser tooling.

```bash
cd specs/ui
npm ci
npm run validate
```

`npm run validate` runs, in order:

1. `python3 validate.py` — dependency-free workspace, contract, traceability, and static UI checks;
2. `node validate-browser.cjs` — 12 real Chromium renderings plus axe;
3. `impeccable detect --json …` — the actual HTML/CSS/source set.

On Linux, the pinned `@sparticuz/chromium` package supplies a headless binary and extracts its runtime libraries to the system temporary directory. On another platform, set `CHROME_PATH` to an installed Chrome/Chromium executable:

```bash
CHROME_PATH=/path/to/chrome npm run validate:browser
```

No credentials, account, network service, or application server is required.

## 5. Regenerate the committed reports

```bash
cd specs/ui
python3 validate.py --report audit/static.json
node validate-browser.cjs --report audit/browser.json
npm run --silent audit:ui > audit/impeccable.json
npm run validate
```

The reports intentionally omit timestamps and workstation paths so repeated unchanged runs produce stable content. Browser screenshots are not committed. To inspect them locally:

```bash
UI_SCREENSHOT_DIR=/tmp/ringer-ui-screenshots npm run validate:browser
```

## 6. Impeccable-only scan

```bash
cd specs/ui
npx --yes impeccable@4.7.0 detect --json \
  index.html index.css wireframes.css wireframes.js \
  ../../projects/accesshold/ui ../../projects/curiolab/ui \
  ../../projects/nexttime/ui ../../projects/harvestgrid/ui \
  ../../projects/roomsmith/ui
```

The detector exits nonzero when findings exist. Its result supplements rather than replaces token contrast checks, axe, browser layout checks, and human design review.

## 7. Change workflow

1. Update `PRODUCT.md` and the normative implementation specification first if product behavior changes.
2. Update `DESIGN.md`/`WIREFRAMES.md` and the semantic tokens together.
3. Keep each `theme.css` synchronized with `tokens.json`; static validation fails on drift.
4. Keep controls honestly static (`data-static`) or implement a real local navigation anchor. Do not make a simulated integration look operational.
5. Regenerate all three reports, resolve or document every finding, and review desktop/mobile renderings.
6. Do not describe the phase as high fidelity, device validated, or user validated without separate evidence.
