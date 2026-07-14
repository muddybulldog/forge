### Task 3: forge-run.py — review, rework cap, halt, resume, final review
- [ ] Done

**Files:**
- Modify: `scripts/forge-run.py` (extends Task 2 module; new functions only, no rework of Task 2 interfaces)
- Test: `tests/test_forge_run.py` (extend)

**Spec:** Task loop, Resume, Halt, Receipts

**Interface:**
- `dispatch_reviewer(task, packet_path, codex_bin, run_dir) -> Verdict` — reviewer via `codex exec` per `REVIEW_MAP`; packet from `review-packet.py`.
- `parse_verdict(last_message: str) -> Verdict` — extraction rule: last parseable JSON object in the message (fenced or bare) matching `{"verdict": "pass"}` or `{"verdict": "findings", "findings": [str, ...]}`; anything else raises (exit 1).
- Rework loop: findings or failed acceptance → re-dispatch worker with findings appended to brief; cap at 2 iterations (initial attempt + 1 rework), then status `escalated`, receipt carries outstanding findings, no further tasks start, exit 2.
- `resume(plan_path, spec_path, run_dir)` — re-invocation with an existing `--run-dir` skips tasks whose latest receipt status is `passed`.
- Final review: after last task passes, one `codex exec` sol/high reviewer against whole-plan diff + spec (packet via `review-packet.py`); findings → exit 2 with `run.json` status `escalated-final-review` (no rework loop at plan level — human gate).

**Tests:** trivial tier skips reviewer dispatch entirely (no reviewer argv recorded); standard/complex dispatch reviewer with mapped model/effort after acceptance passes; `pass` verdict → task passed; findings → rework dispatch carries findings text in prompt; second findings verdict → halt: receipt `escalated` with findings, subsequent task not dispatched, exit 2; unparseable verdict (prose, malformed JSON) → exit 1 naming the cause; verdict embedded in prose/fence extracted correctly; worker crash counts as failed iteration within the cap; resume run skips `passed` tasks (no re-dispatch argv) and resumes at escalated task; ledger annotated `escalated: <one-liner>` on halt; final review dispatched sol/high after all tasks pass; final-review findings → exit 2.

**Acceptance:** `python3 -m pytest tests/test_forge_run.py -q` passes (full file); `python3 -m pytest -q` passes (whole suite).

**Tier:** complex

**Depends on:** Task 2.

````diff
diff --git a/scripts/forge-run.py b/scripts/forge-run.py
index 85885f0..b6119d0 100644
--- a/scripts/forge-run.py
+++ b/scripts/forge-run.py
@@ -1,10 +1,13 @@
 #!/usr/bin/env python3
 """forge-run.py — deterministic whole-plan task runner over ``codex exec``.
 
-Task 2 scope: the sequential task loop (dependency order), worker dispatch via
-one ``codex exec`` process per task, direct acceptance-command execution, JSON
-receipts, a ``run.json`` summary, and plan-checkbox ledger annotations. Review
-dispatch, the rework cap, halt/resume, and final review are added in Task 3.
+Scope: the sequential task loop (dependency order), worker dispatch via one
+``codex exec`` process per task, direct acceptance-command execution, standard/
+complex reviewer dispatch with a machine-parsed JSON verdict, the 2-iteration
+rework cap enforced as a loop counter, mechanical halt on escalation, resume
+(skip tasks already ``passed`` in the run-dir), a plan-level final review against
+the whole-plan diff + spec, JSON receipts, a ``run.json`` summary, and plan-
+checkbox ledger annotations.
 
 Usage:
     forge-run.py <plan.md> --spec <spec.md> [--run-dir DIR] [--codex-bin PATH]
@@ -41,6 +44,14 @@ _eb_spec = importlib.util.spec_from_file_location(
 eb = importlib.util.module_from_spec(_eb_spec)
 _eb_spec.loader.exec_module(eb)
 
+# Reuse review-packet.py for the reviewer packet (task block / spec + git diff) —
+# no duplicated packet assembly or heading grammar.
+_rp_spec = importlib.util.spec_from_file_location(
+    "forge_run_review_packet", os.path.join(SCRIPTS_DIR, "review-packet.py")
+)
+rp = importlib.util.module_from_spec(_rp_spec)
+_rp_spec.loader.exec_module(rp)
+
 
 # Tier -> (model, model_reasoning_effort). Single update point on model churn.
 TIER_MAP = {
@@ -63,6 +74,21 @@ CONTRACT_AGENT = {
 
 _ACC_TAIL_CHARS = 2000
 
+# Rework cap: initial attempt + one rework, enforced as a loop counter
+# (DECISIONS 2026-07-13 — the prose cap proved unenforceable).
+MAX_ATTEMPTS = 2
+
+# Reviewer verdict contract — the machine-readable half the runner parses. Kept
+# in the runner (not agents/*.md) because the JSON shape is a runner concern; the
+# reviewer's judgement rules live in the agents/*.md review paragraph (preamble).
+REVIEW_VERDICT_INSTRUCTION = (
+    "End your message with your verdict as exactly one JSON object and nothing "
+    "after it: {\"verdict\": \"pass\"} when the diff satisfies the spec and the "
+    "task, or {\"verdict\": \"findings\", \"findings\": [\"<file:line - issue>\", "
+    "...]} listing every blocking issue as strings. The runner parses the last "
+    "JSON object in your message; emit nothing parseable as JSON after it."
+)
+
 
 @dataclass
 class Task:
@@ -88,11 +114,18 @@ class WorkerResult:
     argv: list
 
 
+@dataclass
+class Verdict:
+    kind: str  # "pass" | "findings"
+    findings: list = field(default_factory=list)
+
+
 @dataclass
 class TaskOutcome:
     status: str  # "passed" | "escalated"
     attempts: int
     summary: str
+    findings: list = field(default_factory=list)
 
 
 # --- plan parsing (reuses extract-brief heading grammar) --------------------
@@ -326,6 +359,215 @@ def run_acceptance(task, cwd):
     return results
 
 
+# --- reviewer dispatch & verdict --------------------------------------------
+
+
+def _verdict_from_obj(obj):
+    """Map a decoded JSON value to a Verdict if it matches a verdict shape, else
+    None. ``{"verdict": "pass"}`` -> pass; ``{"verdict": "findings", "findings":
+    [str, ...]}`` -> findings; anything else is not a verdict."""
+    if not isinstance(obj, dict) or obj.get("verdict") is None:
+        return None
+    if obj["verdict"] == "pass":
+        return Verdict(kind="pass")
+    if obj["verdict"] == "findings":
+        findings = obj.get("findings")
+        if isinstance(findings, list) and all(isinstance(x, str) for x in findings):
+            return Verdict(kind="findings", findings=list(findings))
+    return None
+
+
+def parse_verdict(last_message):
+    """Extract the reviewer verdict: the last parseable JSON object in the
+    message (fenced or bare) matching a verdict shape. Anything else raises
+    RuntimeError naming the cause — never guessed, never retried silently
+    (DECISIONS 2026-07-11)."""
+    decoder = json.JSONDecoder()
+    found = None
+    i = 0
+    n = len(last_message)
+    while i < n:
+        if last_message[i] != "{":
+            i += 1
+            continue
+        try:
+            obj, end = decoder.raw_decode(last_message, i)
+        except ValueError:
+            i += 1
+            continue
+        verdict = _verdict_from_obj(obj)
+        if verdict is not None:
+            found = verdict
+        i = end  # skip past the parsed object
+    if found is None:
+        raise RuntimeError(
+            "reviewer produced no parseable verdict JSON "
+            '({"verdict": "pass"} or {"verdict": "findings", "findings": [...]}); '
+            "got: " + repr(last_message.strip()[:300])
+        )
+    return found
+
+
+def verdict_to_dict(verdict):
+    if verdict.kind == "pass":
+        return {"verdict": "pass"}
+    return {"verdict": "findings", "findings": list(verdict.findings)}
+
+
+def _dispatch_review_call(model, effort, preamble, packet_path, codex_bin, last_msg_path):
+    """Shared plumbing for per-task and final reviewers: one ``codex exec`` call,
+    prompt = review preamble + verdict instruction + packet; returns the parsed
+    Verdict. Fail-loud on a crashed reviewer or an unparseable verdict — never
+    silently trusts or reuses stale output (Halt spec; ``parse_verdict`` never
+    retries silently). The last-message file is cleared before the call so a prior
+    attempt's verdict can never be re-read, and the reviewer's own exit code is
+    checked (unlike a worker crash, a reviewer crash yields no verdict to judge, so
+    it halts the run rather than consuming a rework iteration)."""
+    with open(packet_path, "r", encoding="utf-8") as f:
+        packet = f.read()
+    prompt = preamble + "\n\n" + REVIEW_VERDICT_INSTRUCTION + "\n\n" + packet
+    argv = [
+        codex_bin,
+        "exec",
+        "-m",
+        model,
+        "-c",
+        "model_reasoning_effort={}".format(effort),
+        "--output-last-message",
+        last_msg_path,
+        prompt,
+    ]
+    if os.path.exists(last_msg_path):
+        os.remove(last_msg_path)  # never re-read a prior attempt's message
+    proc = subprocess.run(argv, capture_output=True, text=True)
+    if proc.returncode != 0:
+        stderr_tail = (proc.stderr or "").strip()[:300]
+        raise RuntimeError(
+            "reviewer process ({} at effort {}) exited {} without a usable "
+            "verdict{}".format(
+                model,
+                effort,
+                proc.returncode,
+                ": " + stderr_tail if stderr_tail else "",
+            )
+        )
+    last_message = ""
+    if os.path.exists(last_msg_path):
+        with open(last_msg_path, "r", encoding="utf-8") as f:
+            last_message = f.read()
+    return parse_verdict(last_message)
+
+
+def dispatch_reviewer(task, packet_path, codex_bin, run_dir):
+    """Per-task reviewer via ``codex exec`` routed by REVIEW_MAP[tier] (standard ->
+    terra/high, complex -> sol/high). Preamble = the tier agent's review paragraph.
+    Returns the parsed Verdict."""
+    model, effort = REVIEW_MAP[task.tier]
+    preamble = contract_preamble(task.tier)
+    last_msg_path = os.path.join(run_dir, "task-{}-review-last.txt".format(task.number))
+    return _dispatch_review_call(
+        model, effort, preamble, packet_path, codex_bin, last_msg_path
+    )
+
+
+def dispatch_final_review(packet_path, codex_bin, run_dir):
+    """Whole-plan final review: one sol/high ``codex exec`` call (REVIEW_MAP[
+    'complex']) with the forge-deep final-integration-review preamble against the
+    whole-plan diff + spec. Returns the parsed Verdict."""
+    model, effort = REVIEW_MAP["complex"]
+    preamble = contract_preamble("complex")
+    last_msg_path = os.path.join(run_dir, "final-review-last.txt")
+    return _dispatch_review_call(
+        model, effort, preamble, packet_path, codex_bin, last_msg_path
+    )
+
+
+# --- git helpers (diff base for review packets) -----------------------------
+
+
+def _git_head(cwd):
+    """HEAD SHA of the repo at ``cwd``, or None when ``cwd`` is not a git repo
+    (git unavailable / no commits). Reviews require a repo; callers that must have
+    one raise loudly, and the plan-level final review is skipped without one."""
+    try:
+        proc = subprocess.run(
+            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True
+        )
+    except OSError:
+        return None
+    if proc.returncode != 0:
+        return None
+    return proc.stdout.strip() or None
+
+
+def _snapshot_worktree(cwd):
+    """Per-task review base: a tree-ish capturing the working tree *before* the
+    task runs, so ``git diff <snapshot>`` afterwards shows only this task's own
+    changes. Nothing commits between tasks (HEAD never advances), so a HEAD base
+    would fold every prior task's still-uncommitted change into this task's packet;
+    ``git stash create`` snapshots the current tracked working tree as a dangling
+    commit without touching the working tree, index, or refs. Returns that commit
+    SHA, or HEAD when the tree is clean (``stash create`` emits nothing), or None
+    when ``cwd`` is not a git repo (callers that reach the reviewer raise loudly).
+    Untracked files are invisible to this base, consistent with ``git diff``
+    (DEFERRALS 2026-07-11)."""
+    head = _git_head(cwd)
+    if head is None:
+        return None
+    try:
+        proc = subprocess.run(
+            ["git", "stash", "create"], cwd=cwd, capture_output=True, text=True
+        )
+    except OSError:
+        return None
+    if proc.returncode != 0:
+        return None
+    return proc.stdout.strip() or head
+
+
+def _git_diff(cwd, base):
+    """``git diff <base>`` in ``cwd``. Raises RuntimeError naming the cause on a
+    git failure (a packet-generation error — halt per the Halt spec)."""
+    proc = subprocess.run(
+        ["git", "diff", base], cwd=cwd, capture_output=True, text=True
+    )
+    if proc.returncode != 0:
+        raise RuntimeError(
+            "git diff {} failed in {}: {}".format(base, cwd, proc.stderr.strip())
+        )
+    return proc.stdout
+
+
+def _packet_for(task, plan_path, run_dir, base, cwd):
+    """Per-task review packet via review-packet.py: the task block + ``git diff
+    <base>``. Missing task block raises (fail-loud)."""
+    with open(plan_path, "r", encoding="utf-8") as f:
+        plan_text = f.read()
+    block = rp.extract_task_block(plan_text, task.number)
+    if block is None:
+        raise RuntimeError(
+            "review packet: " + rp.diagnose_missing_task(plan_text, task.number, plan_path)
+        )
+    diff = _git_diff(cwd, base)
+    packet = rp.build_packet(block, base, diff)
+    path = os.path.join(run_dir, "task-{}-review.md".format(task.number))
+    with open(path, "w", encoding="utf-8") as f:
+        f.write(packet)
+    return path
+
+
+def _final_packet(spec_path, base, diff, run_dir):
+    """Whole-plan final-review packet: the spec + the whole-plan ``git diff
+    <base>``, assembled by review-packet.py's fence-safe builder."""
+    with open(spec_path, "r", encoding="utf-8") as f:
+        spec_text = f.read()
+    packet = rp.build_packet(spec_text, base, diff)
+    path = os.path.join(run_dir, "final-review.md")
+    with open(path, "w", encoding="utf-8") as f:
+        f.write(packet)
+    return path
+
+
 # --- receipts & ledger ------------------------------------------------------
 
 
@@ -351,6 +593,59 @@ def write_run_json(run_dir, plan_path, spec_path, status, task_summaries):
     return path
 
 
+def write_final_review_receipt(run_dir, verdict):
+    """Persist the plan-level final-review verdict alongside the task receipts."""
+    os.makedirs(run_dir, exist_ok=True)
+    path = os.path.join(run_dir, "final-review.json")
+    with open(path, "w", encoding="utf-8") as f:
+        json.dump(verdict_to_dict(verdict), f, indent=2)
+    return path
+
+
+_ATTEMPT_RE = re.compile(r"^task-(\d+)-attempt-(\d+)\.json$")
+
+
+def _read_latest_receipt(run_dir, task_number):
+    """The highest-attempt receipt dict for a task in ``run_dir``, or None. The
+    receipts are the only resume state — no separate store (Resume spec)."""
+    if not os.path.isdir(run_dir):
+        return None
+    best_attempt = -1
+    best_name = None
+    for name in os.listdir(run_dir):
+        m = _ATTEMPT_RE.match(name)
+        if m and int(m.group(1)) == task_number and int(m.group(2)) > best_attempt:
+            best_attempt = int(m.group(2))
+            best_name = name
+    if best_name is None:
+        return None
+    with open(os.path.join(run_dir, best_name), "r", encoding="utf-8") as f:
+        return json.load(f)
+
+
+def latest_status(run_dir, task_number):
+    """Latest receipt status for a task (``passed`` | ``rework`` | ``escalated``),
+    or None when the task has no receipt yet."""
+    receipt = _read_latest_receipt(run_dir, task_number)
+    return receipt.get("status") if receipt else None
+
+
+def _clear_task_receipts(run_dir, task_number):
+    """Remove a task's prior receipts, plus its stale reviewer last-message file,
+    so a re-run writes a clean attempt sequence (attempt-1, attempt-2) and can
+    never re-read a prior run's verdict — the reviewer call also clears the file,
+    this closes the gap on resume when the reviewer is never reached."""
+    if not os.path.isdir(run_dir):
+        return
+    for name in os.listdir(run_dir):
+        m = _ATTEMPT_RE.match(name)
+        if m and int(m.group(1)) == task_number:
+            os.remove(os.path.join(run_dir, name))
+    stale_review = os.path.join(run_dir, "task-{}-review-last.txt".format(task_number))
+    if os.path.exists(stale_review):
+        os.remove(stale_review)
+
+
 def ensure_forge_gitignore(cwd):
     """Self-ignoring ``.forge/.gitignore`` containing ``*`` — no target-repo
     setup required (Receipts spec, 2026-07-13 amendment). Idempotent: only
@@ -389,9 +684,18 @@ def annotate_ledger(plan_path, task, status_line):
 # --- per-task execution & plan loop -----------------------------------------
 
 
-def _brief_for(task, plan_path, spec_path, run_dir):
+def _brief_for(task, plan_path, spec_path, run_dir, attempt, findings):
+    """Write the worker brief for one attempt and return its path + SHA-256. On a
+    rework attempt (findings non-empty) the outstanding findings are appended so
+    the re-dispatched worker sees exactly what to fix; the SHA covers that text."""
     brief = eb.build_brief(plan_path, task.number, spec_path)
-    brief_path = os.path.join(run_dir, "task-{}-brief.md".format(task.number))
+    if findings:
+        lines = ["", "", "## Rework — address these findings before resubmitting", ""]
+        lines.extend("- {}".format(f) for f in findings)
+        brief = brief.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
+    brief_path = os.path.join(
+        run_dir, "task-{}-attempt-{}-brief.md".format(task.number, attempt)
+    )
     with open(brief_path, "w", encoding="utf-8") as f:
         f.write(brief)
     sha = hashlib.sha256(brief.encode("utf-8")).hexdigest()
@@ -399,62 +703,138 @@ def _brief_for(task, plan_path, spec_path, run_dir):
 
 
 def execute_task(task, plan_path, spec_path, run_dir, codex_bin, cwd):
-    """Task 2: a single worker attempt plus acceptance. Passes when the worker
-    exits 0 and every acceptance command exits 0; otherwise escalates (the rework
-    loop and review arrive in Task 3)."""
-    attempt = 1
+    """Run one task through the rework loop: worker -> acceptance -> (standard/
+    complex) reviewer, capped at MAX_ATTEMPTS. A worker crash, a failed acceptance
+    command, or a findings verdict is a failed iteration; the next iteration
+    re-dispatches the worker with the outstanding findings appended to the brief.
+    Hitting the cap yields status ``escalated`` with the outstanding findings on
+    the final receipt."""
     model, effort = TIER_MAP[task.tier]
-    brief_path, brief_sha = _brief_for(task, plan_path, spec_path, run_dir)
+    # Snapshot the working tree before this task runs — the per-task review base.
+    # HEAD does not advance between tasks (nothing commits), so a HEAD base would
+    # include prior tasks' uncommitted changes; the snapshot isolates `git diff`
+    # to this task's own changes. Taken once (trivial tiers need no reviewer).
+    review_base = _snapshot_worktree(cwd) if task.tier != "trivial" else None
+    findings_carry = []
+
+    attempt = 0
+    while True:
+        attempt += 1
+        brief_path, brief_sha = _brief_for(
+            task, plan_path, spec_path, run_dir, attempt, findings_carry
+        )
+        worker = dispatch_worker(task, brief_path, codex_bin, run_dir)
+        acceptance = run_acceptance(task, cwd)
 
-    worker = dispatch_worker(task, brief_path, codex_bin, run_dir)
-    acceptance = run_acceptance(task, cwd)
+        worker_ok = worker.exit_code == 0
+        acc_ok = all(r.exit_code == 0 for r in acceptance)
 
-    worker_ok = worker.exit_code == 0
-    acc_ok = all(r.exit_code == 0 for r in acceptance)
-    passed = worker_ok and acc_ok
+        review_verdict = None
+        iteration_findings = []
+        failure_summary = None
 
-    if passed:
-        status = "passed"
-        summary = ""
-    else:
-        status = "escalated"
         if not worker_ok:
-            summary = "worker exited {}".format(worker.exit_code)
-        else:
+            failure_summary = "worker exited {}".format(worker.exit_code)
+            iteration_findings = [
+                "Prior worker attempt exited {} with no usable result — "
+                "reattempt the task.".format(worker.exit_code)
+            ]
+        elif not acc_ok:
             failed = next(r for r in acceptance if r.exit_code != 0)
-            summary = "acceptance failed: {}".format(failed.command)
-
-    receipt = {
-        "task_number": task.number,
-        "title": task.title,
-        "tier": task.tier,
-        "model": model,
-        "effort": effort,
-        "brief_path": os.path.abspath(brief_path),
-        "brief_sha256": brief_sha,
-        "worker_exit_code": worker.exit_code,
-        "acceptance_results": [asdict(r) for r in acceptance],
-        "review_verdict": None,
-        "attempt": attempt,
-        "status": status,
-    }
-    write_receipt(run_dir, task, attempt, receipt)
-    return TaskOutcome(status=status, attempts=attempt, summary=summary)
+            failure_summary = "acceptance failed: {}".format(failed.command)
+            iteration_findings = [
+                "Acceptance command `{}` failed (exit {}). Output tail:\n{}".format(
+                    failed.command, failed.exit_code, failed.output_tail
+                )
+            ]
+        elif task.tier != "trivial":
+            # Trivial tier: acceptance is the whole verification. Standard/complex:
+            # a reviewer judges the diff against the spec.
+            if review_base is None:
+                raise RuntimeError(
+                    "cannot generate review packet for task {}: cwd is not a git "
+                    "repository".format(task.number)
+                )
+            packet_path = _packet_for(task, plan_path, run_dir, review_base, cwd)
+            verdict = dispatch_reviewer(task, packet_path, codex_bin, run_dir)
+            review_verdict = verdict_to_dict(verdict)
+            if verdict.kind == "findings":
+                iteration_findings = list(verdict.findings)
+                failure_summary = "review findings: {}".format(
+                    "; ".join(verdict.findings) if verdict.findings else "(unspecified)"
+                )
+
+        passed = failure_summary is None
+        if passed:
+            status = "passed"
+        elif attempt >= MAX_ATTEMPTS:
+            status = "escalated"
+        else:
+            status = "rework"
+
+        receipt = {
+            "task_number": task.number,
+            "title": task.title,
+            "tier": task.tier,
+            "model": model,
+            "effort": effort,
+            "brief_path": os.path.abspath(brief_path),
+            "brief_sha256": brief_sha,
+            "worker_exit_code": worker.exit_code,
+            "acceptance_results": [asdict(r) for r in acceptance],
+            "review_verdict": review_verdict,
+            "attempt": attempt,
+            "status": status,
+            "outstanding_findings": iteration_findings if status == "escalated" else [],
+        }
+        write_receipt(run_dir, task, attempt, receipt)
+
+        if passed:
+            return TaskOutcome(status="passed", attempts=attempt, summary="")
+        if status == "escalated":
+            return TaskOutcome(
+                status="escalated",
+                attempts=attempt,
+                summary=failure_summary,
+                findings=iteration_findings,
+            )
+        findings_carry = iteration_findings  # rework: carry into the next brief
 
 
 def run_plan(plan_path, spec_path, run_dir, codex_bin, cwd):
+    """Sequential whole-plan loop. Tasks already ``passed`` in this run-dir (a
+    resume) are skipped; the rest run through execute_task in dependency order.
+    Halts on the first escalation. After every task passes, one plan-level final
+    review runs against the whole-plan diff + spec (git repo required)."""
     os.makedirs(run_dir, exist_ok=True)
     ensure_forge_gitignore(cwd)
     tasks = parse_plan_tasks(plan_path)
     order = order_tasks(tasks)
+    run_base = _git_head(cwd)  # whole-plan diff base for the final review
 
     task_summaries = []
     overall = "passed"
+    escalated = False
 
     # order_tasks yields dependency order (each dependency before its dependents)
     # and the loop breaks on the first escalation, so a dependent is never reached
     # unless every dependency already passed — no separate depends-on guard needed.
     for task in order:
+        if latest_status(run_dir, task.number) == "passed":
+            # Resume: a prior invocation already completed this task.
+            prior = _read_latest_receipt(run_dir, task.number) or {}
+            task_summaries.append(
+                {
+                    "number": task.number,
+                    "title": task.title,
+                    "tier": task.tier,
+                    "status": "passed",
+                    "attempts": prior.get("attempt", 1),
+                }
+            )
+            continue
+
+        _clear_task_receipts(run_dir, task.number)
         outcome = execute_task(task, plan_path, spec_path, run_dir, codex_bin, cwd)
         task_summaries.append(
             {
@@ -472,12 +852,35 @@ def run_plan(plan_path, spec_path, run_dir, codex_bin, cwd):
         else:
             annotate_ledger(plan_path, task, "escalated: {}".format(outcome.summary))
             overall = "escalated"
+            escalated = True
             break
 
+    if not escalated and run_base is not None:
+        # Final broad review: whole-plan diff + spec, one sol/high reviewer. No
+        # rework loop at plan level — findings are a human gate. Skipped when the
+        # diff is empty (nothing to review) or cwd is not a git repo (no baseline).
+        diff = _git_diff(cwd, run_base)
+        if diff.strip():
+            packet_path = _final_packet(spec_path, run_base, diff, run_dir)
+            verdict = dispatch_final_review(packet_path, codex_bin, run_dir)
+            write_final_review_receipt(run_dir, verdict)
+            if verdict.kind == "findings":
+                overall = "escalated-final-review"
+
     write_run_json(run_dir, plan_path, spec_path, overall, task_summaries)
     return 0 if overall == "passed" else 2
 
 
+def resume(plan_path, spec_path, run_dir):
+    """Re-invoke over an existing ``run_dir``: tasks whose latest receipt status is
+    ``passed`` are skipped (not re-dispatched); execution resumes at the first
+    incomplete/escalated task. Receipts + plan checkboxes are the only resume
+    state (Resume spec). A thin, documented alias over ``run_plan`` — whose
+    skip-passed logic already makes every invocation resumable — using the
+    production defaults: ``codex`` on PATH and the current working directory."""
+    return run_plan(plan_path, spec_path, run_dir, "codex", os.getcwd())
+
+
 def _default_run_dir():
     stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
     return os.path.join(".forge", "runs", stamp)
diff --git a/tests/test_forge_run.py b/tests/test_forge_run.py
index 2ce9c47..6373090 100644
--- a/tests/test_forge_run.py
+++ b/tests/test_forge_run.py
@@ -164,6 +164,85 @@ PLAN_DUP = """# Fixture Plan
 
 MINIMAL_SPEC = "# Spec\n\nNothing referenced.\n"
 
+# A single standard-tier task: acceptance passes, so a reviewer is dispatched.
+PLAN_STD = """# Fixture Plan
+
+**Goal:** Do the thing.
+
+### Task 1: Standard task
+- [ ] Done
+
+**Acceptance:** `true`
+
+**Tier:** standard
+
+**Depends on:** nothing
+"""
+
+# Standard task 1 (reviewed) followed by a trivial task 2 that depends on it —
+# used to prove a halt at task 1 never dispatches task 2.
+PLAN_STD_THEN_TRIVIAL = """# Fixture Plan
+
+**Goal:** Do the thing.
+
+### Task 1: Standard task
+- [ ] Done
+
+**Acceptance:** `true`
+
+**Tier:** standard
+
+**Depends on:** nothing
+
+### Task 2: Trivial follow-up
+- [ ] Done
+
+**Acceptance:** `true`
+
+**Tier:** trivial
+
+**Depends on:** Task 1
+"""
+
+# Two trivial tasks, task 2 depends on task 1 — used by the resume test where the
+# escalation is driven by a worker crash (no reviewer, so no git repo required).
+PLAN_TWO_TRIVIAL = PLAN_DEPS
+
+# Two standard (reviewed) tasks, each appending a distinct marker to its OWN
+# tracked file via its acceptance command. Proves per-task review packets are
+# isolated to that task's own diff even though HEAD never advances between tasks
+# (nothing commits) — task 2's packet must carry only task 2's change.
+PLAN_TWO_STD = """# Fixture Plan
+
+**Goal:** Do the thing.
+
+### Task 1: First standard
+- [ ] Done
+
+**Acceptance:** `echo TASK1MARK >> f1.txt`
+
+**Tier:** standard
+
+**Depends on:** nothing
+
+### Task 2: Second standard
+- [ ] Done
+
+**Acceptance:** `echo TASK2MARK >> f2.txt`
+
+**Tier:** standard
+
+**Depends on:** Task 1
+"""
+
+
+def _pass_msg():
+    return '{"verdict": "pass"}'
+
+
+def _findings_msg(*items):
+    return json.dumps({"verdict": "findings", "findings": list(items)})
+
 
 class ParsePlanTasksTests(unittest.TestCase):
     def _write(self, content):
@@ -411,9 +490,14 @@ class LoopSubprocessTests(unittest.TestCase):
         self.assertEqual(res.returncode, 2, res.stderr)
         with open(self.log) as f:
             log_lines = [ln for ln in f.read().splitlines() if ln.strip()]
-        # Exactly one worker dispatch — the failed dependency, never the dependent.
-        self.assertEqual(len(log_lines), 1, log_lines)
-        self.assertIn("task-1-worker-last", log_lines[0])
+        # Every worker dispatch is the failed dependency (task 1), never the
+        # dependent (task 2). A crashing worker consumes the rework cap, so task 1
+        # is dispatched more than once (initial + one rework) — the invariant under
+        # test is that task 2 is never reached, not the exact attempt count.
+        self.assertTrue(log_lines)
+        self.assertTrue(
+            all("task-1-worker-last" in ln for ln in log_lines), log_lines
+        )
         self.assertNotIn("task-2-worker-last", "\n".join(log_lines))
         # Task 2's worker last-message file is never created.
         self.assertFalse(
@@ -484,5 +568,460 @@ class LoopSubprocessTests(unittest.TestCase):
         self.assertIn("contract source", res.stderr.lower())
 
 
+class ParseVerdictTests(unittest.TestCase):
+    """parse_verdict: last parseable JSON object matching the two verdict shapes
+    (fenced or bare); anything else raises naming the cause."""
+
+    def test_bare_pass(self):
+        v = forge_run.parse_verdict('{"verdict": "pass"}')
+        self.assertEqual(v.kind, "pass")
+
+    def test_findings_extracted_from_prose_and_fence(self):
+        msg = (
+            "Here is my review of the diff.\n\n"
+            "```json\n"
+            '{"verdict": "findings", "findings": ["a.py:3 - missing guard"]}\n'
+            "```\n\nThat is all.\n"
+        )
+        v = forge_run.parse_verdict(msg)
+        self.assertEqual(v.kind, "findings")
+        self.assertEqual(v.findings, ["a.py:3 - missing guard"])
+
+    def test_unparseable_prose_raises_naming_cause(self):
+        with self.assertRaises(RuntimeError) as ctx:
+            forge_run.parse_verdict("Looks good to me, ship it.")
+        self.assertIn("verdict", str(ctx.exception).lower())
+
+    def test_malformed_json_raises(self):
+        with self.assertRaises(RuntimeError):
+            forge_run.parse_verdict('{"verdict": ')
+
+    def test_last_matching_object_wins(self):
+        msg = (
+            '{"verdict": "pass"}\n'
+            "on reflection...\n"
+            '{"verdict": "findings", "findings": ["x"]}'
+        )
+        v = forge_run.parse_verdict(msg)
+        self.assertEqual(v.kind, "findings")
+        self.assertEqual(v.findings, ["x"])
+
+
+def _log_argvs(log_path):
+    if not os.path.exists(log_path):
+        return []
+    with open(log_path) as f:
+        return [json.loads(ln) for ln in f if ln.strip()]
+
+
+def _find_dispatch(argvs, marker):
+    """Return the first argv (list) whose --output-last-message path contains
+    ``marker`` — distinguishes worker vs reviewer vs final-review calls."""
+    for a in argvs:
+        if "--output-last-message" in a:
+            path = a[a.index("--output-last-message") + 1]
+            if marker in path:
+                return a
+    return None
+
+
+class DispatchReviewerUnitTests(unittest.TestCase):
+    """dispatch_reviewer routes model/effort by REVIEW_MAP[tier] and returns the
+    parsed Verdict — exercised directly against the fake codex (no plan loop)."""
+
+    def setUp(self):
+        self.d = tempfile.mkdtemp(prefix="forge-run-rev-unit-")
+        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
+        self.fake = write_fake_codex(self.d)
+        self.packet = os.path.join(self.d, "packet.md")
+        with open(self.packet, "w") as f:
+            f.write("### Task 1: X\n\n```diff\n```\n")
+        self.log = os.path.join(self.d, "log")
+        self.resp = os.path.join(self.d, "resp.json")
+        with open(self.resp, "w") as f:
+            json.dump([{"exit": 0, "msg": '{"verdict": "pass"}'}], f)
+        self._set_env("FORGE_FAKE_LOG", self.log)
+        self._set_env("FORGE_FAKE_RESPONSES", self.resp)
+
+    def _set_env(self, key, value):
+        old = os.environ.get(key)
+        os.environ[key] = value
+        self.addCleanup(
+            lambda: os.environ.__setitem__(key, old)
+            if old is not None
+            else os.environ.pop(key, None)
+        )
+
+    def _argv_for(self, marker):
+        with open(self.log) as f:
+            for ln in f:
+                if not ln.strip():
+                    continue
+                a = json.loads(ln)
+                if "--output-last-message" in a:
+                    path = a[a.index("--output-last-message") + 1]
+                    if marker in path:
+                        return a
+        return None
+
+    def test_standard_reviewer_maps_terra_high(self):
+        run_dir = os.path.join(self.d, "run-s")
+        os.makedirs(run_dir)
+        task = forge_run.Task(number=1, title="t", tier="standard")
+        verdict = forge_run.dispatch_reviewer(task, self.packet, self.fake, run_dir)
+        self.assertEqual(verdict.kind, "pass")
+        argv = self._argv_for("task-1-review-last")
+        self.assertIsNotNone(argv)
+        self.assertIn("gpt-5.6-terra", argv)
+        self.assertIn("model_reasoning_effort=high", argv)
+        self.assertNotIn("ultra", " ".join(argv))
+
+    def test_complex_reviewer_maps_sol_high(self):
+        run_dir = os.path.join(self.d, "run-c")
+        os.makedirs(run_dir)
+        task = forge_run.Task(number=2, title="t", tier="complex")
+        verdict = forge_run.dispatch_reviewer(task, self.packet, self.fake, run_dir)
+        self.assertEqual(verdict.kind, "pass")
+        argv = self._argv_for("task-2-review-last")
+        self.assertIsNotNone(argv)
+        self.assertIn("gpt-5.6-sol", argv)
+        self.assertIn("model_reasoning_effort=high", argv)
+        self.assertNotIn("ultra", " ".join(argv))
+
+
+class ReviewLoopTests(unittest.TestCase):
+    """Standard/complex review + rework + halt + final review. These need a git
+    repo because the review packet is a ``git diff`` against the run baseline."""
+
+    def setUp(self):
+        self.d = tempfile.mkdtemp(prefix="forge-run-review-")
+        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
+        self.fake = write_fake_codex(self.d)
+        self.spec = os.path.join(self.d, "spec.md")
+        with open(self.spec, "w") as f:
+            f.write(MINIMAL_SPEC)
+        self.run_dir = os.path.join(self.d, "run")
+        self.log = os.path.join(self.d, "fakelog")
+
+    def _git(self, *args):
+        subprocess.run(
+            ["git", *args], cwd=self.d, check=True, capture_output=True, text=True
+        )
+
+    def _init_repo(self):
+        self._git("init")
+        self._git("config", "user.email", "t@example.com")
+        self._git("config", "user.name", "Test")
+        self._git("add", "-A")
+        self._git("commit", "-m", "base")
+
+    def _plan(self, content, name="plan.md"):
+        p = os.path.join(self.d, name)
+        with open(p, "w") as f:
+            f.write(content)
+        return p
+
+    def _run(self, plan_path, responses=None):
+        if os.path.exists(self.log):
+            os.remove(self.log)
+        env = os.environ.copy()
+        env["FORGE_FAKE_LOG"] = self.log
+        if responses is not None:
+            resp_path = os.path.join(self.d, "responses.json")
+            with open(resp_path, "w") as f:
+                json.dump(responses, f)
+            env["FORGE_FAKE_RESPONSES"] = resp_path
+        return subprocess.run(
+            [sys.executable, str(SCRIPT_PATH), plan_path,
+             "--spec", self.spec, "--run-dir", self.run_dir,
+             "--codex-bin", self.fake],
+            cwd=self.d, capture_output=True, text=True, env=env,
+        )
+
+    def test_standard_dispatches_reviewer_with_mapped_model_and_passes(self):
+        plan = self._plan(PLAN_STD)
+        self._init_repo()
+        res = self._run(plan, responses=[
+            {"exit": 0, "msg": ""},           # worker
+            {"exit": 0, "msg": _pass_msg()},  # reviewer (clamps for final review)
+        ])
+        self.assertEqual(res.returncode, 0, res.stderr)
+        argvs = _log_argvs(self.log)
+        rev = _find_dispatch(argvs, "task-1-review-last")
+        self.assertIsNotNone(rev, argvs)
+        self.assertIn("gpt-5.6-terra", rev)
+        self.assertIn("model_reasoning_effort=high", rev)
+        with open(os.path.join(self.run_dir, "task-1-attempt-1.json")) as f:
+            receipt = json.load(f)
+        self.assertEqual(receipt["status"], "passed")
+        self.assertEqual(receipt["review_verdict"], {"verdict": "pass"})
+
+    def test_findings_then_rework_carries_findings_text_in_worker_prompt(self):
+        plan = self._plan(PLAN_STD)
+        self._init_repo()
+        res = self._run(plan, responses=[
+            {"exit": 0, "msg": ""},                                  # worker a1
+            {"exit": 0, "msg": _findings_msg("GUARDXYZ needed at a.py:3")},  # review a1
+            {"exit": 0, "msg": ""},                                  # worker a2 (rework)
+            {"exit": 0, "msg": _pass_msg()},                         # review a2
+        ])
+        self.assertEqual(res.returncode, 0, res.stderr)
+        # The rework worker's brief carries the finding text; the fake logs the
+        # full argv (prompt is the last arg), so the marker must appear there.
+        with open(self.log) as f:
+            self.assertIn("GUARDXYZ", f.read())
+
+    def test_second_findings_verdict_halts_escalated_and_stops_next_task(self):
+        plan = self._plan(PLAN_STD_THEN_TRIVIAL)
+        self._init_repo()
+        res = self._run(plan, responses=[
+            {"exit": 0, "msg": ""},                              # t1 worker a1
+            {"exit": 0, "msg": _findings_msg("a.py:1 - issue")}, # t1 review a1
+            {"exit": 0, "msg": ""},                              # t1 worker a2
+            {"exit": 0, "msg": _findings_msg("a.py:1 - still")}, # t1 review a2
+        ])
+        self.assertEqual(res.returncode, 2, res.stderr)
+        with open(os.path.join(self.run_dir, "task-1-attempt-2.json")) as f:
+            receipt = json.load(f)
+        self.assertEqual(receipt["status"], "escalated")
+        self.assertTrue(receipt["outstanding_findings"])
+        # Task 2 is never dispatched.
+        self.assertFalse(
+            os.path.exists(os.path.join(self.run_dir, "task-2-worker-last.txt"))
+        )
+        # Ledger annotated escalated on task 1.
+        with open(plan) as f:
+            content = f.read()
+        self.assertIn("escalated:", content)
+
+    def test_unparseable_reviewer_verdict_exits_one_naming_cause(self):
+        plan = self._plan(PLAN_STD)
+        self._init_repo()
+        res = self._run(plan, responses=[
+            {"exit": 0, "msg": ""},                       # worker
+            {"exit": 0, "msg": "looks good, no JSON"},    # reviewer: unparseable
+        ])
+        self.assertEqual(res.returncode, 1, res.stderr)
+        self.assertIn("verdict", res.stderr.lower())
+
+    def test_final_review_dispatched_sol_high_after_all_pass(self):
+        plan = self._plan(PLAN_PASS)  # trivial task: no per-task reviewer
+        self._init_repo()
+        res = self._run(plan, responses=[
+            {"exit": 0, "msg": ""},           # trivial worker
+            {"exit": 0, "msg": _pass_msg()},  # final review
+        ])
+        self.assertEqual(res.returncode, 0, res.stderr)
+        argvs = _log_argvs(self.log)
+        fr = _find_dispatch(argvs, "final-review-last")
+        self.assertIsNotNone(fr, argvs)
+        self.assertIn("gpt-5.6-sol", fr)
+        self.assertIn("model_reasoning_effort=high", fr)
+        # A trivial task never dispatches a per-task reviewer.
+        self.assertIsNone(_find_dispatch(argvs, "task-1-review-last"))
+
+    def test_final_review_findings_exit_two_status_escalated_final_review(self):
+        plan = self._plan(PLAN_PASS)
+        self._init_repo()
+        res = self._run(plan, responses=[
+            {"exit": 0, "msg": ""},                                   # worker
+            {"exit": 0, "msg": _findings_msg("spec drift at x")},     # final review
+        ])
+        self.assertEqual(res.returncode, 2, res.stderr)
+        with open(os.path.join(self.run_dir, "run.json")) as f:
+            summary = json.load(f)
+        self.assertEqual(summary["status"], "escalated-final-review")
+
+    def test_second_reviewed_task_packet_isolated_to_its_own_diff(self):
+        # Two sequential standard tasks, each mutating its OWN tracked file. HEAD
+        # never advances between tasks (nothing commits), so a HEAD-based per-task
+        # base would fold task 1's still-uncommitted change into task 2's packet.
+        # The runner must snapshot the working tree per task so task 2's packet
+        # carries only task 2's change.
+        plan = self._plan(PLAN_TWO_STD)
+        for name in ("f1.txt", "f2.txt"):
+            with open(os.path.join(self.d, name), "w") as f:
+                f.write("base\n")
+        self._init_repo()
+        res = self._run(plan, responses=[
+            {"exit": 0, "msg": ""},           # t1 worker
+            {"exit": 0, "msg": _pass_msg()},  # t1 review
+            {"exit": 0, "msg": ""},           # t2 worker
+            {"exit": 0, "msg": _pass_msg()},  # t2 review
+            {"exit": 0, "msg": _pass_msg()},  # final review
+        ])
+        self.assertEqual(res.returncode, 0, res.stderr)
+        with open(os.path.join(self.run_dir, "task-1-review.md")) as f:
+            p1 = f.read()
+        with open(os.path.join(self.run_dir, "task-2-review.md")) as f:
+            p2 = f.read()
+        self.assertIn("TASK1MARK", p1)
+        self.assertIn("TASK2MARK", p2)
+        # The task-2 packet must not carry task 1's still-uncommitted change.
+        self.assertNotIn("TASK1MARK", p2)
+
+    def test_reviewer_process_crash_exits_one_naming_cause(self):
+        # The reviewer subprocess exits non-zero but still writes a parseable
+        # verdict. A runner that discards the reviewer's exit code would trust the
+        # message and pass; the runner must instead fail loud on a crashed
+        # reviewer rather than silently trust (or reuse) its output.
+        plan = self._plan(PLAN_STD)
+        self._init_repo()
+        res = self._run(plan, responses=[
+            {"exit": 0, "msg": ""},            # worker
+            {"exit": 3, "msg": _pass_msg()},   # reviewer crashes (exit 3)
+        ])
+        self.assertEqual(res.returncode, 1, res.stderr)
+        self.assertIn("reviewer", res.stderr.lower())
+
+
+class ReviewNonGitTests(unittest.TestCase):
+    """Review-path behaviors that need no git repo: trivial tier skips the
+    reviewer entirely, and a worker crash consumes rework iterations without ever
+    reaching the reviewer."""
+
+    def setUp(self):
+        self.d = tempfile.mkdtemp(prefix="forge-run-review-nogit-")
+        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
+        self.fake = write_fake_codex(self.d)
+        self.spec = os.path.join(self.d, "spec.md")
+        with open(self.spec, "w") as f:
+            f.write(MINIMAL_SPEC)
+        self.run_dir = os.path.join(self.d, "run")
+        self.log = os.path.join(self.d, "fakelog")
+
+    def _plan(self, content, name="plan.md"):
+        p = os.path.join(self.d, name)
+        with open(p, "w") as f:
+            f.write(content)
+        return p
+
+    def _run(self, plan_path, responses=None):
+        if os.path.exists(self.log):
+            os.remove(self.log)
+        env = os.environ.copy()
+        env["FORGE_FAKE_LOG"] = self.log
+        if responses is not None:
+            resp_path = os.path.join(self.d, "responses.json")
+            with open(resp_path, "w") as f:
+                json.dump(responses, f)
+            env["FORGE_FAKE_RESPONSES"] = resp_path
+        return subprocess.run(
+            [sys.executable, str(SCRIPT_PATH), plan_path,
+             "--spec", self.spec, "--run-dir", self.run_dir,
+             "--codex-bin", self.fake],
+            cwd=self.d, capture_output=True, text=True, env=env,
+        )
+
+    def test_trivial_tier_skips_reviewer_dispatch_entirely(self):
+        # Non-git cwd: no final review either, so the log must show no reviewer.
+        plan = self._plan(PLAN_PASS)
+        res = self._run(plan)
+        self.assertEqual(res.returncode, 0, res.stderr)
+        argvs = _log_argvs(self.log)
+        self.assertIsNone(_find_dispatch(argvs, "review-last"), argvs)
+
+    def test_worker_crash_counts_as_failed_iteration_within_cap(self):
+        # Standard tier, but the worker crashes every attempt so the reviewer is
+        # never reached; two crashes hit the rework cap -> escalated, exit 2.
+        plan = self._plan(PLAN_STD)
+        res = self._run(plan, responses=[{"exit": 1, "msg": ""}])
+        self.assertEqual(res.returncode, 2, res.stderr)
+        argvs = _log_argvs(self.log)
+        self.assertIsNone(_find_dispatch(argvs, "task-1-review-last"), argvs)
+        with open(os.path.join(self.run_dir, "task-1-attempt-2.json")) as f:
+            receipt = json.load(f)
+        self.assertEqual(receipt["status"], "escalated")
+        self.assertEqual(receipt["worker_exit_code"], 1)
+
+
+class ResumeTests(unittest.TestCase):
+    """Re-invocation with an existing --run-dir skips tasks whose latest receipt
+    status is ``passed`` and resumes at the incomplete/escalated one. Trivial
+    tasks + worker-crash escalation keep this off the git path."""
+
+    def setUp(self):
+        self.d = tempfile.mkdtemp(prefix="forge-run-resume-")
+        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
+        self.fake = write_fake_codex(self.d)
+        self.spec = os.path.join(self.d, "spec.md")
+        with open(self.spec, "w") as f:
+            f.write(MINIMAL_SPEC)
+        self.run_dir = os.path.join(self.d, "run")
+        self.log = os.path.join(self.d, "fakelog")
+
+    def _plan(self, content, name="plan.md"):
+        p = os.path.join(self.d, name)
+        with open(p, "w") as f:
+            f.write(content)
+        return p
+
+    def _run(self, plan_path, responses):
+        # Fresh log every invocation so the fake's response index starts at 0 and
+        # the log reflects only this invocation's dispatches.
+        if os.path.exists(self.log):
+            os.remove(self.log)
+        env = os.environ.copy()
+        env["FORGE_FAKE_LOG"] = self.log
+        resp_path = os.path.join(self.d, "responses.json")
+        with open(resp_path, "w") as f:
+            json.dump(responses, f)
+        env["FORGE_FAKE_RESPONSES"] = resp_path
+        return subprocess.run(
+            [sys.executable, str(SCRIPT_PATH), plan_path,
+             "--spec", self.spec, "--run-dir", self.run_dir,
+             "--codex-bin", self.fake],
+            cwd=self.d, capture_output=True, text=True, env=env,
+        )
+
+    def test_resume_skips_passed_tasks_and_resumes_at_escalated(self):
+        plan = self._plan(PLAN_TWO_TRIVIAL)  # task 2 depends on task 1
+        # Run 1: task 1 passes, task 2 crashes both attempts -> escalated, exit 2.
+        res1 = self._run(plan, responses=[
+            {"exit": 0, "msg": ""},  # task 1 worker
+            {"exit": 1, "msg": ""},  # task 2 worker attempt 1
+            {"exit": 1, "msg": ""},  # task 2 worker attempt 2
+        ])
+        self.assertEqual(res1.returncode, 2, res1.stderr)
+        # Run 2 (same run-dir): task 1 is skipped (passed receipt); task 2 resumes
+        # and now passes.
+        res2 = self._run(plan, responses=[{"exit": 0, "msg": ""}])
+        self.assertEqual(res2.returncode, 0, res2.stderr)
+        joined = "\n".join(ln for ln in open(self.log).read().splitlines())
+        self.assertNotIn("task-1-worker-last", joined)  # task 1 not re-dispatched
+        self.assertIn("task-2-worker-last", joined)     # task 2 resumed
+        with open(os.path.join(self.run_dir, "run.json")) as f:
+            summary = json.load(f)
+        self.assertEqual(summary["status"], "passed")
+        with open(plan) as f:
+            content = f.read()
+        self.assertIn("[x] Done", content)
+
+    def test_resume_forwards_to_run_plan_with_declared_signature(self):
+        # resume(plan_path, spec_path, run_dir) is the documented re-invocation
+        # entry: it forwards to run_plan with the production defaults (codex on
+        # PATH, cwd = getcwd()). Guards against signature drift and dead code.
+        calls = []
+        orig = forge_run.run_plan
+
+        def _record(*a, **k):
+            calls.append(a)
+            return 0
+
+        forge_run.run_plan = _record
+        try:
+            rc = forge_run.resume("plan.md", "spec.md", "/run/dir")
+        finally:
+            forge_run.run_plan = orig
+        self.assertEqual(rc, 0)
+        self.assertEqual(len(calls), 1)
+        args = calls[0]
+        self.assertEqual(args[0], "plan.md")
+        self.assertEqual(args[1], "spec.md")
+        self.assertEqual(args[2], "/run/dir")
+        self.assertEqual(args[3], "codex")
+        self.assertEqual(args[4], os.getcwd())
+
+
 if __name__ == "__main__":
     unittest.main()
````
