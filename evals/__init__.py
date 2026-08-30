"""The eval harness: it grades the calling agent, not the code under analysis. Lives
outside src/ on purpose -- nothing here ships in the wheel, and nothing in src/ knows
it exists. See evals/README.md for how to run it.
"""
