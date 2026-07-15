"""Tests for scripts/forge-run.py (Task 2: plan loop, dispatch, receipts, ledger).

Loaded via importlib since the script filename contains a hyphen. Task 3 tests
(review, rework cap, halt, resume, final review) are added later and are excluded
from this task's acceptance via `-k "not review and not resume"`; nothing here
uses those words in a node id.

The fake `codex` executable records its argv and replays scripted exit codes and
last-messages, so dispatch is observable without a live Codex CLI.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "forge-run.py"

_spec = importlib.util.spec_from_file_location("forge_run", SCRIPT_PATH)
forge_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(forge_run)


# A fake `codex` binary: appends its argv (JSON) to FORGE_FAKE_LOG, reads a
# per-call response from FORGE_FAKE_RESPONSES ([{"exit":int,"msg":str}, ...],
# index = prior log line count, clamped to last), writes msg to the
# --output-last-message path, and exits with the scripted code.
FAKE_CODEX_SRC = '''#!/usr/bin/env python3
import json, os, sys, time
argv = sys.argv[1:]
log = os.environ.get("FORGE_FAKE_LOG")
idx = 0
if log:
    if os.path.exists(log):
        with open(log) as f:
            idx = sum(1 for _ in f)
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\\n")
exit_code = 0
msg = ""
sleep_s = 0
resp = os.environ.get("FORGE_FAKE_RESPONSES")
if resp and os.path.exists(resp):
    with open(resp) as f:
        responses = json.load(f)
    if responses:
        r = responses[idx] if idx < len(responses) else responses[-1]
        exit_code = r.get("exit", 0)
        msg = r.get("msg", "")
        sleep_s = r.get("sleep", 0)
if sleep_s:
    time.sleep(sleep_s)
if "--output-last-message" in argv:
    p = argv[argv.index("--output-last-message") + 1]
    with open(p, "w") as f:
        f.write(msg)
sys.exit(exit_code)
'''


def write_fake_codex(dirpath):
    path = os.path.join(dirpath, "fake_codex.py")
    with open(path, "w") as f:
        f.write(FAKE_CODEX_SRC)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


PLAN_PASS = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: First task
- [ ] Done

**Files:**
- Modify: `foo.txt`

**Acceptance:** `true`

**Tier:** trivial

**Depends on:** nothing
"""

# Task 2 listed before Task 1 in the file; Task 2 depends on Task 1. A correct
# runner dispatches Task 1 first regardless of file order.
PLAN_DEPS = """# Fixture Plan

**Goal:** Do the thing.

### Task 2: Second task
- [ ] Done

**Acceptance:** `true`

**Tier:** trivial

**Depends on:** Task 1

### Task 1: First task
- [ ] Done

**Acceptance:** `true`

**Tier:** trivial

**Depends on:** nothing
"""

PLAN_ACC_FAIL = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: First task
- [ ] Done

**Acceptance:** `false`

**Tier:** trivial

**Depends on:** nothing
"""

PLAN_BAD_HEADING = """# Fixture Plan

**Goal:** Do the thing.

## Task 1: Wrong level
- [ ] Done

**Acceptance:** `true`

**Tier:** trivial

**Depends on:** nothing
"""

PLAN_DUP = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: First
- [ ] Done

**Acceptance:** `true`

**Tier:** trivial

**Depends on:** nothing

### Task 1: Second
- [ ] Done

**Acceptance:** `true`

**Tier:** trivial

**Depends on:** nothing
"""

MINIMAL_SPEC = "# Spec\n\nNothing referenced.\n"

# A single standard-tier task: acceptance passes, so a reviewer is dispatched.
PLAN_STD = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: Standard task
- [ ] Done

**Acceptance:** `true`

**Tier:** standard

**Depends on:** nothing
"""

# Standard task 1 (reviewed) followed by a trivial task 2 that depends on it —
# used to prove a halt at task 1 never dispatches task 2.
PLAN_STD_THEN_TRIVIAL = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: Standard task
- [ ] Done

**Acceptance:** `true`

**Tier:** standard

**Depends on:** nothing

### Task 2: Trivial follow-up
- [ ] Done

**Acceptance:** `true`

**Tier:** trivial

**Depends on:** Task 1
"""

# Two trivial tasks, task 2 depends on task 1 — used by the resume test where the
# escalation is driven by a worker crash (no reviewer, so no git repo required).
PLAN_TWO_TRIVIAL = PLAN_DEPS

# Two standard (reviewed) tasks, each appending a distinct marker to its OWN
# tracked file via its acceptance command. Proves per-task review packets are
# isolated to that task's own diff: task 1 commits when it passes, so task 2's
# base is that commit and its packet carries only task 2's change.
PLAN_TWO_STD = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: First standard
- [ ] Done

**Acceptance:** `echo TASK1MARK >> f1.txt`

**Tier:** standard

**Depends on:** nothing

### Task 2: Second standard
- [ ] Done

**Acceptance:** `echo TASK2MARK >> f2.txt`

**Tier:** standard

**Depends on:** Task 1
"""


def _pass_msg():
    return '{"verdict": "pass"}'


def _findings_msg(*items):
    return json.dumps({"verdict": "findings", "findings": list(items)})


# --- Phase 5: commit discipline fixtures -----------------------------------

# One trivial task whose acceptance command mutates a tracked file, so a passed
# task has something to commit.
PLAN_COMMIT_ONE = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: First task
- [ ] Done

**Acceptance:** `echo ONEMARK >> f1.txt`

**Tier:** trivial

**Depends on:** nothing
"""

# Two trivial tasks, each mutating its own tracked file; task 2 depends on task 1.
PLAN_COMMIT_TWO = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: First task
- [ ] Done

**Acceptance:** `echo ONEMARK >> f1.txt`

**Tier:** trivial

**Depends on:** nothing

### Task 2: Second task
- [ ] Done

**Acceptance:** `echo TWOMARK >> f2.txt`

**Tier:** trivial

**Depends on:** Task 1
"""

# One standard (reviewed) task mutating a tracked file — used to force an
# escalation (two findings verdicts) and assert no commit is created.
PLAN_COMMIT_STD = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: Standard task
- [ ] Done

**Acceptance:** `echo STDMARK >> f1.txt`

**Tier:** standard

**Depends on:** nothing
"""

# Task 1 trivial (commits on run 1), task 2 standard (reviewed) so it can be
# forced to escalate via findings — used to prove the final-review base survives
# a resume as the persisted base_commit.
PLAN_COMMIT_ONE_THEN_STD = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: First task
- [ ] Done

**Acceptance:** `echo ONEMARK >> f1.txt`

**Tier:** trivial

**Depends on:** nothing

### Task 2: Second task
- [ ] Done

**Acceptance:** `echo TWOMARK >> f2.txt`

**Tier:** standard

**Depends on:** Task 1
"""

# A passed task that changes no tracked file (acceptance is a no-op) — commit
# must be skipped rather than creating an empty commit.
PLAN_COMMIT_NOOP = """# Fixture Plan

**Goal:** Do the thing.

### Task 1: No-op task
- [ ] Done

**Acceptance:** `true`

**Tier:** trivial

**Depends on:** nothing
"""


class CommitDisciplineTests(unittest.TestCase):
    """Phase 5: the runner commits each passed task, refuses a dirty tree at
    invocation start, uses the prior commit as each task's review base, and
    persists ``base_commit`` for a whole-plan final diff across resume. These
    need a real git repo; harness artifacts are gitignored so the tree is clean
    at run start (mirroring real usage)."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="forge-run-commit-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.fake = write_fake_codex(self.d)
        self.spec = os.path.join(self.d, "spec.md")
        with open(self.spec, "w") as f:
            f.write(MINIMAL_SPEC)
        self.run_dir = os.path.join(self.d, "run")
        self.log = os.path.join(self.d, "fakelog")

    def _git(self, *args, check=True):
        return subprocess.run(
            ["git", *args], cwd=self.d, check=check, capture_output=True, text=True
        )

    def _init_repo(self, tracked=("f1.txt", "f2.txt")):
        # Harness artifacts are committed as ignored so the working tree is clean
        # at run start; the runner's own `.forge/` is also ignored.
        with open(os.path.join(self.d, ".gitignore"), "w") as f:
            f.write("fakelog\nresponses.json\nrun/\n.forge/\n")
        for name in tracked:
            with open(os.path.join(self.d, name), "w") as f:
                f.write("base\n")
        self._git("init")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", "-A")
        self._git("commit", "-m", "base")

    def _plan(self, content, name="plan.md"):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(content)
        return p

    def _run(self, plan_path, responses=None):
        if os.path.exists(self.log):
            os.remove(self.log)
        env = os.environ.copy()
        env["FORGE_FAKE_LOG"] = self.log
        if responses is not None:
            resp_path = os.path.join(self.d, "responses.json")
            with open(resp_path, "w") as f:
                json.dump(responses, f)
            env["FORGE_FAKE_RESPONSES"] = resp_path
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), plan_path,
             "--spec", self.spec, "--run-dir", self.run_dir,
             "--codex-bin", self.fake],
            cwd=self.d, capture_output=True, text=True, env=env,
        )

    def _head(self):
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _log_subjects(self):
        return self._git("log", "--format=%s").stdout.strip().splitlines()

    def test_dirty_tree_at_start_exits_one_naming_path(self):
        plan = self._plan(PLAN_COMMIT_ONE)
        self._init_repo()
        # Dirty a tracked file before the run.
        with open(os.path.join(self.d, "f1.txt"), "a") as f:
            f.write("uncommitted\n")
        res = self._run(plan, responses=[{"exit": 0, "msg": ""}])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("f1.txt", res.stderr)

    def test_dirty_tree_at_start_exits_one_on_resume(self):
        plan = self._plan(PLAN_COMMIT_ONE)
        self._init_repo()
        # First run passes and commits (a commit triggers the final review).
        res1 = self._run(plan, responses=[{"exit": 0, "msg": ""},
                                          {"exit": 0, "msg": _pass_msg()}])
        self.assertEqual(res1.returncode, 0, res1.stderr)
        # Now dirty the tree and resume (same run-dir).
        with open(os.path.join(self.d, "f2.txt"), "a") as f:
            f.write("uncommitted\n")
        res2 = self._run(plan, responses=[{"exit": 0, "msg": ""}])
        self.assertEqual(res2.returncode, 1, res2.stderr)
        self.assertIn("f2.txt", res2.stderr)

    def test_passed_task_creates_one_commit_with_message(self):
        plan = self._plan(PLAN_COMMIT_ONE)
        self._init_repo()
        base = self._head()
        res = self._run(plan, responses=[{"exit": 0, "msg": ""},
                                         {"exit": 0, "msg": _pass_msg()}])  # final review
        self.assertEqual(res.returncode, 0, res.stderr)
        subjects = self._log_subjects()
        self.assertEqual(subjects[0], "forge: task 1 — First task")
        # Exactly one new commit past base.
        self.assertNotEqual(self._head(), base)
        self.assertEqual(len(subjects), 2)  # base + one task commit

    def test_two_passed_tasks_one_commit_each_head_advances(self):
        plan = self._plan(PLAN_COMMIT_TWO)
        self._init_repo()
        res = self._run(plan, responses=[{"exit": 0, "msg": ""},
                                         {"exit": 0, "msg": ""},
                                         {"exit": 0, "msg": _pass_msg()}])  # final review
        self.assertEqual(res.returncode, 0, res.stderr)
        subjects = self._log_subjects()
        self.assertEqual(subjects[0], "forge: task 2 — Second task")
        self.assertEqual(subjects[1], "forge: task 1 — First task")
        self.assertEqual(len(subjects), 3)  # base + two task commits
        # Each commit isolates its own file change.
        t1 = self._git("show", "--stat", "HEAD~1").stdout
        self.assertIn("f1.txt", t1)
        self.assertNotIn("f2.txt", t1)

    def test_escalated_task_creates_no_commit(self):
        plan = self._plan(PLAN_COMMIT_STD)
        self._init_repo()
        base = self._head()
        res = self._run(plan, responses=[
            {"exit": 0, "msg": ""},                        # worker a1
            {"exit": 0, "msg": _findings_msg("f1.txt:1 - x")},  # review a1
            {"exit": 0, "msg": ""},                        # worker a2
            {"exit": 0, "msg": _findings_msg("f1.txt:1 - x")},  # review a2 (cap)
        ])
        self.assertEqual(res.returncode, 2, res.stderr)
        # No task commit — HEAD unchanged from base.
        self.assertEqual(self._head(), base)
        self.assertEqual(len(self._log_subjects()), 1)

    def test_base_commit_persisted_in_run_json(self):
        plan = self._plan(PLAN_COMMIT_ONE)
        self._init_repo()
        base = self._head()
        res = self._run(plan, responses=[{"exit": 0, "msg": ""},
                                         {"exit": 0, "msg": _pass_msg()}])  # final review
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(self.run_dir, "run.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["base_commit"], base)
        # The passed task records its commit SHA.
        t1 = next(t for t in summary["tasks"] if t["number"] == 1)
        self.assertEqual(t1["commit"], self._head())

    def test_final_review_base_is_persisted_base_commit_across_resume(self):
        # Task 1 passes and commits on run 1 (HEAD moves). Task 2 escalates, so
        # run 1 halts. On resume (run 2), task 2 passes; the final review must
        # diff the ORIGINAL base_commit (before task 1), not run-2's HEAD — so
        # its packet carries BOTH task markers.
        plan = self._plan(PLAN_COMMIT_ONE_THEN_STD)
        self._init_repo()
        base = self._head()
        res1 = self._run(plan, responses=[
            {"exit": 0, "msg": ""},                             # t1 worker (trivial)
            {"exit": 0, "msg": ""},                             # t2 worker a1
            {"exit": 0, "msg": _findings_msg("f2.txt:1 - x")},  # t2 review a1
            {"exit": 0, "msg": ""},                             # t2 worker a2
            {"exit": 0, "msg": _findings_msg("f2.txt:1 - x")},  # t2 review a2 (cap)
        ])
        self.assertEqual(res1.returncode, 2, res1.stderr)
        self.assertNotEqual(self._head(), base)  # task 1 committed
        # The escalated task's attempt left f2.txt dirty; the human discards it
        # before resume (the precondition requires a clean tree).
        self._git("reset", "--hard")
        # Resume: task 2 passes, then final review.
        res2 = self._run(plan, responses=[
            {"exit": 0, "msg": ""},           # t2 worker
            {"exit": 0, "msg": _pass_msg()},  # t2 review
            {"exit": 0, "msg": _pass_msg()},  # final review
        ])
        self.assertEqual(res2.returncode, 0, res2.stderr)
        with open(os.path.join(self.run_dir, "run.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["base_commit"], base)
        with open(os.path.join(self.run_dir, "final-review.md")) as f:
            packet = f.read()
        self.assertIn("ONEMARK", packet)
        self.assertIn("TWOMARK", packet)

    def test_noop_task_skips_commit_no_empty_commit(self):
        self._init_repo()
        # Plan lives outside the repo, so its ledger annotation doesn't dirty the
        # tree; the task's acceptance (`true`) changes nothing in the repo, so the
        # stage is empty and the commit must be skipped (no empty commit).
        plandir = tempfile.mkdtemp(prefix="forge-run-noop-plan-")
        self.addCleanup(shutil.rmtree, plandir, ignore_errors=True)
        plan = os.path.join(plandir, "plan.md")
        with open(plan, "w") as f:
            f.write(PLAN_COMMIT_NOOP)
        base = self._head()
        res = self._run(plan, responses=[{"exit": 0, "msg": ""}])
        self.assertEqual(res.returncode, 0, res.stderr)
        # No file changed -> no commit; HEAD unchanged, summary commit is null.
        self.assertEqual(self._head(), base)
        with open(os.path.join(self.run_dir, "run.json")) as f:
            summary = json.load(f)
        t1 = next(t for t in summary["tasks"] if t["number"] == 1)
        self.assertIsNone(t1["commit"])

    def test_commit_task_raises_loud_when_git_add_fails(self):
        # `git add -A` failure must fail loud (like every other git call), not
        # silently fall through to an empty-stage skip that drops the task's real
        # changes with no error. Forced deterministically via an unwritable index.
        self._init_repo()
        with open(os.path.join(self.d, "f1.txt"), "a") as f:
            f.write("change\n")
        task = types.SimpleNamespace(number=1, title="First")
        bad_index = os.path.join(self.d, "nonexistent-dir", "index")
        prev = os.environ.get("GIT_INDEX_FILE")
        os.environ["GIT_INDEX_FILE"] = bad_index

        def _restore():
            if prev is None:
                os.environ.pop("GIT_INDEX_FILE", None)
            else:
                os.environ["GIT_INDEX_FILE"] = prev
        self.addCleanup(_restore)
        with self.assertRaises(RuntimeError) as cm:
            forge_run._git_commit_task(self.d, task)
        self.assertIn("git add", str(cm.exception).lower())

    def test_snapshot_worktree_is_retired(self):
        # The stash-snapshot per-task base is replaced by the prior commit.
        self.assertFalse(hasattr(forge_run, "_snapshot_worktree"))
        src = SCRIPT_PATH.read_text()
        self.assertNotIn("_snapshot_worktree", src)


class ParsePlanTasksTests(unittest.TestCase):
    def _write(self, content):
        d = tempfile.mkdtemp(prefix="forge-run-parse-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "plan.md")
        with open(p, "w") as f:
            f.write(content)
        return p

    def test_parses_number_title_tier_depends_acceptance(self):
        tasks = forge_run.parse_plan_tasks(self._write(PLAN_DEPS))
        by_num = {t.number: t for t in tasks}
        self.assertEqual(set(by_num), {1, 2})
        self.assertEqual(by_num[1].title, "First task")
        self.assertEqual(by_num[1].tier, "trivial")
        self.assertEqual(by_num[1].depends_on, [])
        self.assertEqual(by_num[1].acceptance_commands, ["true"])
        self.assertEqual(by_num[2].depends_on, [1])

    def test_checkbox_line_points_at_done_line(self):
        p = self._write(PLAN_PASS)
        tasks = forge_run.parse_plan_tasks(p)
        with open(p) as f:
            lines = f.read().splitlines()
        idx = tasks[0].checkbox_line
        self.assertIn("[ ]", lines[idx])

    def test_wrong_level_heading_raises_naming_cause(self):
        with self.assertRaises(RuntimeError) as ctx:
            forge_run.parse_plan_tasks(self._write(PLAN_BAD_HEADING))
        msg = str(ctx.exception)
        self.assertIn("### Task 1:", msg)
        self.assertIn("## Task 1:", msg)

    def test_duplicate_task_number_raises_naming_cause(self):
        with self.assertRaises(RuntimeError) as ctx:
            forge_run.parse_plan_tasks(self._write(PLAN_DUP))
        self.assertIn("1", str(ctx.exception))
        self.assertIn("duplicate", str(ctx.exception).lower())


class DispatchWorkerTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="forge-run-dispatch-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.fake = write_fake_codex(self.d)
        self.brief = os.path.join(self.d, "brief.md")
        with open(self.brief, "w") as f:
            f.write("# Task brief\n")

    def test_tier_resolution_emits_exact_model_effort_argv(self):
        for tier, (model, effort) in forge_run.TIER_MAP.items():
            run_dir = os.path.join(self.d, "run-" + tier)
            os.makedirs(run_dir, exist_ok=True)
            task = forge_run.Task(number=1, title="t", tier=tier)
            res = forge_run.dispatch_worker(task, self.brief, self.fake, run_dir)
            argv = res.argv
            self.assertIn("exec", argv)
            self.assertIn("-m", argv)
            self.assertIn(model, argv)
            self.assertIn("-c", argv)
            self.assertIn("model_reasoning_effort=" + effort, argv)
            self.assertIn("--output-last-message", argv)

    def test_ultra_never_appears_in_emitted_argv(self):
        for tier in forge_run.TIER_MAP:
            run_dir = os.path.join(self.d, "runu-" + tier)
            os.makedirs(run_dir, exist_ok=True)
            task = forge_run.Task(number=1, title="t", tier=tier)
            res = forge_run.dispatch_worker(task, self.brief, self.fake, run_dir)
            self.assertNotIn("ultra", " ".join(res.argv))

    def test_prompt_carries_contract_preamble_and_brief(self):
        run_dir = os.path.join(self.d, "run-prompt")
        os.makedirs(run_dir, exist_ok=True)
        task = forge_run.Task(number=1, title="t", tier="trivial")
        res = forge_run.dispatch_worker(task, self.brief, self.fake, run_dir)
        prompt = res.argv[-1]
        self.assertIn("# Task brief", prompt)
        self.assertIn("forge execution worker", prompt)

    def test_missing_contract_source_raises(self):
        empty = tempfile.mkdtemp(prefix="forge-run-noagents-")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        old = os.environ.get("FORGE_AGENTS_DIR")
        os.environ["FORGE_AGENTS_DIR"] = empty
        self.addCleanup(
            lambda: os.environ.__setitem__("FORGE_AGENTS_DIR", old)
            if old is not None
            else os.environ.pop("FORGE_AGENTS_DIR", None)
        )
        run_dir = os.path.join(self.d, "run-noagents")
        os.makedirs(run_dir, exist_ok=True)
        task = forge_run.Task(number=1, title="t", tier="trivial")
        with self.assertRaises(RuntimeError):
            forge_run.dispatch_worker(task, self.brief, self.fake, run_dir)


class RunAcceptanceTests(unittest.TestCase):
    def test_success_and_failure_recorded_per_command(self):
        d = tempfile.mkdtemp(prefix="forge-run-acc-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        task = forge_run.Task(
            number=1, title="t", tier="trivial",
            acceptance_commands=["true", "false"],
        )
        results = forge_run.run_acceptance(task, d)
        self.assertEqual([r.command for r in results], ["true", "false"])
        self.assertEqual(results[0].exit_code, 0)
        self.assertNotEqual(results[1].exit_code, 0)


class AnnotateLedgerTests(unittest.TestCase):
    def test_checks_box_and_appends_status(self):
        d = tempfile.mkdtemp(prefix="forge-run-ledger-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "plan.md")
        with open(p, "w") as f:
            f.write(PLAN_PASS)
        task = forge_run.parse_plan_tasks(p)[0]
        forge_run.annotate_ledger(p, task, "passed, 1 attempt(s)")
        with open(p) as f:
            content = f.read()
        self.assertIn("[x] Done", content)
        self.assertIn("passed, 1 attempt(s)", content)

    def test_escalated_leaves_checkbox_unchecked(self):
        d = tempfile.mkdtemp(prefix="forge-run-ledger-esc-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "plan.md")
        with open(p, "w") as f:
            f.write(PLAN_PASS)
        task = forge_run.parse_plan_tasks(p)[0]
        forge_run.annotate_ledger(p, task, "escalated: worker exited 1")
        with open(p) as f:
            content = f.read()
        self.assertIn("[ ] Done", content)
        self.assertNotIn("[x] Done", content)
        self.assertIn("escalated: worker exited 1", content)


class LoopSubprocessTests(unittest.TestCase):
    """End-to-end: invoke forge-run.py as a subprocess with a fake codex on the
    --codex-bin seam and the plan's dir as cwd (so acceptance commands run there)."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="forge-run-loop-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.fake = write_fake_codex(self.d)
        self.spec = os.path.join(self.d, "spec.md")
        with open(self.spec, "w") as f:
            f.write(MINIMAL_SPEC)
        self.run_dir = os.path.join(self.d, "run")
        self.log = os.path.join(self.d, "fakelog")

    def _plan(self, content, name="plan.md"):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(content)
        return p

    def _run(self, plan_path, responses=None):
        env = os.environ.copy()
        env["FORGE_FAKE_LOG"] = self.log
        if responses is not None:
            resp_path = os.path.join(self.d, "responses.json")
            with open(resp_path, "w") as f:
                json.dump(responses, f)
            env["FORGE_FAKE_RESPONSES"] = resp_path
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), plan_path,
             "--spec", self.spec, "--run-dir", self.run_dir,
             "--codex-bin", self.fake],
            cwd=self.d, capture_output=True, text=True, env=env,
        )

    def test_help_exits_zero(self):
        res = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_passing_task_writes_receipt_with_all_fields_and_brief_sha(self):
        plan = self._plan(PLAN_PASS)
        res = self._run(plan)
        self.assertEqual(res.returncode, 0, res.stderr)
        receipt_path = os.path.join(self.run_dir, "task-1-attempt-1.json")
        with open(receipt_path) as f:
            receipt = json.load(f)
        for key in ("task_number", "title", "tier", "model", "effort",
                    "brief_path", "brief_sha256", "worker_exit_code",
                    "acceptance_results", "review_verdict", "attempt", "status"):
            self.assertIn(key, receipt)
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["tier"], "trivial")
        self.assertEqual(receipt["model"], "gpt-5.6-luna")
        self.assertEqual(receipt["effort"], "medium")
        import hashlib
        with open(receipt["brief_path"], "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(receipt["brief_sha256"], actual)

    def test_run_json_summarizes_task_statuses(self):
        plan = self._plan(PLAN_PASS)
        res = self._run(plan)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(self.run_dir, "run.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["status"], "passed")
        statuses = {t["number"]: t["status"] for t in summary["tasks"]}
        self.assertEqual(statuses[1], "passed")

    def test_ledger_annotated_passed_with_attempts(self):
        plan = self._plan(PLAN_PASS)
        res = self._run(plan)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(plan) as f:
            content = f.read()
        self.assertIn("[x] Done", content)
        self.assertIn("passed, 1 attempt(s)", content)

    def test_depends_on_order_dependency_dispatched_first(self):
        plan = self._plan(PLAN_DEPS)
        res = self._run(plan)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(self.log) as f:
            log_lines = f.read().splitlines()
        # Each line is the argv of one worker dispatch; the --output-last-message
        # path names the task. Task 1 must be dispatched before Task 2.
        joined = "\n".join(log_lines)
        pos1 = joined.find("task-1-worker-last")
        pos2 = joined.find("task-2-worker-last")
        self.assertNotEqual(pos1, -1)
        self.assertNotEqual(pos2, -1)
        self.assertLess(pos1, pos2)

    def test_dependency_failure_halts_before_dependent_dispatched(self):
        # Task 1 (the dependency) fails; Task 2 depends on it and must never be
        # dispatched. Guards run_plan's break-on-escalation: a refactor that kept
        # looping would dispatch the dependent, and this test would catch it.
        plan = self._plan(PLAN_DEPS)
        res = self._run(plan, responses=[{"exit": 1, "msg": ""}])
        self.assertEqual(res.returncode, 2, res.stderr)
        with open(self.log) as f:
            log_lines = [ln for ln in f.read().splitlines() if ln.strip()]
        # Every worker dispatch is the failed dependency (task 1), never the
        # dependent (task 2). A crashing worker consumes the rework cap, so task 1
        # is dispatched more than once (initial + one rework) — the invariant under
        # test is that task 2 is never reached, not the exact attempt count.
        self.assertTrue(log_lines)
        self.assertTrue(
            all("task-1-worker-last" in ln for ln in log_lines), log_lines
        )
        self.assertNotIn("task-2-worker-last", "\n".join(log_lines))
        # Task 2's worker last-message file is never created.
        self.assertFalse(
            os.path.exists(os.path.join(self.run_dir, "task-2-worker-last.txt"))
        )

    def test_worker_nonzero_exit_marks_attempt_failed_and_halts(self):
        plan = self._plan(PLAN_PASS)
        res = self._run(plan, responses=[{"exit": 1, "msg": ""}])
        self.assertEqual(res.returncode, 2, res.stderr)
        with open(os.path.join(self.run_dir, "task-1-attempt-1.json")) as f:
            receipt = json.load(f)
        self.assertEqual(receipt["worker_exit_code"], 1)
        self.assertNotEqual(receipt["status"], "passed")

    def test_acceptance_failure_marks_attempt_failed_and_halts(self):
        plan = self._plan(PLAN_ACC_FAIL)
        res = self._run(plan)
        self.assertEqual(res.returncode, 2, res.stderr)
        with open(os.path.join(self.run_dir, "task-1-attempt-1.json")) as f:
            receipt = json.load(f)
        self.assertNotEqual(receipt["status"], "passed")
        self.assertTrue(
            any(r["exit_code"] != 0 for r in receipt["acceptance_results"])
        )

    def test_malformed_plan_bad_heading_exits_one_naming_cause(self):
        plan = self._plan(PLAN_BAD_HEADING)
        res = self._run(plan)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("### Task 1:", res.stderr)

    def test_malformed_plan_duplicate_number_exits_one_naming_cause(self):
        plan = self._plan(PLAN_DUP)
        res = self._run(plan)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("duplicate", res.stderr.lower())

    def test_run_writes_forge_gitignore(self):
        # Receipts spec (2026-07-13 amendment): on run-dir creation the runner
        # writes a self-ignoring `.forge/.gitignore` containing `*` — no
        # target-repo setup required.
        plan = self._plan(PLAN_PASS)
        res = self._run(plan)
        self.assertEqual(res.returncode, 0, res.stderr)
        gitignore_path = os.path.join(self.d, ".forge", ".gitignore")
        self.assertTrue(os.path.exists(gitignore_path))
        with open(gitignore_path) as f:
            content = f.read()
        self.assertEqual(content.strip(), "*")

    def test_missing_contract_source_cli_exits_one_naming_cause(self):
        # Spec Tests bullet: "missing agents/*.md contract source exits 1" —
        # driven through the CLI (not just the unit-level dispatch_worker raise).
        plan = self._plan(PLAN_PASS)
        empty = os.path.join(self.d, "no-agents")
        os.makedirs(empty, exist_ok=True)
        env = os.environ.copy()
        env["FORGE_FAKE_LOG"] = self.log
        env["FORGE_AGENTS_DIR"] = empty
        res = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), plan,
             "--spec", self.spec, "--run-dir", self.run_dir,
             "--codex-bin", self.fake],
            cwd=self.d, capture_output=True, text=True, env=env,
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("contract source", res.stderr.lower())


class ParseVerdictTests(unittest.TestCase):
    """parse_verdict: last parseable JSON object matching the two verdict shapes
    (fenced or bare); anything else raises naming the cause."""

    def test_bare_pass(self):
        v = forge_run.parse_verdict('{"verdict": "pass"}')
        self.assertEqual(v.kind, "pass")

    def test_findings_extracted_from_prose_and_fence(self):
        msg = (
            "Here is my review of the diff.\n\n"
            "```json\n"
            '{"verdict": "findings", "findings": ["a.py:3 - missing guard"]}\n'
            "```\n\nThat is all.\n"
        )
        v = forge_run.parse_verdict(msg)
        self.assertEqual(v.kind, "findings")
        self.assertEqual(v.findings, ["a.py:3 - missing guard"])

    def test_unparseable_prose_raises_naming_cause(self):
        with self.assertRaises(RuntimeError) as ctx:
            forge_run.parse_verdict("Looks good to me, ship it.")
        self.assertIn("verdict", str(ctx.exception).lower())

    def test_malformed_json_raises(self):
        with self.assertRaises(RuntimeError):
            forge_run.parse_verdict('{"verdict": ')

    def test_last_matching_object_wins(self):
        msg = (
            '{"verdict": "pass"}\n'
            "on reflection...\n"
            '{"verdict": "findings", "findings": ["x"]}'
        )
        v = forge_run.parse_verdict(msg)
        self.assertEqual(v.kind, "findings")
        self.assertEqual(v.findings, ["x"])


def _log_argvs(log_path):
    if not os.path.exists(log_path):
        return []
    with open(log_path) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def _find_dispatch(argvs, marker):
    """Return the first argv (list) whose --output-last-message path contains
    ``marker`` — distinguishes worker vs reviewer vs final-review calls."""
    for a in argvs:
        if "--output-last-message" in a:
            path = a[a.index("--output-last-message") + 1]
            if marker in path:
                return a
    return None


class DispatchReviewerUnitTests(unittest.TestCase):
    """dispatch_reviewer routes model/effort by REVIEW_MAP[tier] and returns the
    parsed Verdict — exercised directly against the fake codex (no plan loop)."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="forge-run-rev-unit-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.fake = write_fake_codex(self.d)
        self.packet = os.path.join(self.d, "packet.md")
        with open(self.packet, "w") as f:
            f.write("### Task 1: X\n\n```diff\n```\n")
        self.log = os.path.join(self.d, "log")
        self.resp = os.path.join(self.d, "resp.json")
        with open(self.resp, "w") as f:
            json.dump([{"exit": 0, "msg": '{"verdict": "pass"}'}], f)
        self._set_env("FORGE_FAKE_LOG", self.log)
        self._set_env("FORGE_FAKE_RESPONSES", self.resp)

    def _set_env(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(
            lambda: os.environ.__setitem__(key, old)
            if old is not None
            else os.environ.pop(key, None)
        )

    def _argv_for(self, marker):
        with open(self.log) as f:
            for ln in f:
                if not ln.strip():
                    continue
                a = json.loads(ln)
                if "--output-last-message" in a:
                    path = a[a.index("--output-last-message") + 1]
                    if marker in path:
                        return a
        return None

    def test_standard_reviewer_maps_terra_high(self):
        run_dir = os.path.join(self.d, "run-s")
        os.makedirs(run_dir)
        task = forge_run.Task(number=1, title="t", tier="standard")
        verdict = forge_run.dispatch_reviewer(task, self.packet, self.fake, run_dir)
        self.assertEqual(verdict.kind, "pass")
        argv = self._argv_for("task-1-review-last")
        self.assertIsNotNone(argv)
        self.assertIn("gpt-5.6-terra", argv)
        self.assertIn("model_reasoning_effort=high", argv)
        self.assertNotIn("ultra", " ".join(argv))

    def test_complex_reviewer_maps_sol_high(self):
        run_dir = os.path.join(self.d, "run-c")
        os.makedirs(run_dir)
        task = forge_run.Task(number=2, title="t", tier="complex")
        verdict = forge_run.dispatch_reviewer(task, self.packet, self.fake, run_dir)
        self.assertEqual(verdict.kind, "pass")
        argv = self._argv_for("task-2-review-last")
        self.assertIsNotNone(argv)
        self.assertIn("gpt-5.6-sol", argv)
        self.assertIn("model_reasoning_effort=high", argv)
        self.assertNotIn("ultra", " ".join(argv))


class ReviewLoopTests(unittest.TestCase):
    """Standard/complex review + rework + halt + final review. These need a git
    repo because the review packet is a ``git diff`` against the run baseline."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="forge-run-review-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.fake = write_fake_codex(self.d)
        self.spec = os.path.join(self.d, "spec.md")
        with open(self.spec, "w") as f:
            f.write(MINIMAL_SPEC)
        self.run_dir = os.path.join(self.d, "run")
        self.log = os.path.join(self.d, "fakelog")

    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=self.d, check=True, capture_output=True, text=True
        )

    def _init_repo(self):
        # Ignore harness artifacts so the working tree is clean at run start
        # (the commit-discipline precondition halts on a dirty tree).
        with open(os.path.join(self.d, ".gitignore"), "w") as f:
            f.write("fakelog\nresponses.json\nrun/\n.forge/\n")
        self._git("init")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", "-A")
        self._git("commit", "-m", "base")

    def _plan(self, content, name="plan.md"):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(content)
        return p

    def _run(self, plan_path, responses=None):
        if os.path.exists(self.log):
            os.remove(self.log)
        env = os.environ.copy()
        env["FORGE_FAKE_LOG"] = self.log
        if responses is not None:
            resp_path = os.path.join(self.d, "responses.json")
            with open(resp_path, "w") as f:
                json.dump(responses, f)
            env["FORGE_FAKE_RESPONSES"] = resp_path
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), plan_path,
             "--spec", self.spec, "--run-dir", self.run_dir,
             "--codex-bin", self.fake],
            cwd=self.d, capture_output=True, text=True, env=env,
        )

    def test_standard_dispatches_reviewer_with_mapped_model_and_passes(self):
        plan = self._plan(PLAN_STD)
        self._init_repo()
        res = self._run(plan, responses=[
            {"exit": 0, "msg": ""},           # worker
            {"exit": 0, "msg": _pass_msg()},  # reviewer (clamps for final review)
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        argvs = _log_argvs(self.log)
        rev = _find_dispatch(argvs, "task-1-review-last")
        self.assertIsNotNone(rev, argvs)
        self.assertIn("gpt-5.6-terra", rev)
        self.assertIn("model_reasoning_effort=high", rev)
        with open(os.path.join(self.run_dir, "task-1-attempt-1.json")) as f:
            receipt = json.load(f)
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["review_verdict"], {"verdict": "pass"})

    def test_findings_then_rework_carries_findings_text_in_worker_prompt(self):
        plan = self._plan(PLAN_STD)
        self._init_repo()
        res = self._run(plan, responses=[
            {"exit": 0, "msg": ""},                                  # worker a1
            {"exit": 0, "msg": _findings_msg("GUARDXYZ needed at a.py:3")},  # review a1
            {"exit": 0, "msg": ""},                                  # worker a2 (rework)
            {"exit": 0, "msg": _pass_msg()},                         # review a2
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        # The rework worker's brief carries the finding text; the fake logs the
        # full argv (prompt is the last arg), so the marker must appear there.
        with open(self.log) as f:
            self.assertIn("GUARDXYZ", f.read())

    def test_second_findings_verdict_halts_escalated_and_stops_next_task(self):
        plan = self._plan(PLAN_STD_THEN_TRIVIAL)
        self._init_repo()
        res = self._run(plan, responses=[
            {"exit": 0, "msg": ""},                              # t1 worker a1
            {"exit": 0, "msg": _findings_msg("a.py:1 - issue")}, # t1 review a1
            {"exit": 0, "msg": ""},                              # t1 worker a2
            {"exit": 0, "msg": _findings_msg("a.py:1 - still")}, # t1 review a2
        ])
        self.assertEqual(res.returncode, 2, res.stderr)
        with open(os.path.join(self.run_dir, "task-1-attempt-2.json")) as f:
            receipt = json.load(f)
        self.assertEqual(receipt["status"], "escalated")
        self.assertTrue(receipt["outstanding_findings"])
        # Task 2 is never dispatched.
        self.assertFalse(
            os.path.exists(os.path.join(self.run_dir, "task-2-worker-last.txt"))
        )
        # Ledger annotated escalated on task 1.
        with open(plan) as f:
            content = f.read()
        self.assertIn("escalated:", content)

    def test_unparseable_reviewer_verdict_exits_one_naming_cause(self):
        plan = self._plan(PLAN_STD)
        self._init_repo()
        res = self._run(plan, responses=[
            {"exit": 0, "msg": ""},                       # worker
            {"exit": 0, "msg": "looks good, no JSON"},    # reviewer: unparseable
        ])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("verdict", res.stderr.lower())

    def test_final_review_dispatched_sol_high_after_all_pass(self):
        plan = self._plan(PLAN_PASS)  # trivial task: no per-task reviewer
        self._init_repo()
        res = self._run(plan, responses=[
            {"exit": 0, "msg": ""},           # trivial worker
            {"exit": 0, "msg": _pass_msg()},  # final review
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        argvs = _log_argvs(self.log)
        fr = _find_dispatch(argvs, "final-review-last")
        self.assertIsNotNone(fr, argvs)
        self.assertIn("gpt-5.6-sol", fr)
        self.assertIn("model_reasoning_effort=high", fr)
        # A trivial task never dispatches a per-task reviewer.
        self.assertIsNone(_find_dispatch(argvs, "task-1-review-last"))

    def test_final_review_findings_exit_two_status_escalated_final_review(self):
        plan = self._plan(PLAN_PASS)
        self._init_repo()
        res = self._run(plan, responses=[
            {"exit": 0, "msg": ""},                                   # worker
            {"exit": 0, "msg": _findings_msg("spec drift at x")},     # final review
        ])
        self.assertEqual(res.returncode, 2, res.stderr)
        with open(os.path.join(self.run_dir, "run.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["status"], "escalated-final-review")

    def test_second_reviewed_task_packet_isolated_to_its_own_diff(self):
        # Two sequential standard tasks, each mutating its OWN tracked file. Task 1
        # commits when it passes, so task 2's per-task base is task 1's commit and
        # its packet carries only task 2's change — never task 1's.
        plan = self._plan(PLAN_TWO_STD)
        for name in ("f1.txt", "f2.txt"):
            with open(os.path.join(self.d, name), "w") as f:
                f.write("base\n")
        self._init_repo()
        res = self._run(plan, responses=[
            {"exit": 0, "msg": ""},           # t1 worker
            {"exit": 0, "msg": _pass_msg()},  # t1 review
            {"exit": 0, "msg": ""},           # t2 worker
            {"exit": 0, "msg": _pass_msg()},  # t2 review
            {"exit": 0, "msg": _pass_msg()},  # final review
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(self.run_dir, "task-1-review.md")) as f:
            p1 = f.read()
        with open(os.path.join(self.run_dir, "task-2-review.md")) as f:
            p2 = f.read()
        self.assertIn("TASK1MARK", p1)
        self.assertIn("TASK2MARK", p2)
        # The task-2 packet must not carry task 1's change (now committed).
        self.assertNotIn("TASK1MARK", p2)

    def test_reviewer_process_crash_exits_one_naming_cause(self):
        # The reviewer subprocess exits non-zero but still writes a parseable
        # verdict. A runner that discards the reviewer's exit code would trust the
        # message and pass; the runner must instead fail loud on a crashed
        # reviewer rather than silently trust (or reuse) its output.
        plan = self._plan(PLAN_STD)
        self._init_repo()
        res = self._run(plan, responses=[
            {"exit": 0, "msg": ""},            # worker
            {"exit": 3, "msg": _pass_msg()},   # reviewer crashes (exit 3)
        ])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("reviewer", res.stderr.lower())


class ReviewNonGitTests(unittest.TestCase):
    """Review-path behaviors that need no git repo: trivial tier skips the
    reviewer entirely, and a worker crash consumes rework iterations without ever
    reaching the reviewer."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="forge-run-review-nogit-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.fake = write_fake_codex(self.d)
        self.spec = os.path.join(self.d, "spec.md")
        with open(self.spec, "w") as f:
            f.write(MINIMAL_SPEC)
        self.run_dir = os.path.join(self.d, "run")
        self.log = os.path.join(self.d, "fakelog")

    def _plan(self, content, name="plan.md"):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(content)
        return p

    def _run(self, plan_path, responses=None):
        if os.path.exists(self.log):
            os.remove(self.log)
        env = os.environ.copy()
        env["FORGE_FAKE_LOG"] = self.log
        if responses is not None:
            resp_path = os.path.join(self.d, "responses.json")
            with open(resp_path, "w") as f:
                json.dump(responses, f)
            env["FORGE_FAKE_RESPONSES"] = resp_path
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), plan_path,
             "--spec", self.spec, "--run-dir", self.run_dir,
             "--codex-bin", self.fake],
            cwd=self.d, capture_output=True, text=True, env=env,
        )

    def test_trivial_tier_skips_reviewer_dispatch_entirely(self):
        # Non-git cwd: no final review either, so the log must show no reviewer.
        plan = self._plan(PLAN_PASS)
        res = self._run(plan)
        self.assertEqual(res.returncode, 0, res.stderr)
        argvs = _log_argvs(self.log)
        self.assertIsNone(_find_dispatch(argvs, "review-last"), argvs)

    def test_worker_crash_counts_as_failed_iteration_within_cap(self):
        # Standard tier, but the worker crashes every attempt so the reviewer is
        # never reached; two crashes hit the rework cap -> escalated, exit 2.
        plan = self._plan(PLAN_STD)
        res = self._run(plan, responses=[{"exit": 1, "msg": ""}])
        self.assertEqual(res.returncode, 2, res.stderr)
        argvs = _log_argvs(self.log)
        self.assertIsNone(_find_dispatch(argvs, "task-1-review-last"), argvs)
        with open(os.path.join(self.run_dir, "task-1-attempt-2.json")) as f:
            receipt = json.load(f)
        self.assertEqual(receipt["status"], "escalated")
        self.assertEqual(receipt["worker_exit_code"], 1)


class ResumeTests(unittest.TestCase):
    """Re-invocation with an existing --run-dir skips tasks whose latest receipt
    status is ``passed`` and resumes at the incomplete/escalated one. Trivial
    tasks + worker-crash escalation keep this off the git path."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="forge-run-resume-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.fake = write_fake_codex(self.d)
        self.spec = os.path.join(self.d, "spec.md")
        with open(self.spec, "w") as f:
            f.write(MINIMAL_SPEC)
        self.run_dir = os.path.join(self.d, "run")
        self.log = os.path.join(self.d, "fakelog")

    def _plan(self, content, name="plan.md"):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(content)
        return p

    def _run(self, plan_path, responses):
        # Fresh log every invocation so the fake's response index starts at 0 and
        # the log reflects only this invocation's dispatches.
        if os.path.exists(self.log):
            os.remove(self.log)
        env = os.environ.copy()
        env["FORGE_FAKE_LOG"] = self.log
        resp_path = os.path.join(self.d, "responses.json")
        with open(resp_path, "w") as f:
            json.dump(responses, f)
        env["FORGE_FAKE_RESPONSES"] = resp_path
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), plan_path,
             "--spec", self.spec, "--run-dir", self.run_dir,
             "--codex-bin", self.fake],
            cwd=self.d, capture_output=True, text=True, env=env,
        )

    def test_resume_skips_passed_tasks_and_resumes_at_escalated(self):
        plan = self._plan(PLAN_TWO_TRIVIAL)  # task 2 depends on task 1
        # Run 1: task 1 passes, task 2 crashes both attempts -> escalated, exit 2.
        res1 = self._run(plan, responses=[
            {"exit": 0, "msg": ""},  # task 1 worker
            {"exit": 1, "msg": ""},  # task 2 worker attempt 1
            {"exit": 1, "msg": ""},  # task 2 worker attempt 2
        ])
        self.assertEqual(res1.returncode, 2, res1.stderr)
        # Run 2 (same run-dir): task 1 is skipped (passed receipt); task 2 resumes
        # and now passes.
        res2 = self._run(plan, responses=[{"exit": 0, "msg": ""}])
        self.assertEqual(res2.returncode, 0, res2.stderr)
        joined = "\n".join(ln for ln in open(self.log).read().splitlines())
        self.assertNotIn("task-1-worker-last", joined)  # task 1 not re-dispatched
        self.assertIn("task-2-worker-last", joined)     # task 2 resumed
        with open(os.path.join(self.run_dir, "run.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["status"], "passed")
        with open(plan) as f:
            content = f.read()
        self.assertIn("[x] Done", content)

    def test_resume_forwards_to_run_plan_with_declared_signature(self):
        # resume(plan_path, spec_path, run_dir) is the documented re-invocation
        # entry: it forwards to run_plan with the production defaults (codex on
        # PATH, cwd = getcwd()). Guards against signature drift and dead code.
        calls = []
        orig = forge_run.run_plan

        def _record(*a, **k):
            calls.append(a)
            return 0

        forge_run.run_plan = _record
        try:
            rc = forge_run.resume("plan.md", "spec.md", "/run/dir")
        finally:
            forge_run.run_plan = orig
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertEqual(args[0], "plan.md")
        self.assertEqual(args[1], "spec.md")
        self.assertEqual(args[2], "/run/dir")
        self.assertEqual(args[3], "codex")
        self.assertEqual(args[4], os.getcwd())


class ParseEffortOverridesTests(unittest.TestCase):
    """parse_effort_overrides: repeatable --effort N=LEVEL entries -> {int: str}.
    Malformed entries and disallowed levels (including 'ultra') raise naming the
    cause; task-number existence is validated later, against the parsed plan."""

    def test_parses_single_override(self):
        overrides = forge_run.parse_effort_overrides(["3=max"])
        self.assertEqual(overrides, {3: "max"})

    def test_parses_multiple_overrides(self):
        overrides = forge_run.parse_effort_overrides(["1=low", "2=xhigh"])
        self.assertEqual(overrides, {1: "low", 2: "xhigh"})

    def test_empty_or_none_yields_empty_dict(self):
        self.assertEqual(forge_run.parse_effort_overrides([]), {})
        self.assertEqual(forge_run.parse_effort_overrides(None), {})

    def test_malformed_entry_raises_naming_cause(self):
        with self.assertRaises(RuntimeError) as ctx:
            forge_run.parse_effort_overrides(["nope"])
        self.assertIn("nope", str(ctx.exception))

    def test_ultra_rejected_naming_cause(self):
        with self.assertRaises(RuntimeError) as ctx:
            forge_run.parse_effort_overrides(["1=ultra"])
        msg = str(ctx.exception)
        self.assertIn("ultra", msg)

    def test_unknown_level_rejected_naming_cause(self):
        with self.assertRaises(RuntimeError) as ctx:
            forge_run.parse_effort_overrides(["1=bogus"])
        self.assertIn("bogus", str(ctx.exception))


class EffortOverrideCliTests(unittest.TestCase):
    """CLI --effort N=LEVEL applies only to task N's worker dispatch."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="forge-run-effort-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.fake = write_fake_codex(self.d)
        self.spec = os.path.join(self.d, "spec.md")
        with open(self.spec, "w") as f:
            f.write(MINIMAL_SPEC)
        self.run_dir = os.path.join(self.d, "run")
        self.log = os.path.join(self.d, "fakelog")

    def _plan(self, content, name="plan.md"):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(content)
        return p

    def _run(self, plan_path, extra_args=(), responses=None):
        env = os.environ.copy()
        env["FORGE_FAKE_LOG"] = self.log
        if responses is not None:
            resp_path = os.path.join(self.d, "responses.json")
            with open(resp_path, "w") as f:
                json.dump(responses, f)
            env["FORGE_FAKE_RESPONSES"] = resp_path
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), plan_path,
             "--spec", self.spec, "--run-dir", self.run_dir,
             "--codex-bin", self.fake, *extra_args],
            cwd=self.d, capture_output=True, text=True, env=env,
        )

    def test_override_changes_effort_for_only_that_task(self):
        plan = self._plan(PLAN_DEPS)  # task 1 (trivial) then task 2 depends on it
        res = self._run(plan, extra_args=["--effort", "1=max"])
        self.assertEqual(res.returncode, 0, res.stderr)
        argvs = _log_argvs(self.log)
        t1 = _find_dispatch(argvs, "task-1-worker-last")
        t2 = _find_dispatch(argvs, "task-2-worker-last")
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)
        self.assertIn("model_reasoning_effort=max", t1)
        self.assertIn("model_reasoning_effort=medium", t2)  # trivial default, unaffected

    def test_ultra_effort_rejected_cli_exits_one_naming_cause(self):
        plan = self._plan(PLAN_PASS)
        res = self._run(plan, extra_args=["--effort", "1=ultra"])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("ultra", res.stderr.lower())

    def test_unknown_task_number_rejected_cli_exits_one_naming_cause(self):
        plan = self._plan(PLAN_PASS)  # only task 1 exists
        res = self._run(plan, extra_args=["--effort", "99=max"])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("99", res.stderr)


class TimeoutTests(unittest.TestCase):
    """--timeout SECONDS bounds worker and reviewer codex subprocess calls. A
    worker timeout is a failed iteration (rework/escalation path); a reviewer
    timeout is a contract error (loud exit 1, no receipt)."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="forge-run-timeout-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.fake = write_fake_codex(self.d)
        self.spec = os.path.join(self.d, "spec.md")
        with open(self.spec, "w") as f:
            f.write(MINIMAL_SPEC)
        self.run_dir = os.path.join(self.d, "run")
        self.log = os.path.join(self.d, "fakelog")

    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=self.d, check=True, capture_output=True, text=True
        )

    def _init_repo(self):
        # Ignore harness artifacts so the working tree is clean at run start
        # (the commit-discipline precondition halts on a dirty tree).
        with open(os.path.join(self.d, ".gitignore"), "w") as f:
            f.write("fakelog\nresponses.json\nrun/\n.forge/\n")
        self._git("init")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", "-A")
        self._git("commit", "-m", "base")

    def _plan(self, content, name="plan.md"):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(content)
        return p

    def _run(self, plan_path, extra_args=(), responses=None):
        env = os.environ.copy()
        env["FORGE_FAKE_LOG"] = self.log
        if responses is not None:
            resp_path = os.path.join(self.d, "responses.json")
            with open(resp_path, "w") as f:
                json.dump(responses, f)
            env["FORGE_FAKE_RESPONSES"] = resp_path
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), plan_path,
             "--spec", self.spec, "--run-dir", self.run_dir,
             "--codex-bin", self.fake, *extra_args],
            cwd=self.d, capture_output=True, text=True, env=env,
        )

    def test_worker_timeout_counts_as_failed_iteration_then_escalates(self):
        plan = self._plan(PLAN_PASS)  # trivial, no reviewer, no git repo needed
        res = self._run(
            plan,
            extra_args=["--timeout", "0.2"],
            responses=[{"exit": 0, "msg": "", "sleep": 2}],
        )
        self.assertEqual(res.returncode, 2, res.stderr)
        with open(os.path.join(self.run_dir, "task-1-attempt-2.json")) as f:
            receipt = json.load(f)
        self.assertEqual(receipt["status"], "escalated")
        self.assertTrue(
            any("timed out" in f_ for f_ in receipt["outstanding_findings"])
        )

    def test_reviewer_timeout_exits_one_naming_cause(self):
        plan = self._plan(PLAN_STD)  # standard tier -> reviewer dispatched
        self._init_repo()
        res = self._run(
            plan,
            extra_args=["--timeout", "0.2"],
            responses=[
                {"exit": 0, "msg": ""},              # worker: fast
                {"exit": 0, "msg": _pass_msg(), "sleep": 2},  # reviewer: sleeps past timeout
            ],
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("reviewer", res.stderr.lower())
        self.assertIn("timed out", res.stderr.lower())


if __name__ == "__main__":
    unittest.main()
