# References

URLs opened while writing this gate, and what was taken from each. No fetch failed.

- https://arxiv.org/html/2512.17419 — SWE-Bench++ Appendix C uses three independent automated reviewers, rejects low-clarity instances, and reports precision 0.93–0.95 against a human near 0.96 with uneven recall, so a model verdict here cannot accept a brief.
- https://arxiv.org/abs/2512.17419 — Same article as the HTML URL; the abs fetch returned that article.
- https://openai.com/index/introducing-swe-bench-verified/ — Issue clarity is scored 0–3, labels 2 and 3 are inadequate and discarded, and three annotators are ensembled by highest severity so any one severe flag rejects the sample.
- https://cdn.openai.com/introducing-swe-bench-verified/swe-b-annotation-instructions.pdf — The issue-text rubric runs from 0 (well-specified) to 3 (almost impossible to understand what is being asked).
- https://arxiv.org/html/2601.11868 — Terminal-Bench Appendix B requires that behaviour described in the instruction is tested, that behaviour checked by the tests is described, that a hard-coded solution fails review, and that the oracle solution is not left where the agent can read it.
- https://arxiv.org/abs/2601.11868 — Same article as the HTML URL; the abs fetch returned that article.
- https://github.com/harbor-framework/terminal-bench-3/blob/main/rubrics/task-implementation.toml — The implementation rubric fails a task when described behaviour is untested or when answers are leaked into what the agent can read, and it fails a solution that prints a hard-coded answer.
