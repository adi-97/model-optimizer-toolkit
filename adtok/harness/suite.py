import os
import sys
import tomllib
from dataclasses import dataclass, field

DEFAULT_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent", "Task", "TodoWrite"]


@dataclass
class Check:
    name: str
    run: list | None = None
    unchanged: str | None = None
    timeout_s: int = 900


@dataclass
class Task:
    id: str
    prompt: str
    dir: str
    kind: str = ""
    template: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    timeout_min: int = 60


@dataclass
class Arm:
    id: str
    proxy: dict = field(default_factory=dict)
    append_system_prompt: str | None = None
    model: str | None = None
    install: str | None = None
    layers: dict = field(default_factory=dict)
    route_traffic: bool = True


@dataclass
class Suite:
    name: str
    root: str
    tasks: list
    arms: list
    results_dir: str
    repeats: int = 1
    budget_usd: float = 50.0
    per_run_usd: float = 20.0
    baseline_arm: str | None = None
    keep_venv: bool = False
    claude: str | None = None
    python: str = sys.executable
    allowed_tools: list = field(default_factory=lambda: list(DEFAULT_TOOLS))
    allow_subscription_proxy: bool = False

    def task(self, task_id):
        return next(t for t in self.tasks if t.id == task_id)

    def arm(self, arm_id):
        return next(a for a in self.arms if a.id == arm_id)


def _path(root, value):
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(root, value))


def load_suite(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    root = os.path.dirname(os.path.abspath(path))
    tasks = []
    for raw in data.get("task", []):
        prompt_path = _path(root, raw["prompt"])
        with open(prompt_path, encoding="utf-8") as f:
            prompt = f.read()
        template = dict(raw.get("template") or {})
        if template.get("patch"):
            template["patch"] = _path(root, template["patch"])
        if template.get("copy"):
            template["copy"] = _path(root, template["copy"])
        checks = [Check(**c) for c in raw.get("check", [])]
        for c in checks:
            if not (c.run or c.unchanged):
                raise ValueError("check %r in task %r needs 'run' or 'unchanged'" % (c.name, raw["id"]))
        tasks.append(Task(id=raw["id"], prompt=prompt, dir=os.path.dirname(prompt_path), kind=raw.get("kind", ""),
                          template=template, checks=checks, timeout_min=int(raw.get("timeout_min", 60))))
    arms = [Arm(**a) for a in data.get("arm", [])]
    for arm in arms:
        if arm.install and arm.install not in ("native", "hybrid"):
            raise ValueError("arm %r: install must be 'native' or 'hybrid'" % arm.id)
    if not tasks or not arms:
        raise ValueError("a suite needs at least one [[task]] and one [[arm]]")
    ids = [t.id for t in tasks] + [a.id for a in arms]
    if len(set(ids)) != len(ids):
        raise ValueError("task and arm ids must be unique")
    suite = Suite(name=data.get("name", os.path.basename(root)), root=root, tasks=tasks, arms=arms,
                  results_dir=_path(root, data.get("results_dir", "results")),
                  repeats=int(data.get("repeats", 1)), budget_usd=float(data.get("budget_usd", 50.0)),
                  per_run_usd=float(data.get("per_run_usd", 20.0)), baseline_arm=data.get("baseline_arm"),
                  keep_venv=bool(data.get("keep_venv", False)), claude=data.get("claude"),
                  python=data.get("python") or sys.executable,
                  allowed_tools=list(data.get("allowed_tools") or DEFAULT_TOOLS),
                  allow_subscription_proxy=bool(data.get("allow_subscription_proxy", False)))
    if suite.baseline_arm and suite.baseline_arm not in [a.id for a in arms]:
        raise ValueError("baseline_arm %r is not an arm" % suite.baseline_arm)
    return suite
