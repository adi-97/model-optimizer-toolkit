TIERS = ("simple", "mid", "complex")

QUESTION_ID = "tier"

QUESTIONS = {
    QUESTION_ID: {
        "type": "choice",
        "instructions": "How much work will a coding agent need to complete `task`?",
        "criteria": {
            "simple": "a direct answer or one small edit: explain, rename, typo, one-liner, run a command",
            "mid": "a contained change in a few files: fix a located bug, add a small feature, write tests",
            "complex": "multi-file or open-ended: refactor, design, unknown root cause, migration, performance",
        },
    }
}


def state_for(task: str, max_chars: int = 4000) -> dict:
    return {"task": task.strip()[:max_chars]}
