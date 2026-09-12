import argparse
import csv
import gzip
import json
import math
import random
import statistics
from pathlib import Path
from types import SimpleNamespace

from task_evolver.fit import fit_bradley_terry, importance_from_z


def workload(seed, shape):
    rng = random.Random(seed)
    arrival = 0.0
    calls = []
    for index in range(80):
        arrival += rng.expovariate(1.0)
        session = rng.randrange(4)
        duration = 2 if shape == "uniform" else rng.choice([1, 1, 1, 2, 2, 3, 5, 8])
        calls.append(
            {"id": index, "arrival": arrival, "duration": duration, "session": session}
        )
    return calls


def replay(calls, scores, policy, parameter):
    todo = list(calls)
    waiting = []
    now = 0.0
    result = []
    while todo or waiting:
        if not waiting:
            now = max(now, todo[0]["arrival"])
        while todo and todo[0]["arrival"] <= now:
            waiting.append(todo.pop(0))

        def rank(call, now=now):
            age = now - call["arrival"]
            importance = scores[str(call["session"])]
            if policy == "fifo":
                return (0, call["arrival"], call["id"])
            if policy == "aging" and age >= parameter:
                return (-1, call["arrival"], call["id"])
            score = importance + (age * parameter if policy == "linear" else 0)
            return (0, -score, call["arrival"], call["id"])

        selected = min(waiting, key=rank)
        waiting.remove(selected)
        wait = now - selected["arrival"]
        result.append(
            dict(
                selected,
                start=now,
                wait=wait,
                slowdown=(wait + selected["duration"]) / selected["duration"],
            )
        )
        now += selected["duration"]
    return result


def metrics(trace):
    urgent = [call["wait"] for call in trace if call["session"] == 0]
    background = [call for call in trace if call["session"] != 0]
    sessions = [
        {
            "session": session,
            "grants": sum(call["session"] == session for call in trace),
            "max_wait": max(
                call["wait"] for call in trace if call["session"] == session
            ),
            "slowdown": statistics.mean(
                call["slowdown"] for call in trace if call["session"] == session
            ),
        }
        for session in range(4)
    ]
    gaps = []
    for session in range(4):
        own = [call for call in trace if call["session"] == session]
        previous_end = 0.0
        for call in own:
            gaps.append(call["start"] - max(previous_end, call["arrival"]))
            previous_end = call["start"] + call["duration"]
    return {
        "urgent_mean_wait": statistics.mean(urgent),
        "urgent_p95_wait": sorted(urgent)[math.ceil(0.95 * len(urgent)) - 1],
        "background_max_wait": max(call["wait"] for call in background),
        "background_slowdown": statistics.mean(
            row["slowdown"] for row in sessions if row["session"] != 0
        ),
        "max_unserved_seconds": max(gaps),
        "sessions": sessions,
    }


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    pairs = [
        SimpleNamespace(a_key="0", b_key=str(session), p_a_wins=0.9, weight=1.0)
        for session in range(1, 4)
    ]
    scores = importance_from_z(fit_bradley_terry(pairs, "1"))
    policies = (
        [("fifo", 0), ("priority", 0)]
        + [("aging", cap) for cap in [2, 4, 8, 16]]
        + [("linear", rate) for rate in [0.25, 1, 4, 16]]
    )
    rows = []
    with gzip.open(output / "replay-trace.jsonl.gz", "wt") as trace_file:
        for shape in ["uniform", "long_tail"]:
            for seed in range(10):
                calls = workload(seed, shape)
                order = list(policies)
                random.Random(seed + 100).shuffle(order)
                for policy, parameter in order:
                    trace = replay(calls, scores, policy, parameter)
                    row = dict(
                        shape=shape,
                        seed=seed,
                        policy=policy,
                        parameter=parameter,
                        **metrics(trace),
                    )
                    rows.append(row)
                    for call in trace:
                        trace_file.write(
                            json.dumps(
                                dict(
                                    shape=shape,
                                    seed=seed,
                                    policy=policy,
                                    parameter=parameter,
                                    **call,
                                )
                            )
                            + "\n"
                        )
    summary = []
    for shape in ["uniform", "long_tail"]:
        for policy, parameter in policies:
            group = [
                row
                for row in rows
                if (row["shape"], row["policy"], row["parameter"])
                == (shape, policy, parameter)
            ]
            point = {
                "shape": shape,
                "policy": policy,
                "parameter": parameter,
                "rounds": len(group),
            }
            for metric in [
                "urgent_mean_wait",
                "urgent_p95_wait",
                "background_max_wait",
                "background_slowdown",
                "max_unserved_seconds",
            ]:
                values = [row[metric] for row in group]
                point[metric] = statistics.mean(values)
                point[metric + "_sd"] = statistics.stdev(values)
            summary.append(point)
    for point in summary:
        point["frontier"] = not any(
            other["shape"] == point["shape"]
            and other["urgent_mean_wait"] <= point["urgent_mean_wait"]
            and other["background_max_wait"] <= point["background_max_wait"]
            and (
                other["urgent_mean_wait"] < point["urgent_mean_wait"]
                or other["background_max_wait"] < point["background_max_wait"]
            )
            for other in summary
        )
    with (output / "summary.csv").open("w") as file:
        writer = csv.DictWriter(file, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    (output / "scores.json").write_text(json.dumps(scores, indent=2) + "\n")
    (output / "rounds.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    print(
        json.dumps(
            {
                "rounds": len(rows),
                "calls": len(rows) * 80,
                "frontier": [point for point in summary if point["frontier"]],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
