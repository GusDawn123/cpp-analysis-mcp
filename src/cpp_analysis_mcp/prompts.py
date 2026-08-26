"""The guided workflows, published as MCP prompts: multi-tool recipes a single tool
description cannot carry, written as work orders with success first, steps, then rules."""

from __future__ import annotations

CHECKUP = """\
Run a full correctness pass over {source} and fix what it finds.

Success means full_check_file reports zero findings, and every fix is explained in one
line: what changed and why.

{source} must be a standalone program whose main() exercises the code. For a library
file, write a small driver file that calls into it and run this recipe on the driver;
never add a main() to the library itself just to satisfy a tool.

Steps:
1. Call capabilities. Note anything unavailable; a missing detector's silence is not a
   pass, so say plainly what could not be checked.
2. Call full_check_file on {source}.
3. For each finding, open the code it points at and fix the cause, not the message.
4. Run full_check_file again. Repeat until findings is empty. If you believe a finding
   is wrong, say why instead of silencing it.
5. Report what was found, what you changed, which detectors ran, and which could not.

Rules: never delete or weaken a check to get a pass. If a build fails, fix the build
first. If a finding sits in third-party code, report it and leave that code alone.
"""

MAKE_IT_FASTER = """\
Make {source} measurably faster without changing what it computes.

Success means a benchmark_variants report where the adopted version beats the original
with matching output, and a second profile showing the hotspot shrank.

{source} must be a standalone program whose main() runs a real workload. For a library
file, write a small driver that exercises the hot path for a second or more and run
this recipe on the driver; never add a main() to the library itself.

Steps:
1. Call profile_file on {source}. Read samples and confidence before trusting the
   ranking; if it says coarse, lengthen the workload and profile again.
2. Read fingerprints. When the report names library machinery, start from its
   candidates. When it only names your own functions, read them and choose rewrites
   yourself.
3. Write 2 or 3 whole-program variants along those candidates. Keep the printed output
   identical and deterministic: fixed seed, same format.
4. Call benchmark_variants with the original as the baseline. A rejected variant
   answered wrongly or unstably; fix it or drop it, never adopt it.
5. Before adopting the winner, run full_check_file on it and get it clean. Fast and
   wrong is wrong, and fast with a data race is worse.
6. Profile the adopted version and report before and after: hotspot shares, mean
   times, and the code change in one paragraph.

Rules: never claim a speedup without a race behind it. Never adopt a variant whose
output differed. Leave the workload generator alone when the rng fingerprint names it.
"""


def checkup(source: str) -> str:
    return CHECKUP.format(source=source)


def make_it_faster(source: str) -> str:
    return MAKE_IT_FASTER.format(source=source)
