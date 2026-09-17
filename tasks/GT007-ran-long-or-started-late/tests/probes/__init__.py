"""Reusable grading probes. Nothing in here is GT007-specific.

`deploy_probe` is the deployment-axis probe: it reads the episode's own action
log, finds the canaried deploys the agent actually shipped, and answers the one
question a canary cannot answer about itself - whether it was watching
anything. Copy the directory into a sibling task and it works unchanged; the
only task-specific input is the path to the world's observability definitions.
"""
