import csv
import json
import os
import re
import subprocess
import sys
import tempfile

import numpy as np

SEED = 20261112
N_GENES = 2000
N_PER_GROUP = 12
N_UP = 50
N_DOWN = 50


def generate(out):
    rng = np.random.default_rng(SEED)
    samples = ["S%02d" % i for i in range(1, 2 * N_PER_GROUP + 1)]
    condition = ["healthy"] * N_PER_GROUP + ["disease"] * N_PER_GROUP
    genes = ["G%05d" % i for i in range(1, N_GENES + 1)]
    base = np.exp(rng.normal(5.0, 1.2, N_GENES))
    lfc = np.zeros(N_GENES)
    planted = rng.choice(N_GENES, N_UP + N_DOWN, replace=False)
    lfc[planted[:N_UP]] = rng.uniform(1.5, 3.0, N_UP)
    lfc[planted[N_UP:]] = -rng.uniform(1.5, 3.0, N_DOWN)
    size = rng.uniform(0.7, 1.4, len(samples))
    dispersion = 0.05
    counts = np.zeros((N_GENES, len(samples)), dtype=int)
    for j, cond in enumerate(condition):
        mu = base * size[j] * (2.0 ** lfc if cond == "disease" else 1.0)
        lam = rng.gamma(1.0 / dispersion, mu * dispersion)
        counts[:, j] = rng.poisson(lam)
    with open(os.path.join(out, "counts.tsv"), "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["gene_id"] + samples)
        for g, row in zip(genes, counts):
            w.writerow([g] + list(row))
    with open(os.path.join(out, "samples.tsv"), "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["sample_id", "condition", "age", "sex", "batch"])
        for s, c in zip(samples, condition):
            w.writerow([s, c, int(rng.integers(25, 75)), rng.choice(["F", "M"]), rng.choice(["b1", "b2"])])
    with open(os.path.join(out, "genes.tsv"), "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["gene_id", "symbol", "biotype"])
        for i, g in enumerate(genes):
            w.writerow([g, "SYM%d" % (i + 1), "protein_coding"])
    truth = {genes[i]: float(lfc[i]) for i in planted}
    return truth, dict(zip(samples, condition))


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def has_columns(rows, cols):
    return bool(rows) and all(c in rows[0] for c in cols)


def main(workspace):
    checks = {}
    tmp = tempfile.mkdtemp(prefix="genexp-check-")
    data, out = os.path.join(tmp, "data"), os.path.join(tmp, "out")
    os.makedirs(data)
    truth, condition = generate(data)
    python = os.path.join(workspace, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python):
        python = os.path.join(workspace, ".venv", "bin", "python")
    try:
        proc = subprocess.run([python, "-m", "genexp", "run", "--counts", os.path.join(data, "counts.tsv"),
                               "--samples", os.path.join(data, "samples.tsv"), "--genes",
                               os.path.join(data, "genes.tsv"), "--out", out], cwd=workspace, capture_output=True,
                              text=True, timeout=900)
        checks["cli_runs"] = proc.returncode == 0
        cli_tail = (proc.stdout + proc.stderr)[-1500:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        checks["cli_runs"] = False
        cli_tail = str(exc)

    report = os.path.join(out, "report.html")
    if os.path.exists(report):
        html = open(report, encoding="utf-8", errors="replace").read()
        external = re.search(r"""<(script|img|link)[^>]+(src|href)\s*=\s*["']https?://""", html, re.I)
        checks["report_self_contained"] = len(html) > 2000 and not external
    else:
        checks["report_self_contained"] = False

    de_path = os.path.join(out, "de_results.csv")
    de = read_csv(de_path) if os.path.exists(de_path) else []
    checks["de_columns"] = has_columns(de, ["gene_id", "log2fc", "pvalue", "padj"])
    if checks["de_columns"]:
        rows = {r["gene_id"]: r for r in de}
        try:
            pv = [float(r["pvalue"]) for r in de]
            pa = [float(r["padj"]) for r in de]
            checks["pvalues_valid"] = all(0 <= p <= 1 for p in pv + pa) and all(a >= p - 1e-12 for p, a in zip(pv, pa))
        except ValueError:
            checks["pvalues_valid"] = False
        called = {g for g, r in rows.items() if _float(r["padj"]) < 0.05}
        present = [g for g in truth if g in rows]
        recall = len(called & set(present)) / max(1, len(present))
        fdr = len(called - set(truth)) / max(1, len(called))
        checks["de_recall_at_least_0.7"] = recall >= 0.7
        checks["de_fdr_at_most_0.2"] = fdr <= 0.2
        hits = [g for g in called & set(truth)]
        same = sum(1 for g in hits if np.sign(_float(rows[g]["log2fc"])) == np.sign(truth[g]))
        checks["de_direction_disease_vs_healthy"] = bool(hits) and same / len(hits) >= 0.9
        metrics = {"recall": round(recall, 3), "fdr": round(fdr, 3), "called": len(called),
                   "planted_present": len(present)}
    else:
        checks.update({"pvalues_valid": False, "de_recall_at_least_0.7": False, "de_fdr_at_most_0.2": False,
                       "de_direction_disease_vs_healthy": False})
        metrics = {}

    bm_path = os.path.join(out, "biomarkers.csv")
    bm = read_csv(bm_path) if os.path.exists(bm_path) else []
    if has_columns(bm, ["gene_id", "rank"]):
        top = [r["gene_id"] for r in sorted(bm, key=lambda r: _float(r["rank"]))[:20]]
        metrics["biomarker_top20_planted"] = sum(1 for g in top if g in truth)
        checks["biomarkers_top20_has_12_planted"] = metrics["biomarker_top20_planted"] >= 12
    else:
        checks["biomarkers_top20_has_12_planted"] = False

    pca_path = os.path.join(out, "pca.csv")
    pca = read_csv(pca_path) if os.path.exists(pca_path) else []
    if has_columns(pca, ["sample_id", "PC1", "PC2"]):
        best = 0.0
        for pc in ("PC1", "PC2"):
            a = np.array([_float(r[pc]) for r in pca if condition.get(r["sample_id"]) == "disease"])
            b = np.array([_float(r[pc]) for r in pca if condition.get(r["sample_id"]) == "healthy"])
            if len(a) > 1 and len(b) > 1:
                pooled = np.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2) or 1e-9
                best = max(best, abs(a.mean() - b.mean()) / pooled)
        metrics["pca_separation"] = round(float(best), 2)
        checks["pca_separates_conditions"] = best >= 1.5
    else:
        checks["pca_separates_conditions"] = False

    checks = {k: bool(v) for k, v in checks.items()}
    passed = all(checks.values())
    print(json.dumps({"passed": passed, "checks": checks, "metrics": metrics,
                      "cli_tail": cli_tail if not checks["cli_runs"] else ""}, indent=1))
    return 0 if passed else 1


def _float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


if __name__ == "__main__":
    sys.exit(main(os.path.abspath(sys.argv[1])))
