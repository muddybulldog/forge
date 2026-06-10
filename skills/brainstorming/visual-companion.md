# Visual Companion Guide

Browser-based companion for showing mockups, diagrams, and visual options during
brainstorming. **Display-only:** the browser shows; all conversation — reactions,
choices, direction — happens in the terminal.

## When to use

Decide per-question, not per-session. The test: **would the user understand this
better by seeing it than reading it?**

**Use the browser** when the content itself is visual: UI mockups and wireframes,
layout or design-direction comparisons, architecture diagrams, state machines and
flowcharts, look-and-feel questions.

**Use the terminal** when the content is text: requirements and scope, conceptual
A/B/C choices, trade-off lists, API and data-model decisions. A question *about*
a UI topic is not automatically visual — "what should the wizard do?" is
conceptual (terminal); "which of these wizard layouts feels right?" is visual
(browser).

## The iteration loop

Visuals are part of the design, so they go through the same approval discipline
as everything else in brainstorming:

1. **Present** — write a mockup (or several variants side by side) to `screen_dir`;
   the browser refreshes automatically.
2. **Discuss in the terminal** — the user reacts in the CLI, never by clicking.
3. **Checkpoint on selection** — when the user picks a direction, do not start
   building. Ask: *"Option A it is — want to refine it further or go deeper on
   that direction, or is this good to fold into the design?"*
4. **Refine** — apply the feedback, present the updated mockup, return to step 2.
5. **Exit only on explicit approval** — keep iterating until the user says it's
   good. Visual approval is part of the design-approval gate: a design with
   unapproved mockups is not an approved design.

The default assumption is *more refinement*, not building. Going from "user
picked an option" straight to implementation is the failure mode this loop exists
to prevent.

## How it works

The server watches a directory and serves the newest `.html` file to the browser,
auto-reloading on changes. You write HTML to `screen_dir`; the user watches it
evolve.

**Fragments vs. full documents:** if your file starts with `<!DOCTYPE` or
`<html`, it's served as-is. Otherwise it's wrapped in the frame template (header,
OS-aware light/dark theme, layout CSS). **Write fragments by default** — the
frame provides `.options`/`.option` (lettered choice rows), `.cards`/`.card`,
`.mockup`, `.split` (side-by-side), `.pros-cons`, `.placeholder`, and `.mock-*`
elements for quick wireframes.

## Starting a session

```bash
# From this skill's scripts/ directory — always pass the project root
scripts/start-server.sh --project-dir /path/to/project

# Returns: {"type":"server-started","port":52341,"url":"http://localhost:52341",
#           "screen_dir":".../.theforge/brainstorm/<session>/content",
#           "state_dir":".../.theforge/brainstorm/<session>/state"}
```

Save `screen_dir` from the response and tell the user to open the URL. With
`--project-dir`, mockups persist in `.theforge/brainstorm/` (gitignored; the
`.theforge/` directory also serves as the theforge session-hook opt-in signal).
Without it, files go to `/tmp` and are cleaned up. If you launched in the
background and lost the startup JSON, read `state_dir/server-info`.

Remote/container environments: add `--host 0.0.0.0` (and `--url-host <name>` to
control the displayed hostname). If background processes get reaped, rerun with
`--foreground` in a persistent terminal.

## Stopping

```bash
scripts/stop-server.sh <session_dir>   # or let it die: 30-min idle timeout, or owner-exit detection
```

Project-dir sessions keep their mockup files after the server stops — useful as
input to the spec.
