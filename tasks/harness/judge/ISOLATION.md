# Isolation

Two tools, both off the score.

The clarity gate may reject a generated task or hold it. It never accepts one. SWE-Bench++ (arXiv 2512.17419) is the reject-only clarity pattern: an LLM scores issue clarity and a low score drops the instance instead of grading the solution.

The exploit runner treats a pass without a repair as a grader bug. Terminal-Bench (arXiv 2601.11868) is the exploit-agent pattern: a task is attacked before release so a way through the tests is found as a defect, not awarded as a point.

`pass@1` stays in `outcome.py`. `episode_reward` stays in `reward.py`.
