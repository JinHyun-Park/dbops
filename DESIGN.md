# DBOps Design System

A console for DBAs operating Aurora at scale, with an AI agent
on call. The design language has to read like infrastructure software
— precise, dense, legible at a glance — without the generic
"AI-generated dark dashboard" cliché (rounded blue buttons, gradient
purple accents, evenly-distributed neon palettes).

## Voice

- **Industrial editorial.** Engineering tool with editorial discipline:
  monospace section labels, all-caps tracked eyebrows, generous figure
  spacing for numbers, and exactly one accent color for emphasis.
- **Information-first.** Every page exposes structured data — tables,
  stats, time series. Chrome (cards, borders) recedes; data is loud.
- **Authoritative, not friendly.** No mascots, no playful copy, no
  rounded corners trying to feel approachable. Sharp edges, terse copy.

## Typography

```
--font-display:  "IBM Plex Sans", system-ui, sans-serif
--font-body:     "IBM Plex Sans", system-ui, sans-serif
--font-mono:     "IBM Plex Mono", "JetBrains Mono", ui-monospace
```

IBM Plex is intentional — it is the typeface IBM commissioned for its
own engineering products. It is open-source, distinctive (not Geist
/Inter/Roboto), and reads as infrastructure software rather than
consumer SaaS.

**Hierarchy**

| Role        | Class                                                | Note                       |
|-------------|------------------------------------------------------|----------------------------|
| Page title  | `text-3xl font-semibold tracking-tight`              | Plex Sans                  |
| Section H   | `text-base font-medium`                              | Plex Sans                  |
| Eyebrow     | `font-mono text-[10px] tracking-[0.22em] uppercase`  | Plex Mono                  |
| Body        | `text-sm text-zinc-300`                              | Plex Sans                  |
| Hint        | `text-[11px] text-zinc-500`                          | Plex Sans                  |
| Data/ID     | `font-mono text-xs tabular-nums`                     | Plex Mono                  |
| Big number  | `text-3xl font-semibold tracking-tight tabular-nums` | Plex Mono looks brittle    |
| `<kbd>`     | `font-mono text-[10px]` in bordered chip             |                            |

## Color

Single accent — **amber** (`#fbbf24`). It signals "DBOps brand" and
nothing else; not used for warnings (which use amber-300, distinct
hue/value).

```
/* Surfaces */
--surface-base:    rgb(9  9  11)    /* zinc-950, the canvas      */
--surface-raised:  rgb(24 24 27)    /* zinc-900, cards/tables    */
--surface-inset:   rgb(39 39 42)    /* zinc-800, inputs/hover    */

/* Borders */
--border-subtle:   rgb(39 39 42)    /* zinc-800 */
--border-strong:   rgb(63 63 70)    /* zinc-700 */

/* Text */
--text-primary:    rgb(244 244 245) /* zinc-100 */
--text-secondary:  rgb(212 212 216) /* zinc-300 */
--text-muted:      rgb(161 161 170) /* zinc-400 */
--text-faint:      rgb(113 113 122) /* zinc-500 */
--text-ghost:      rgb(82  82  91)  /* zinc-600 */

/* Brand */
--accent:          rgb(251 191 36)  /* amber-400 */
--accent-strong:   rgb(245 158 11)  /* amber-500 */

/* Signal */
--signal-critical: rgb(251 113 133) /* rose-400 — blocking locks, errors */
--signal-warn:     rgb(252 211 77)  /* amber-300 — degraded, high CPU    */
--signal-ok:       rgb(52  211 153) /* emerald-400 — healthy             */
--signal-info:     rgb(125 211 252) /* sky-300 — informational           */
```

**Rules**
- One accent color. Don't use amber and emerald-400 next to each other
  for "primary action vs success" — pick one role.
- Signal colors are reserved for data state, not decoration.
- No gradients. No glow/blur effects. Single 1px borders.

## Layout

```
--page-max-width: 80rem          /* max-w-7xl */
--page-padding:   2rem (sm) / 2.5rem (lg)
--card-padding:   1.25rem
--section-gap:    2.5rem
```

- Pages always wrap content in `<PageBody>` for consistent gutter.
- Every page starts with `<PageHeader>` (eyebrow + title + description
  + actions).
- Sections use `<Section>` with eyebrow.
- Empty states use `<EmptyState>` with primary/secondary CTA — never
  inline "no data" text.

## Components

- `<PageHeader>` — page-scoped chrome. eyebrow (monospace), title
  (3xl), description (max-w-2xl), actions (right-aligned).
- `<PageBody>` — max-w-7xl + padding.
- `<Section>` — content group with eyebrow + title + optional actions.
- `<EmptyState>` — onboarding nudge with primary/secondary CTA.
- `<Stat>` — keyed metric card with eyebrow + tabular value + hint.

## Borders & corners

- **Radius**: 0 by default. Inputs/selects: `rounded` (≈4px) only.
  Buttons: 0. Cards: 0. Avoid `rounded-lg` everywhere.
- **Borders**: 1px solid `--border-subtle`. Hover: `--border-strong`
  for ghost buttons, `--accent` for primary inputs.

## Motion

- Microcorrections only. No entry animations on data tables.
- `transition-colors` on hover (150ms). No `scale`, `translate`, or
  bounce easings.
- Loading: dim text to `--text-ghost` with `···` glyph, no spinners
  unless the wait is intentional (chat streaming, deploy progress).

## What we do not do

- 🚫 No gradient backgrounds (purple→pink, dark→darker meshes).
- 🚫 No glassmorphism / backdrop-blur on cards.
- 🚫 No rounded-2xl shadow-2xl cards.
- 🚫 No emoji as primary iconography (acceptable inline in chat).
- 🚫 No "AI" colors (electric purple, gradient cyan).
- 🚫 No multi-accent palettes ("each section gets its own color").

## What we do

- ✅ Monospace eyebrows for section structure.
- ✅ Tabular-nums on every number that compares vertically.
- ✅ One signature accent (amber), used sparingly.
- ✅ 1px borders, sharp corners.
- ✅ Empty states with explicit CTA — never silent.
- ✅ Dense tables with `<thead>` in monospace tracked uppercase.
