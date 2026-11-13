import os
import re
import sys
import time

ERROR = re.compile(r"error|fail|exception|traceback|assert|panic|fatal|warning|^E\s|^\s*>\s|FAILED|passed|failed",
                   re.IGNORECASE)


def digest(text, log_dir, min_chars=8000, tail=60, max_errors=40):
    if len(text) <= min_chars:
        return text, None
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "%s-%d.log" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid())).replace("\\", "/")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    lines = text.splitlines()
    head = len(lines) - tail
    errors = [(i, line) for i, line in enumerate(lines[:max(head, 0)]) if ERROR.search(line)][:max_errors]
    out = ["%d: %s" % (i + 1, line[:400]) for i, line in errors]
    if out:
        out.insert(0, "[adtok] matching lines before the tail:")
    out.append("[adtok] last %d of %d lines:" % (min(tail, len(lines)), len(lines)))
    out += [line[:400] for line in lines[-tail:]]
    out.append("[adtok] %d lines omitted; full log at %s" % (max(head, 0) - len(errors), path))
    return "\n".join(out) + "\n", path


def main(log_dir, min_chars=8000, tail=60):
    text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    shown, _ = digest(text, log_dir, min_chars, tail)
    sys.stdout.write(shown)
    sys.stdout.flush()
    return 0
