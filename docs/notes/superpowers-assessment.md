# Superpowers plugin — assessment & rewrite notes

*Basis for a personal fork/rewrite. Assessment done 2026-06-09 against superpowers v5.1.0
(`~/.claude/plugins/cache/claude-plugins-official/superpowers/5.1.0/`) and the current
Claude Code harness.*

## TL;DR

Fork and slim it. About two-thirds of superpowers is now redundant with the harness.
The two biggest token sinks are structural choices that can be changed without losing
the parts that work (brainstorming, forced TDD).

## What's actually in there

- 14 skills, ~16,600 words of markdown, plus a session-start hook.
- It is **not** actually a black box — every behavior is a readable `SKILL.md`.
- The black-box *feeling* comes from skills chaining into each other with mandatory
  language: brainstorming → writing-plans → subagent-driven-development →
  finishing-a-development-branch. One "build X" request triggers a four-skill
  pipeline you never explicitly opted into.

## Where the tokens and time actually go

1. **Plans contain the full implementation.** `writing-plans` requires every step to
   include complete code — actual test bodies, actual implementations, exact commands
   with expected output, no placeholders; "Similar to Task N" is forbidden so code gets
   repeated. The implementation is written twice: once in the plan (without compiler or
   test feedback, so it's often wrong and gets rewritten anyway) and once for real.
   Made sense for weaker models executing blind; current models don't need it.
   **Single biggest cost.**

2. **Three-plus subagents per task.** `subagent-driven-development` dispatches an
   implementer, a spec-compliance reviewer, and a code-quality reviewer per task, with
   mandatory re-review loops, plus a final whole-implementation reviewer. Each subagent
   receives full task text + context. A 6-task plan ≈ 20 agent invocations minimum.
   No proportionality — a one-line config task gets the same two-stage review as a
   tricky auth change.

3. **Constant per-session overhead.** The session-start hook injects the full
   `using-superpowers` skill (~800 words wrapped in `EXTREMELY_IMPORTANT`) on every
   startup, clear, and compact. Its "even a 1% chance a skill applies → you MUST
   invoke it" rule forces skill invocations before *every* response, including simple
   questions.

## Skill-by-skill verdict

| Skill | Verdict | Notes |
|---|---|---|
| brainstorming | **Keep** | Liked, and good. Overlaps with native plan mode but produces durable specs the project tracking depends on. |
| test-driven-development | **Keep** | Models still rationalize skipping TDD; this discipline is real value. Best-written skill in the plugin. |
| writing-plans | **Keep but rewrite** | See "planning" below — drop the embedded implementation code. |
| subagent-driven-development | **Replace** | Native Workflow tool does deterministic orchestration (pipelines, structured outputs, resume, progress display) better than prompt-driven dispatch loops. |
| executing-plans | **Replace** | Thin; same replacement as SDD. |
| dispatching-parallel-agents | **Drop** | Parallel tool calls + Workflow tool are native. |
| using-git-worktrees | **Drop** | Native now (`EnterWorktree`, Agent `isolation: "worktree"`). Generic skill also doesn't know the env-symlink gotcha; memory file does. |
| requesting-code-review | **Drop** | Built-in `/code-review` (incl. ultra) is more capable. |
| receiving-code-review | **Drop** | Same. |
| verification-before-completion | **Drop** | "Evidence before claims" is baked into current system prompts; `/verify` exists. |
| systematic-debugging | **Marginal** | Current models do hypothesis-driven debugging natively; keep only if it has demonstrably changed behavior. |
| using-superpowers | **Drop** | Pure overhead now — native skill discovery covers it. |
| writing-skills | **Drop** | Meta, rarely needed. |
| finishing-a-development-branch | **Absorb** | A few lines of preference; belongs in CLAUDE.md. |

## Recommended rewrite

Fork into a personal skill set (`~/.claude/skills/` or per-repo `.claude/skills/`) —
do **not** edit the plugin cache; it gets overwritten on update. Three skills:

1. **brainstorming** — trimmed copy. Drop the visual-companion consent message
   (default-yes per existing preference). Keep: one-question-at-a-time flow, 2–3
   approaches with recommendation, spec written to `docs/superpowers/specs/`, user
   review gate before planning.

2. **planning** — the big rewrite. A plan specifies *what and where*: files touched,
   interfaces/signatures, the list of test cases to write, acceptance criteria,
   ordering. It does **not** contain implementation code. Roughly halves planning
   tokens and removes the write-it-twice problem while keeping what matters (forcing
   design decisions before code).

3. **tdd** — keep nearly verbatim from the original.

**Execution model** (replaces SDD / executing-plans):
- Small phases: run inline with TDD.
- Big phases: a Workflow script with **one** combined review pass per task
  (spec + quality together), escalating to a second reviewer only when the first
  finds issues. Cuts per-task agent count from 3+ to ~2.

Then **disable the superpowers plugin** so the session hook and always-invoke
pressure go away.

## Caveats / migration notes

- foundation-lacrosse docs and existing plans reference the superpowers flow
  (`docs/superpowers/` paths, plan headers naming
  `superpowers:subagent-driven-development`). The fork should keep those paths so
  nothing breaks; the in-flight Phase 4a plan can execute under either regime.
- Open design decisions for the rewrite: how much review per task (proportionality
  rules), where skills live (user-global vs per-repo), whether to keep
  systematic-debugging at all.
