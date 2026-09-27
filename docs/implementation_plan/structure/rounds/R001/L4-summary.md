# L4 summary — RQ7 prior-art review

Produced 19 findings (S300–S318), every quote taken verbatim from a page actually fetched (GitHub raw READMEs, Harbor docs, two Mercor blog posts). Headline: the substrate we want to build already has a de facto open stack — Harbor (harness + task format + RL rollout generation), Harbor-format task corpora (Snowflake data-eng-bench as the closest DE analog), and SkyRL as the RL consumer — and Mercor has publicly documented both the judge-calibration recipe and the harness-quality economics that should anchor our pitch. The main things we cannot use are Mercor's closed OTS task data (license-blocked) and universql (needs a live Snowflake account, so useless for hermetic sandboxing); fakesnow plus sqlglot cover Snowflake emulation/transpilation with documented fidelity caveats that must be handled in grading (ORDER BY, liberal dialect, transpiler-not-validator).

| Project | Verdict | Key evidence |
|---|---|---|
| Harbor (laude-institute) | adopt — harness + task format + reward-file contract | S300, S301 |
| Snowflake data-eng-bench | adopt tasks / copy dual-backend (DuckDB-hermetic + Snowflake) pattern | S302, S303 |
| dbt-labs ade-bench | copy-pattern — shared projects, auto-generated equality tests, oracle "sage" agent as task CI | S304, S305 |
| Terminal-Bench | copy-pattern (conceptual ancestor; superseded by Harbor) | S306 |
| OpenAI MLE-Bench | copy-pattern — deterministic grading + ≥3 seeds; cannot-use content (Kaggle); leakage postmortems as warning | S307 |
| Mercor APEX-Agents 1.1 | copy-pattern — anti-scattergunning judge + GEPA prompt-optimized rubrics on 1,407 hand-labeled items; benchmark itself closed | S308, S309 |
| Mercor ApexAgents-SkyRL-Recipe + SkyRL | adopt architecture / cannot-use data; harness fixes ≈ one free epoch; TITO is a hard RL requirement | S310, S311, S312, S313, S314 |
| UC Berkeley DataAgentBench | copy-pattern — per-query validate.py + ground_truth.csv, read-only tools, validator versioning | S315 |
| fakesnow vs universql | fakesnow adopt-with-caveats (hermetic, isolated DBs per connection); universql cannot-use (needs real account) | S316, S317 |
| sqlglot | adopt — Snowflake↔DuckDB transpilation at task-build time, never as verification | S318 |

Surprises: (1) the entire Harbor→SkyRL→Mercor chain is public and import-only with zero forks, meaning our differentiation must be the licensed task corpus + calibrated graders, not plumbing; (2) Mercor quantified harness quality — sandbox fixes bought ~6 reward points with zero training, our strongest sales datum; (3) binary inclusion-only rubrics are provably gameable (scattergunning), which validates emphasizing deterministic verifiers. Could NOT find evidence for: the actual updated APEX judge prompts/rubrics themselves (only the methodology is public; judge artifacts are closed, though Mercor's archipelago grading runner repo exists and is referenced by the recipe), any public Harbor-native RL recipe for SQL/data-engineering tasks specifically (SkyRL-SQL predates the Harbor integration and is not Harbor-format), and independent third-party usage reports of fakesnow/universql reliability in agent benchmarks beyond their own READMEs.
