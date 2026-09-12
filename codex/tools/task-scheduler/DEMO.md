# Core demo

Run each command from the repository root. Use Python 3.11 or newer.

## Verify two real Codex processes

Build this checkout:

```sh
cd codex-rs
cargo build -p codex-cli
cd ..
```

Run the controlled demonstration on macOS or Linux:

```sh
PYTHONPATH=tools/task-scheduler python3 tools/task-scheduler/experiments/app_server_demo.py \
  --codex codex-rs/target/debug/codex --output /tmp/scheduler-demo
```

The script starts two real app-server processes with isolated configuration.
It uses a local SSE model stub and deterministic fake expansion. It sends no
requests to OpenAI. Each real tool runs a short Python command.

The script queues both clients behind one controlled lease. A scripted human
preference changes from 0.1 to 0.9, expands the pair, and refits. Incident work
must then receive the first grant, after the other client arrives first.
The assertion checks that both cwd bindings resolve to their learned task keys.
The original enqueue timestamps must stay unchanged.

A second phase interrupts one queued turn. The other client's queued call must
still complete. The report requires three successful tool outputs, distinct
client identities, an interrupted turn, and no grant for that canceled turn.

`app-server-report.json` records the checks. `app-server-trace.jsonl` records
queue events. The checked-in example is under `results/app-server/`.
The process test uses fake model output to control arrival timing. Real OpenAI
expansion was verified separately with `gpt-5.6-luna` and medium effort.
`results/live-learning-report.json` records a real service question answered with
a synthetic 0.9 preference, followed by two refits and nine learned keys.

## Try real automatic learning

Follow the [service commands](README.md#automatic-questions-in-the-service).
Set `CODEX_SCHEDULER_SERVICE=127.0.0.1:8765` on both app-server processes.
A new bound task key triggers one question in the service terminal. Enter a
number from 0 to 1. The service saves the preference and calls OpenAI expansion.
Subsequent grants use the published BT importance plus aging.

An existing learned key does not trigger another question. Use CLI `compare`
to correct it with one active writer, as described in the README.

## Reproduce the frontier

```sh
PYTHONPATH=tools/task-scheduler python3 tools/task-scheduler/experiments/replay.py \
  --output /tmp/scheduler-replay
uv run --with matplotlib python tools/task-scheduler/experiments/plot.py /tmp/scheduler-replay
```

The checked-in results are under `results/replay/`:

- `frontier.png` and `frontier.svg`: the observed two-objective frontier.
- `summary.csv`: means and sample standard deviations across ten paired seeds.
- `rounds.jsonl`: per-round and per-session metrics.
- `scores.json`: BT scores from fixed synthetic seed preferences.
- `replay-trace.jsonl.gz`: every arrival, duration, grant time and wait.

Each workload has 80 calls, four sessions, one slot, and no preemption.
Interarrival times follow an exponential distribution with mean one second.
Uniform service time is two seconds. Bounded long-tail times are sampled from
`[1, 1, 1, 2, 2, 3, 5, 8]`. Each shape uses seeds 0 through 9. Policy order is
shuffled per seed. All policies share that seed's arrival and service times.
This is a finite overloaded workload, not a steady-state stability experiment.

The replay compares FIFO, strict importance priority, aging caps 2/4/8/16 seconds,
and linear rates 0.25/1/4/16 points per second. Aging serves the oldest overdue
call first; otherwise it uses importance. Linear uses `importance + age * rate`.
A parity test compares every replay grant against the actual Python service for
all four linear rates and both workload shapes. Replay uses learned importance
for all scored policies; it does not reuse historical cwd tier scores.

The plot minimizes important-session mean wait and background maximum wait.
Error bars show sample SD, not confidence intervals or significance tests.
The results also include important p95 wait, background per-session mean slowdown,
per-session grant counts and maximum wait, and the longest pending interval
without service. Slowdown is `(wait + service) / service`. No-work intervals do
not count as lack of service. Each round has the same number of calls; the
important group has one session, while background slowdown gives each of the
three background sessions equal weight.

This experiment shows a workload-dependent tradeoff. It does not establish a
universal best rate, a waiting-time guarantee, or production incident outcomes.
Reproducing the input and numerical results does not require an API key.

## Delivery boundary

The core demo includes learning, shared admission, cancellation, and reproducible
fairness experiments. Durable queues, reconnect, lease recovery, MoM and Modal
routing remain deferred. No preemption of running tools is implemented.

Review the work in three parts: online learning and trace handling, the real
app-server demonstration, and replay with its generated results.
