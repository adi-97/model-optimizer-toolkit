import hashlib
import io
import os
import shutil
import subprocess
import tarfile

GIT_IDENTITY = ["-c", "user.name=token-bench", "-c", "user.email=token-bench@localhost", "-c", "core.autocrlf=false"]


def git(args, cwd, check=True):
    return subprocess.run(["git"] + GIT_IDENTITY + args, cwd=cwd, capture_output=True, check=check)


def prepare(task, dest, before_commit=None):
    os.makedirs(dest)
    template = task.template
    if template.get("git"):
        archive = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", template["git"], "archive", "--format=tar",
                                  template.get("ref", "HEAD")], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(dest, filter="data")
    elif template.get("copy"):
        shutil.copytree(template["copy"], dest, dirs_exist_ok=True)
    git(["init", "-q"], dest)
    if template.get("patch"):
        git(["apply", "--whitespace=nowarn", template["patch"]], dest)
    if before_commit:
        before_commit(dest)
    git(["add", "-A"], dest)
    git(["commit", "-q", "--allow-empty", "-m", "initial state"], dest)
    return {c.unchanged: snapshot(dest, c.unchanged) for c in task.checks if c.unchanged}


def snapshot(root, rel):
    base = os.path.join(root, rel)
    digests = {}
    for folder, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                digests[os.path.relpath(path, root).replace("\\", "/")] = hashlib.sha256(f.read()).hexdigest()
    return digests


def changed_files(root, rel, before):
    after = snapshot(root, rel)
    return sorted(set(before) ^ set(after) | {k for k in before if k in after and before[k] != after[k]})


def prune(dest):
    for name in (".venv", "venv", "node_modules"):
        shutil.rmtree(os.path.join(dest, name), ignore_errors=True)
