# AGENTS.md — the in-session loop

You were dispatched by `tmq` into a worktree to resolve **one GitHub issue**.
The issue's **Acceptance criteria** are your contract. This file is the same for
every agent — pi (MiniMax M3 / DeepSeek v4) or Claude. It does not assume you are
smart; it assumes you can print one line reliably. The discipline lives in the
loop, not in your good intentions.

## The state contract — DO THIS OR YOU ARE INVISIBLE

At every transition below, print your state on its own line, exactly:

```
<<AGENT-STATE: WORKING>>
```

This line is how the fleet sees you. `aoe` greps your pane for the most recent
`<<AGENT-STATE: …>>` and turns it into the `ST` column that `tms` and the
operator watch. If you stop printing it, the operator cannot tell what you are
doing or whether you are waiting on them — you are a hung session.

Valid states (this is the whole vocabulary — do not invent others):

| State you print | Means | aoe ST | Who acts next |
|---|---|---|---|
| `<<AGENT-STATE: PLAN-REVIEW>>` | plan written, not yet coding | Waiting 🟡 | **human** approves/edits the plan |
| `<<AGENT-STATE: WORKING>>` | executing / TDD / fixing | Running 🔵 | nobody — leave it alone |
| `<<AGENT-STATE: PR-REVIEW>>` | PR open, needs review | Waiting 🟦 | `tmq … --type review` auto-fires |
| `<<AGENT-STATE: MERGE-READY>>` | tests green + AC verified + review clean | Waiting 🟢 | **human** merges |
| `<<AGENT-STATE: BLOCKED: reason>>` | stuck / needs a decision | Waiting 🔴 | **human** answers |
| `<<AGENT-STATE: DONE>>` | merged, worktree clean | Stopped ⚪ | nobody |

Three of these stop for a human: 🟡 read a plan, 🔴 answer a question, 🟢 merge.
Everything else is autonomous. One line per transition — no more, no less.

## Step 0 — Plan gate (STOP for a human)

1. Read the issue. Restate **Scope**, **Out of scope**, and **Acceptance
   criteria** in your own words. If any AC is ambiguous, that is a Step-0 block.
2. Write a short plan: files you will touch, the **first failing test** you will
   write, and anything uncertain.
3. Print `<<AGENT-STATE: PLAN-REVIEW>>` and **STOP. Do not write code.**
   A human approves, edits, or redirects.
   - **Fast path:** if dispatched `--type fix` AND the change is < ~20 lines and
     unambiguous, print the plan inline, skip the stop, and go to Step 1.

## Step 1 — TDD-first execute

1. Print `<<AGENT-STATE: WORKING>>`.
2. Write the failing test **FIRST**. Run it. **Paste the red output.**
   No test = no code. The pasted output is your proof, not your claim —
   we do not trust "this should fail," we read the failure.
3. Write the minimum code to pass. Run the test. **Paste the green output.**
4. If the same mechanical step fails **twice**, do not thrash. Print
   `<<AGENT-STATE: BLOCKED: <what failed>>>` and STOP.

## Step 2 — Self-verify against AC, THEN open the PR

1. Walk the issue's **Acceptance criteria one by one**. For each, state how your
   change satisfies it and **which test covers it**. If any AC is unmet, return
   to Step 1 — do not open the PR.
2. Open the PR (body per the team's PR structure; `Closes #<num>`).
3. Print `<<AGENT-STATE: PR-REVIEW>>`. A reviewer runs in this same worktree.

## Step 3 — Fix review findings

1. Print `<<AGENT-STATE: WORKING>>`. Fix every **P0**; address or rebut **P1s**.
2. Re-run the tests. Re-confirm the AC still hold.
3. When tests are green, AC verified, and review is clean, print
   `<<AGENT-STATE: MERGE-READY>>` and STOP. A human merges.

## When to STOP for a human (print BLOCKED and wait)

- The plan needs a decision you cannot make from the issue alone (Step 0).
- A choice changes **scope, a DB schema, an external service, or another repo**.
- You failed the same mechanical step twice (don't burn the budget thrashing).
- An AC is impossible or contradicts another AC.

Never guess past one of these. **A clear stop is cheaper than a confident wrong
build** — especially on a model that flakes mechanically. The operator would
rather answer one question than unwind an afternoon of plausible-but-wrong work.

## Repo conventions (tms-specific)

- Worktrees: `/root/wt-<repo>-<num>` (managed by `tmq`). A fresh worktree has no
  deps — install before any test/build hook.
- Branch: `feat/issue-<num>-<slug>` (or `fix/`). Commit: `type: description`,
  no emojis, no generated-by footer.
- Session naming: `feat-<repo>#<num>` / `fix-<repo>#<num>` / `review-<repo>#<num>`.
- Test command for this repo: `pytest tests/` (see issue #8 scaffold).
