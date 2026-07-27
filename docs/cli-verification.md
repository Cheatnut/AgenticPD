# AgenticPD CLI Verification Guide

All commands run from `flow/agenticpd/`.  Append `--mock-llm --mock-orfs`
to avoid API cost and EDA runtime (each command completes in seconds).

---

## Quick verification loop (8 commands)

```bash
# 1. Full mock optimization (3 iterations)
python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 3

# 2. List trials in the latest session
python3 tools/trial_inspect.py --list \
  --runs-dir runs/sky130hd_gcd/$(ls -t runs/sky130hd_gcd/ | head -1)

# 3. Inspect one trial in detail
python3 tools/trial_inspect.py <trial-id> --stages \
  --runs-dir runs/sky130hd_gcd/$(ls -t runs/sky130hd_gcd/ | head -1)

# 4. Preview cleanup (dry-run — nothing deleted)
python3 tools/clean.py sky130hd gcd --dry-run

# 5. Generate optimization tree PNG
python3 tools/visualize.py runs/sky130hd_gcd/$(ls -t runs/sky130hd_gcd/ | head -1)

# 6. Run again — verify baseline cache hit
python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 2

# 7. List reproducible trials
python3 tools/trial_reproduce.py \
  --runs-dir runs/sky130hd_gcd/$(ls -t runs/sky130hd_gcd/ | head -1) --list

# 8. Clean up (deletes runs/sky130hd_gcd/ + ORFS variants)
python3 tools/clean.py sky130hd gcd --yes
```

---

## Full command reference

### main.py — Optimisation entry point

| # | Command | What it does | Expected output |
|---|---------|-------------|-----------------|
| 1 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 3` | Full mock optimisation, 3 iters | `Iter #0 (Baseline)` → cached → `Iter #1/2/3` each showing Judge decision + StageAgent params + QoR. Generates PNG. |
| 2 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 5 --log-level DEBUG` | Same with DEBUG logging | As above + `agenticpd.log` contains full prompt text (mock decisions visible). |
| 3 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --baseline-only` | Baseline only, no LLM | `Iter #0 (Baseline)` → cached to `.baseline/` → exit. Zero LLM calls. |
| 4 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --platform nangate45` | Different platform | Output under `runs/nangate45_gcd/` instead of `sky130hd_gcd/`. |
| 5 | Run twice: `python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 2` | Baseline cache verification | **1st run:** `Iter #0` + `Baseline cached to`. **2nd run:** `Baseline cache hit (skipping ORFS run)` + starts from `Iter #1`. |
| 6 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --resume latest` | Resume from latest session | `[OPTIMIZER] --resume: loaded N history entries, M tree nodes` → continues from last iteration. |

### tools/trial_inspect.py — Trial viewer

| # | Command | What it does | Expected output |
|---|---------|-------------|-----------------|
| 7 | `--list --runs-dir <session>` | List all trials in a session | Table: Trial ID \| Status \| QoR \| Elapsed. Old trials show `[no params]`. |
| 8 | `<trial_id> --runs-dir <session>` | Single trial detail | Parent lineage / param_diff / elapsed / per-stage summary / QoR. |
| 9 | `<trial_id> --stages --runs-dir <session>` | Add per-stage breakdown | Above + each stage: status \| elapsed \| intermediate ws. |
| 10 | `--latest --runs-dir <session>` | Most recent trial | Same as #8 for the latest trial in the session. |
| 11 | `--failed --runs-dir <session>` | Failed trials only | Only trials with status `failed`. Mock mode produces no failures. |

### tools/trial_reproduce.py — Trial reproduction

| # | Command | What it does | Expected output |
|---|---------|-------------|-----------------|
| 12 | `--runs-dir <session> --list` | List reproducible trials | Trials with full `params` dict. `[no params]` = old trial, cannot reproduce. |
| 13 | `<trial_id> --runs-dir <session>` | Reproduce (real ORFS) | Extracts params → `run_flow()` → compares original vs. reproduced QoR with delta. **Runs real ORFS — not mock-safe.** |

### tools/clean.py — Artifact cleanup

| # | Command | What it does | Expected output |
|---|---------|-------------|-----------------|
| 14 | `sky130hd gcd --dry-run` | Preview what would be deleted | Directory listing with file counts + sizes. `base directory will NOT be affected.` |
| 15 | `sky130hd gcd --yes` | Delete without confirmation | Deletes all ORFS `agenticpd_iter*` + entire `runs/sky130hd_gcd/`. `base` protected. |
| 16 | `sky130hd gcd` (no `--yes`) | Interactive delete | Same listing → `Delete N directories? [y/N]` prompt. Default N (safe). |

### tools/visualize.py — Tree visualisation

| # | Command | What it does | Expected output |
|---|---------|-------------|-----------------|
| 17 | `runs/sky130hd_gcd/<session>/` | Generate optimisation tree PNG | `Tree image saved to .../optimization_tree.png`. Green = baseline path, red = best path. |

### schemas/trial.py — Data model self-test

| # | Command | What it does | Expected output |
|---|---------|-------------|-----------------|
| 18 | `python3 schemas/trial.py` | Run built-in self-tests | `20/20 passed — ALL OK`. Pure Python, zero dependencies. |

### make test — Full test suite

| # | Command | What it does | Expected output |
|---|---------|-------------|-----------------|
| 19 | `make test` | Run all 56 unit tests | `Ran 56 tests in ...s — OK`. No network, no LLM, no EDA. |

---

## Directory layout after verification

```
runs/
  sky130hd_gcd/
    .baseline/
      trial.json                     ← shared baseline cache
    20260727_230000/                 ← session 1
      iter-1-xxxxxxxx/               ← first optimisation trial
        trial.json
      iter-2-yyyyyyyy/
        trial.json
      trials.jsonl                   ← global index
      tree.json
      optimization_tree.png
      agenticpd.log
      config_snapshot.json
    20260727_231500/                 ← session 2 (baseline cache hit)
      iter-1-zzzzzzzz/               ← starts from iter 1 (no iter-0)
        trial.json
      ...
```
