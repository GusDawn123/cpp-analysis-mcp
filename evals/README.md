# evals

The 800-odd tests under `tests/` prove the tools work. Nothing there proves an agent
*uses* them well, and that is a separate failure mode: a server whose every tool is
correct is still useless if the agent reaches for a sanitizer before a linter, calls a
clean compile-time result an all-clear, or claims a rewrite is faster without racing it.

An eval here is a task -- a prompt a user might actually send -- plus expectations about
the calls that should follow. The grader reads the session transcript and says which
habits held. It grades the agent, not the code the agent was pointed at.

## Running it

The fake driver replays recorded transcripts and costs nothing:

```sh
uv run python -m evals.run --driver fake --arm full
```

The real driver runs 23 short headless sessions through your own Claude Code
authentication, which spends usage:

```sh
EVAL_PROJECT=/path/to/orderbook uv run python -m evals.run --driver real --arm full --spend
```

Without `--spend` the real driver refuses rather than starting anything. `EVAL_PROJECT`
points at the C++ corpus the tasks are written against; tasks that need it are skipped by
name when it is unset. `--show-argv` prints the command a task would run and starts
nothing, which is the way to read what a session would look like before paying for one.

`--arm` only reaches the real driver. The fake driver replays whatever is in
`--transcripts`, so a bare-arm score from recordings of a full-arm run measures nothing;
point `--transcripts` at the directory belonging to the arm you mean.

## The two arms

`--arm full` launches the shipped server. `--arm bare` launches `evals.bare_server`, a
wrapper that registers the same thirteen tools with every description cut to its first
sentence -- what each tool would say if nobody had thought about how an agent picks one.
The difference between the two scores is what the product copy in `server.py` is worth.

The wrapper lives here, not in `src/`: the product has no "bare" mode and gains no flag
for one. Both arms register the server under the same name, so tool names, the allowed
list, and the transcripts are identical across them.

## The task files

One YAML file per task in `tasks/`, named for its id. Five check kinds, and no more:

| check | means |
| --- | --- |
| `first_tool` | the session opened with exactly this tool |
| `calls` | each of these appeared somewhere |
| `never` | none of these appeared |
| `after_clean_escalates_to` | `escalates_to` was called after `after` |
| `max_calls` | the session made at most this many tool calls, of any kind |

`max_calls` counts everything in the transcript, `Read` and `Glob` included, since a
budget that ignored them would not notice an agent flailing.

A task file that names a tool the server does not have, or a check nobody implements,
fails to load with the file named. A check that quietly matches nothing would grade green
forever, which is worse than a broken file.

## Scorecard

Real runs have not been executed. The maintainer deferred them: the runs cost usage, and
the cloud they were meant to run in is not available yet. The harness composes the exact
command and refuses to spend without being told to, so the row below fills in when that
call changes.

| arm | tasks | passed | run on |
| --- | --- | --- | --- |
| full | 23 | -- | not yet run |
| bare | 23 | -- | not yet run |

The fake driver does run, over the sample transcripts checked in under
`tests/unit/evals/transcripts/`. Those are hand-written recordings of four failure modes
and two clean sessions -- they exercise the grader, and they are not measurements of any
model.
