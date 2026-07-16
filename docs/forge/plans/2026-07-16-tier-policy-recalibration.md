# Tier Policy Recalibration Implementation Plan

> **For agentic workers:** Execute task-by-task following the Execution section
> of the planning skill, with strict TDD per task. Checkboxes track progress.

**Goal:** Reframe forge task classification to floor+evidence and lower per-tier effort defaults on both harnesses, collapsing reviewer routing to task-tier fresh-context.
**Architecture:** Codex runner code (`forge_common.py` maps + `Task` field, `forge_plan.py` justification parse/validation, `forge-run.py` reviewer routing + max-tier final review) carries the mechanical/enforcement half; the planning-skill docs carry the classifier steering and the Claude-path routing; the codex-exec-runner spec is amended to defer to the new owner.
**Tech stack:** Python 3 (stdlib only), pytest; markdown skill/spec docs.

Note: this plan predates the plan-format it introduces — its own `Tier:` lines use the current bare form (no `— justification`), since it executes under the current toolchain. Up-tier justifications are disclosed in the execution offer, not the plan file.

### Task 1: Recalibrate TIER_MAP, add justification field + tier ordering
- [ ] Done

**Files:**
- Modify: `scripts/forge_common.py` (recalibrate `TIER_MAP` effort values; add `Task.tier_justification`; add `TIER_ORDER`)
- Test: `tests/test_forge_plan.py`, `tests/test_forge_dispatch.py`, `tests/test_forge_review.py` (update any assertions on old TIER_MAP effort values)

**Interface:**
```
TIER_MAP = {
    "trivial": ("gpt-5.6-luna", "low"),
    "standard": ("gpt-5.6-terra", "medium"),
    "complex": ("gpt-5.6-sol", "medium"),
}
TIER_ORDER = ("trivial", "standard", "complex")  # ascending; index gives rank
# dataclass Task gains:
tier_justification: str | None = None   # after `tier`, before `depends_on`; keyword-defaulted
```
`REVIEW_MAP` is left untouched here (removed in Task 3 with its callers, so the tree stays working).

**Tests:** TIER_MAP resolves trivial→luna·low, standard→terra·medium, complex→sol·medium; TIER_ORDER ranks trivial<standard<complex; a `Task` constructs with `tier_justification` defaulting to None and accepts an explicit value.

**Acceptance:** `python3 -m pytest tests/test_forge_plan.py tests/test_forge_dispatch.py tests/test_forge_review.py -q` passes.

**Tier:** trivial

**Depends on:** nothing.

### Task 2: Parse and enforce the Tier justification in the plan parser
- [ ] Done

**Files:**
- Modify: `scripts/forge_plan.py` (parse the `Tier:` field into level + justification; presence-validate off-floor tiers; set `task.tier_justification`)
- Test: `tests/test_forge_plan.py`

**Spec:** Classification contract — the `Tier:` plan field, Enforcement

**Interface:** the `Tier:` field value is parsed as `<level>[ — <justification>]`, split on the em dash `—`. `level` is lowercased and validated against `TIER_MAP` (existing check retained). `justification` is the stripped remainder (None when absent). `standard` ignores/clears any justification (stored None); `complex`/`trivial` with an empty or absent justification raise a `RuntimeError` naming the task number and stating the justification is a contract requirement. The parser checks **presence only** — never justification quality.

**Tests:** parses `Tier: complex — reconciles two retry semantics` into level `complex` + that justification; parses bare `Tier: standard` into `standard`/None; a bare `Tier: complex` (no `—`/justification) raises RuntimeError naming the task; `Tier: trivial` without justification raises; `Tier: standard — anything` stores None (standard needs none); an unknown level still raises the existing unknown-tier error.

**Acceptance:** `python3 -m pytest tests/test_forge_plan.py -q` passes.

**Tier:** standard

**Depends on:** Task 1.

### Task 3: Reviewer routing = task tier; final review at plan max tier; retire REVIEW_MAP
- [ ] Done

**Files:**
- Modify: `scripts/forge-run.py` (`dispatch_reviewer` reads `TIER_MAP[task.tier]`; `dispatch_final_review` takes a resolved tier and reads `TIER_MAP[tier]`; `run_plan` computes the plan's max tier and passes it; drop the `REVIEW_MAP` import)
- Modify: `scripts/forge_common.py` (remove `REVIEW_MAP`)
- Test: `tests/test_forge_review.py`

**Spec:** Reviewer model

**Interface:**
```
def dispatch_final_review(packet_path, codex_bin, run_dir, tier, timeout=DEFAULT_TIMEOUT):
    # model, effort = TIER_MAP[tier]
# run_plan computes: max(tasks, key=lambda t: TIER_ORDER.index(t.tier)).tier
# dispatch_reviewer: model, effort = TIER_MAP[task.tier]   (was REVIEW_MAP)
```
`REVIEW_MAP` is deleted from `forge_common.py`; no code references it afterward. The final-review header/receipt record the resolved model+effort.

**Tests:** a standard task's reviewer routes to terra·medium and a complex task's to sol·medium (via TIER_MAP, not a removed REVIEW_MAP); the final review of an all-standard plan routes to standard-tier (terra·medium); the final review of a plan containing a complex task routes to complex-tier (sol·medium); `REVIEW_MAP` is absent from the module namespace.

**Acceptance:** `python3 -m pytest tests/test_forge_review.py -q` passes, and `test -z "$(grep -rn REVIEW_MAP scripts/ tests/)"`.

**Tier:** standard

**Depends on:** Task 1.

### Task 4: Rewrite the classifier steering and Claude-path routing in the skill docs
- [ ] Done

**Files:**
- Modify: `skills/planning/SKILL.md` (Tier definition + `Tier:` field format; self-review directional burden; Execution routing table; offer disclosure of justifications; reviewer/proportional/final-review rules)
- Modify: `skills/planning/codex-execution.md` (reviewer routing = task tier; REVIEW_MAP retirement note; final review at plan max tier)

**Spec:** Tier model, Classification contract — the `Tier:` plan field, Routing — model·effort per tier, Reviewer model, Enforcement

**Interface:** the changes the docs must reflect (prose, no code):
- Tier §: standard is the default floor; up→complex requires a **named** design decision / cross-cutting invariant, down→trivial requires demonstrated mechanicalness; file count / category / "touches core" / "feels complex" are explicitly not evidence.
- `Tier:` field format: `standard` (no justification) / `complex — <named decision>` / `trivial — <mechanical rationale>`.
- Self-review: replace the symmetric tier check with the directional burden (every non-standard tier carries a valid, non-categorical justification, else pushed to standard).
- Execution routing table: standard → `forge-standard` sonnet · **medium**; complex → `forge-deep` opus · **high**; trivial → `forge-light` haiku (unchanged).
- Offer: disclose the resolved routing **and each non-standard task's justification** in the tier breakdown.
- Reviewer rules: reviewer at task tier with fresh context; remove the "second reviewer on forge-deep if the first finds issues" escalation; final review at the plan's highest task tier (not pinned forge-deep). `codex-execution.md`: reviewer reads `TIER_MAP`, `REVIEW_MAP` retired.

**Tests:** none (documentation) — verified by the acceptance greps.

**Acceptance:** all pass:
`grep -q "sonnet · medium" skills/planning/SKILL.md && grep -q "opus · high" skills/planning/SKILL.md && ! grep -q "opus · xhigh" skills/planning/SKILL.md && ! grep -q "REVIEW_MAP" skills/planning/codex-execution.md && grep -qi "fresh context" skills/planning/SKILL.md`

**Tier:** standard

**Depends on:** nothing.

### Task 5: Amend the codex-exec-runner spec to defer to the new tier-policy spec
- [ ] Done

**Files:**
- Modify: `docs/forge/specs/2026-07-13-codex-exec-runner-design.md` (update the Tier-mapping table + reviewer-routing rows to the new values; add a `## Changelog` line dated 2026-07-16 pointing to `2026-07-16-tier-policy-recalibration-design.md` as the owning spec)

**Tests:** none — verified by the acceptance greps.

**Acceptance:** `grep -q "2026-07-16-tier-policy-recalibration" docs/forge/specs/2026-07-13-codex-exec-runner-design.md && grep -q "terra | medium" docs/forge/specs/2026-07-13-codex-exec-runner-design.md`

**Tier:** trivial

**Depends on:** nothing.
