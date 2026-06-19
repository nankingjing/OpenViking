# Tau2 train case1 slot1 advice

## Scope

- Dataset/domain: `tau2` / `airline`
- Split/index: `train` case `1`
- Launcher: `benchmark/tau2/train/restart_vikingbot_train_eval.sh`
- Required slot: `--slot 1`
- Exit metric: train for 2 epochs, then evaluate the same train case with 8 trials.

## Runtime requirement

Do not use `OPENVIKING_PROMPT_TEMPLATES_DIR` for this case. Run from the worktree and use the worktree virtualenv so bundled prompt templates resolve to this worktree:

```bash
cd /Users/bytedance/workspace/openviking-tau2-case0-slot1
export VIRTUAL_ENV=$PWD/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
unset OPENVIKING_PROMPT_TEMPLATES_DIR
```

Expected checks:

- `which python` -> `$PWD/.venv/bin/python`
- `which openviking-server` -> `$PWD/.venv/bin/openviking-server`
- `openviking.__file__` under this worktree
- `PromptManager._get_bundled_templates_dir()` -> `$PWD/openviking/prompts/templates`

## Best tested result

- Best commit in this worktree: `f4b911bd Tune tau2 memory gate extraction`
- Best run: `result/tau2/train_1/run_airline_20260619_223605`
- Command shape:
  ```bash
  bash benchmark/tau2/train/restart_vikingbot_train_eval.sh \
    --slot 1 \
    --epochs 2 \
    --train-index 1 \
    --eval-split train \
    --eval-index 1 \
    --trials 8 \
    --skip-final-eval \
    --keep-recent-results 20 \
    --force-baseline-recompute
  ```
- Baseline: `1/8 = 12.50%`
- Final after epoch 1: `4/8 = 50.00%`
- Delta: `+37.50pp`
- Previous best: `42706c9f`, `3/8 = 37.50%`; keep `f4b911bd` as the new rollback point.

## Current generic YAML strategy

Use the committed changes in:

- `openviking/prompts/templates/memory/trajectories.yaml`
- `openviking/prompts/templates/memory/experiences.yaml`

The changes are intentionally generic, not case-specific:

- When read-only action checks and DB mismatch expose a forbidden state-changing write, do not turn the write into a positive post-write workflow.
- Do not generate experiences whose applicability depends on future agents seeing hidden evaluation metadata such as `action_checks`, rubric, or CaseSpec.
- Map evaluation-only failures to agent-visible gates when possible: object status, ownership, exact timestamp arithmetic, cabin/membership/insurance/refund prerequisites, confirmation, and target binding.
- If an observable gate forbids a write, produce a narrow refusal/done or transfer boundary; the Approach must not call the forbidden state-changing tool.
- For cancellation, require exact full timestamp arithmetic for 24-hour windows and never let user/support-rep claims override tool facts.

## Observed memory behavior in the best run

Run `run_airline_20260619_223605` produced the intended second-epoch memory update:

- Epoch 0 still created a positive cancellation confirmation experience from a failed write; this made epoch-0 eval `0/8`.
- Epoch 1 corrected it by updating the experience into a gate-first flow: verify cancellation eligibility; if ineligible, `communicate_with_user` then `done`; if eligible, list details, get explicit `yes`, then call `cancel_reservation`.
- Epoch 1 final eval passed `4/8`. Passed trials avoided `cancel_reservation` and used `transfer_to_human_agents`/`done`; failed trials still called `cancel_reservation`.

## Next experiment ideas

To improve beyond `4/8`, focus on making the first committed experience gate-first rather than requiring one extra epoch to correct it:

- Tighten failure-to-experience creation so failed writes cannot create a positive write workflow just because the trace found an apparent eligibility path.
- Require failed-write experiences to put eligibility/refusal gates before any write branch, and only include a write branch when the successful evaluated outcome or visible feedback explicitly supports the write.
- Preserve generic wording; do not mention this case number, specific passenger, reservation ID, route, or exact timestamps in YAML rules.
