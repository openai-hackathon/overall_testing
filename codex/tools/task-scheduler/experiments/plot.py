import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    with (args.results / "summary.csv").open() as file:
        rows = list(csv.DictReader(file))
    figure, axes = plt.subplots(1, 2, figsize=(13, 5), layout="constrained")
    colors = {
        "fifo": "#64748b",
        "priority": "#dc2626",
        "aging": "#16a34a",
        "linear": "#2563eb",
    }
    for axis, shape in zip(axes, ["uniform", "long_tail"]):
        points = [row for row in rows if row["shape"] == shape]
        frontier = sorted(
            (float(row["background_max_wait"]), float(row["urgent_mean_wait"]))
            for row in points
            if row["frontier"] == "True"
        )
        axis.plot(
            *zip(*frontier),
            color="#0f172a",
            linestyle="--",
            alpha=0.6,
            label="Observed frontier",
        )
        offsets = {
            "fifo": (-100, 65),
            "priority": (15, -5),
            "aging 2": (-100, 50),
            "aging 4": (-100, 35),
            "aging 8": (-100, 20),
            "aging 16": (-100, 5),
            "linear 16": (15, -15),
            "linear 4": (15, 0),
            "linear 1": (15, 0),
            "linear 0.25": (-95, 10),
        }
        for row in points:
            x, y = float(row["background_max_wait"]), float(row["urgent_mean_wait"])
            label = row["policy"] + (
                " " + row["parameter"] if row["policy"] in ("aging", "linear") else ""
            )
            axis.errorbar(
                x,
                y,
                xerr=float(row["background_max_wait_sd"]),
                yerr=float(row["urgent_mean_wait_sd"]),
                color=colors[row["policy"]],
                alpha=0.16,
                linewidth=1,
            )
            axis.scatter(x, y, color=colors[row["policy"]], s=35)
            axis.annotate(
                label,
                (x, y),
                xytext=offsets[label],
                textcoords="offset points",
                fontsize=8,
                arrowprops={"arrowstyle": "-", "color": "#64748b", "linewidth": 0.5},
            )
        axis.set(
            title=shape.replace("_", " ").title(),
            xlabel="Background maximum wait (seconds, lower is better)",
            ylabel="Important task mean wait (seconds, lower is better)",
        )
        axis.set_ylim(
            bottom=-3, top=max(float(row["urgent_mean_wait"]) for row in points) * 1.35
        )
        axis.grid(alpha=0.15)
    figure.suptitle(
        "Single-slot controlled replay: 10 paired seeds; bars show sample SD"
    )
    figure.savefig(args.results / "frontier.svg")
    figure.savefig(args.results / "frontier.png", dpi=180)
