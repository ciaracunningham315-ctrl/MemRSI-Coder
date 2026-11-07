# EvoCode Benchmark formal run

Formal results are generated only from recorded task attempts and independent grading.

Planned: 54; recorded: 54; verified grades: 54; unverified: 0; coverage: 100.0%.
Solved: 46; overall solve rate: 85.2%; successes / planned: 85.2%.
NSRR: calculated; K/N: 0.8333333333333334.

## 1. Condition Summary (五大核心指标分组汇总)

| Condition | Planned | Verified | Unverified | Solved | Solve Rate | Tool Calls / Solved | Online Tokens / Solved | Learning Tokens | Total Tokens | Regression Rate | Published / Proposed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A1 | 9 | 9 | 0 | 8 | 88.9% | 38.6 | 445644.8 | 0 | 3892464 | 0.0% | 0/0 |
| A2 | 9 | 9 | 0 | 8 | 88.9% | 41.9 | 619516.0 | 0 | 5022201 | 0.0% | 0/0 |
| B2 | 12 | 12 | 0 | 10 | 83.3% | 39.2 | 593983.5 | 0 | 6392453 | 0.0% | 0/0 |
| B3 | 12 | 12 | 0 | 9 | 75.0% | 37.8 | 517604.0 | 62922 | 5844047 | 0.0% | 0/1 |
| B4 | 12 | 12 | 0 | 11 | 91.7% | 38.2 | 578561.8 | 51362 | 7025718 | 0.0% | 1/1 |

## 2. Stage Solve Rate Breakdown (分阶段成功率)

| Condition | T1 (seed) | T2 (transfer) | T3 (stale) | T4 (heldout) | Overall |
|---|---:|---:|---:|---:|---:|
| A1 | — | 3/3 (100.0%) | 2/3 (66.7%) | 3/3 (100.0%) | 8/9 (88.9%) |
| A2 | — | 2/3 (66.7%) | 3/3 (100.0%) | 3/3 (100.0%) | 8/9 (88.9%) |
| B2 | 2/3 (66.7%) | 3/3 (100.0%) | 2/3 (66.7%) | 3/3 (100.0%) | 10/12 (83.3%) |
| B3 | 2/3 (66.7%) | 2/3 (66.7%) | 2/3 (66.7%) | 3/3 (100.0%) | 9/12 (75.0%) |
| B4 | 2/3 (66.7%) | 3/3 (100.0%) | 3/3 (100.0%) | 3/3 (100.0%) | 11/12 (91.7%) |

## 3. Skill Transfer & Post-Meta Summary (B2 / B3 / B4 迁移与元进化对比)

| Condition | T2 Transfer Solve Rate | T4 Heldout Solve Rate | Combined (T2+T4) Solve Rate | Post-Meta (T3+T4) Solve Rate |
|---|---:|---:|---:|---:|
| B2 | 100.0% | 100.0% | 100.0% | 83.3% |
| B3 | 66.7% | 100.0% | 83.3% | 83.3% |
| B4 | 100.0% | 100.0% | 100.0% | 100.0% |

## 4. Paired-Solved Cost (共同成功题目配对成本)

| Pair | Common Solved Tasks | Left Tool Calls | Right Tool Calls | Delta Tool Calls | Left Online Tokens | Right Online Tokens | Delta Online Tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| A1_vs_A2 | 7 | 39.6 | 41.3 | 1.7 | 469545.6 | 587071.7 | 117526.1 |
| B2_vs_B3 | 8 | 36.4 | 37.0 | 0.6 | 548563.9 | 460080.4 | -88483.5 |
| B3_vs_B4 | 9 | 37.8 | 36.6 | -1.2 | 517604.0 | 545493.3 | 27889.3 |
| B2_vs_B4 | 10 | 39.2 | 37.3 | -1.9 | 593983.5 | 559837.8 | -34145.7 |

## 5. Position Ledger (54 位置明细)

| # | Condition | Repository | Stage | State | Grade | Solved |
|---:|---|---|---|---|---|---|
| 1 | A2 | repo_api | t2_transfer | recorded | graded | True |
| 2 | A2 | repo_cli | t2_transfer | recorded | graded | False |
| 3 | A2 | repo_data | t2_transfer | recorded | graded | True |
| 4 | A1 | repo_api | t2_transfer | recorded | graded | True |
| 5 | A1 | repo_cli | t2_transfer | recorded | graded | True |
| 6 | A1 | repo_data | t2_transfer | recorded | graded | True |
| 7 | A2 | repo_api | t3_stale | recorded | graded | True |
| 8 | A2 | repo_cli | t3_stale | recorded | graded | True |
| 9 | A2 | repo_data | t3_stale | recorded | graded | True |
| 10 | A1 | repo_api | t3_stale | recorded | graded | True |
| 11 | A1 | repo_cli | t3_stale | recorded | graded | True |
| 12 | A1 | repo_data | t3_stale | recorded | graded | False |
| 13 | A1 | repo_api | t4_heldout | recorded | graded | True |
| 14 | A1 | repo_cli | t4_heldout | recorded | graded | True |
| 15 | A1 | repo_data | t4_heldout | recorded | graded | True |
| 16 | A2 | repo_api | t4_heldout | recorded | graded | True |
| 17 | A2 | repo_cli | t4_heldout | recorded | graded | True |
| 18 | A2 | repo_data | t4_heldout | recorded | graded | True |
| 19 | B3 | repo_api | t1_seed | recorded | graded | True |
| 20 | B3 | repo_cli | t1_seed | recorded | graded | False |
| 21 | B3 | repo_data | t1_seed | recorded | graded | True |
| 22 | B3 | repo_api | t2_transfer | recorded | graded | False |
| 23 | B3 | repo_cli | t2_transfer | recorded | graded | True |
| 24 | B3 | repo_data | t2_transfer | recorded | graded | True |
| 25 | B3 | repo_api | t3_stale | recorded | graded | False |
| 26 | B3 | repo_cli | t3_stale | recorded | graded | True |
| 27 | B3 | repo_data | t3_stale | recorded | graded | True |
| 28 | B3 | repo_api | t4_heldout | recorded | graded | True |
| 29 | B3 | repo_cli | t4_heldout | recorded | graded | True |
| 30 | B3 | repo_data | t4_heldout | recorded | graded | True |
| 31 | B4 | repo_api | t1_seed | recorded | graded | True |
| 32 | B4 | repo_cli | t1_seed | recorded | graded | False |
| 33 | B4 | repo_data | t1_seed | recorded | graded | True |
| 34 | B4 | repo_api | t2_transfer | recorded | graded | True |
| 35 | B4 | repo_cli | t2_transfer | recorded | graded | True |
| 36 | B4 | repo_data | t2_transfer | recorded | graded | True |
| 37 | B4 | repo_api | t3_stale | recorded | graded | True |
| 38 | B4 | repo_cli | t3_stale | recorded | graded | True |
| 39 | B4 | repo_data | t3_stale | recorded | graded | True |
| 40 | B4 | repo_api | t4_heldout | recorded | graded | True |
| 41 | B4 | repo_cli | t4_heldout | recorded | graded | True |
| 42 | B4 | repo_data | t4_heldout | recorded | graded | True |
| 43 | B2 | repo_api | t1_seed | recorded | graded | True |
| 44 | B2 | repo_cli | t1_seed | recorded | graded | False |
| 45 | B2 | repo_data | t1_seed | recorded | graded | True |
| 46 | B2 | repo_api | t2_transfer | recorded | graded | True |
| 47 | B2 | repo_cli | t2_transfer | recorded | graded | True |
| 48 | B2 | repo_data | t2_transfer | recorded | graded | True |
| 49 | B2 | repo_api | t3_stale | recorded | graded | True |
| 50 | B2 | repo_cli | t3_stale | recorded | graded | False |
| 51 | B2 | repo_data | t3_stale | recorded | graded | True |
| 52 | B2 | repo_api | t4_heldout | recorded | graded | True |
| 53 | B2 | repo_cli | t4_heldout | recorded | graded | True |
| 54 | B2 | repo_data | t4_heldout | recorded | graded | True |

Stop reason: none.
