import argparse
import json
import os
import re
import subprocess
import sys

SKIP = {".venv", "venv", "__pycache__", ".git", ".pytest_cache", "node_modules", "build", "dist"}


def walk(root):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.endswith(".egg-info")]
        for name in files:
            yield os.path.join(base, name)


def python_lines(root):
    total, files = 0, 0
    for path in walk(root):
        if path.endswith(".py"):
            files += 1
            with open(path, encoding="utf-8", errors="replace") as f:
                total += sum(1 for line in f if line.strip())
    return files, total


def venv_python(root):
    for candidate in (os.path.join(root, ".venv", "Scripts", "python.exe"), os.path.join(root, ".venv", "bin", "python")):
        if os.path.exists(candidate):
            return candidate
    return None


def run_tests(root, python):
    try:
        proc = subprocess.run([python, "-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider"], cwd=root,
                              capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"tests_error": str(exc)[:200]}
    lines = (proc.stdout or "").strip().splitlines()
    summary = next((l for l in reversed(lines) if re.search(r"\d+ (passed|failed)", l)), lines[-1] if lines else "")
    counts = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|error|errors|skipped)", summary)}
    return {"tests_summary": summary[:160], "tests_passed": counts.get("passed", 0),
            "tests_failed": counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0),
            "tests_exit_code": proc.returncode}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    files, loc = python_lines(args.workspace)
    all_files = sum(1 for _ in walk(args.workspace))
    data = {"files": all_files, "python_files": files, "python_loc": loc,
            "has_readme": os.path.exists(os.path.join(args.workspace, "README.md")),
            "has_requirements": os.path.exists(os.path.join(args.workspace, "requirements.txt"))}
    python = venv_python(args.workspace)
    data["has_venv"] = bool(python)
    if python:
        data.update(run_tests(args.workspace, python))
    html = [p for p in walk(args.workspace) if p.endswith(".html")]
    data["html_reports"] = len(html)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    print(json.dumps(data, indent=1))


if __name__ == "__main__":
    sys.exit(main())
