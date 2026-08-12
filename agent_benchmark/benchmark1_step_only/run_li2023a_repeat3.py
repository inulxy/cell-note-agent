import copy
import csv
import gzip
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import agent


INPUT_H5AD = (
    "/ssd/deecamp/cellnotes/EpiAgent_data/Li2023a/"
    "Li2023a-brain_tissue/Li2023a-brain_tissue-cell_by_peak.h5ad"
)
RUNS = 3


def run_one(run_id):
    task = copy.deepcopy(agent.TASKS[0])
    task["run_id"] = run_id
    agent.run_task(task)


def parse_peak_rss_gb(log_text):
    match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", log_text)
    if not match:
        return ""
    return f"{int(match.group(1)) / 1024 / 1024:.3f}"


def count_model_calls(log_text):
    return len(re.findall(r"MODEL ITERATION \d+", log_text))


def count_lines(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", errors="ignore") as handle:
        rows = [line for line in handle if line.strip()]
    if rows and rows[0].lower().startswith(("barcode", "cell")):
        rows = rows[1:]
    return len(rows)


def count_retained_cells(output_dir):
    candidates = []
    for path in Path(output_dir).rglob("*"):
        name = path.name.lower()
        if path.is_file() and "barcode" in name and path.suffix.lower() in {
            ".txt", ".tsv", ".csv", ".gz"
        }:
            candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda p: ("filtered" not in p.name.lower(), len(str(p))))
    return count_lines(candidates[0])


def count_input_cells():
    code = (
        "import anndata as ad; "
        f"print(ad.read_h5ad({INPUT_H5AD!r}, backed='r').n_obs)"
    )
    py = "/ssd/deecamp/cellnotes/micromamba/envs/snapatac2/bin/python"
    proc = subprocess.run(
        [py, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip().splitlines()[-1])


def parent():
    base = copy.deepcopy(agent.TASKS[0])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    metrics_dir = Path(base["output_root"]) / f"Li2023a_repeat3_metrics_{stamp}"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    input_cells = count_input_cells()
    rows = []
    for i in range(1, RUNS + 1):
        run_id = f"Li2023a_repeat{i}_{stamp}"
        log_path = metrics_dir / f"{run_id}.log"
        output_dir = Path(base["output_root"]) / run_id
        cmd = [sys.executable, __file__, "--one", run_id]
        if Path("/usr/bin/time").exists():
            cmd = ["/usr/bin/time", "-v"] + cmd

        started = time.monotonic()
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.run(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
        runtime_min = (time.monotonic() - started) / 60
        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
        retained_cells = count_retained_cells(output_dir)
        cell_retention = (
            "" if retained_cells is None
            else f"{retained_cells / input_cells:.4f}"
        )
        row = {
            "run_id": run_id,
            "returncode": proc.returncode,
            "End-to-end runtime, min": f"{runtime_min:.2f}",
            "Successful-launcher peak RSS, GB": (
                parse_peak_rss_gb(log_text) if proc.returncode == 0 else ""
            ),
            "Model calls": count_model_calls(log_text),
            "Cell retention": cell_retention,
            "output_dir": str(output_dir),
            "log": str(log_path),
        }
        rows.append(row)
        print(
            f"{run_id}\t"
            f"End-to-end runtime, min={row['End-to-end runtime, min']}\t"
            f"Successful-launcher peak RSS, GB={row['Successful-launcher peak RSS, GB']}\t"
            f"Model calls={row['Model calls']}\t"
            f"Cell retention={row['Cell retention']}",
            flush=True,
        )

    summary = metrics_dir / "summary.csv"
    with open(summary, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved summary: {summary}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--one":
        run_one(sys.argv[2])
    else:
        parent()
