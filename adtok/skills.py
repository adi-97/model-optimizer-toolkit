import json
import os
import re
import time

from .ledger import iter_records, parse_ts, session_files

MAX_DESC = 1536
MAX_BODY_LINES = 500
REINJECT_TOKENS = 5000
LISTING_FALLBACK_CHARS = 8000
MODES = ("name-only", "user-invocable-only", "off")
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
COMMAND = re.compile(r"<command-name>/?([^<\s]+)</command-name>")


def tokens(text):
    return (len(text) + 3) // 4


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def frontmatter(text):
    match = FRONT.match(text or "")
    out, key = {}, None
    for line in (match.group(1) if match else "").splitlines():
        if line[:1] in (" ", "\t"):
            if key and line.strip():
                out[key] = (out[key] + " " + line.strip()).strip()
            continue
        if ":" not in line:
            key = None
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        out[key] = "" if value in (">", "|", ">-", "|-", ">+", "|+") else value.strip("\"'")
    return out


def body_of(text):
    match = FRONT.match(text or "")
    return (text or "")[match.end():] if match else (text or "")


def discover(bases):
    found, seen = [], set()
    for scope, base in bases:
        for kind in ("skills", "commands"):
            folder = os.path.join(base, kind)
            for root, _, files in os.walk(folder) if os.path.isdir(folder) else []:
                for name in sorted(files):
                    path = os.path.normpath(os.path.join(root, name))
                    if (not name.endswith(".md") or (kind == "skills" and name != "SKILL.md")
                            or os.path.normcase(path) in seen):
                        continue
                    seen.add(os.path.normcase(path))
                    text = read(path) or ""
                    fm = frontmatter(text)
                    body = body_of(text)
                    default = os.path.basename(root) if kind == "skills" else name[:-3]
                    synced = "synced" in os.path.relpath(root, folder).split(os.sep)
                    found.append({"name": fm.get("name") or default, "scope": "synced" if synced else scope,
                                  "kind": kind[:-1],
                                  "path": path, "desc_chars": len(fm.get("description", "") + fm.get("when_to_use", "")),
                                  "model": fm.get("model", ""),
                                  "model_invocable": fm.get("disable-model-invocation", "").lower() != "true",
                                  "body_lines": body.count("\n"), "body_tokens": tokens(body),
                                  "supporting_files": len(files) - 1 if kind == "skills" else 0})
    return found


def overrides_in(paths):
    out = {}
    for path in paths:
        text = read(path)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        if isinstance(data.get("skillOverrides"), dict):
            out.update(data["skillOverrides"])
    return out


def listed_chars(skill, overrides):
    mode = overrides.get(skill["name"], "on")
    if mode in ("off", "user-invocable-only") or not skill["model_invocable"]:
        return 0
    if mode == "name-only":
        return len(skill["name"])
    return len(skill["name"]) + min(skill["desc_chars"], MAX_DESC)


def usage(project=None, root=None):
    counts, last, seen, first = {}, {}, set(), None
    for path in session_files(project, root):
        for r in iter_records(path):
            ts = parse_ts(r.get("timestamp")) if r.get("timestamp") else None
            if ts:
                first = ts if first is None else min(first, ts)
            content = (r.get("message") or {}).get("content")
            names = []
            if r.get("type") == "assistant" and isinstance(content, list):
                for c in content:
                    if (isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Skill"
                            and c.get("id") not in seen):
                        seen.add(c.get("id"))
                        names.append(str((c.get("input") or {}).get("skill") or ""))
            elif r.get("type") == "user":
                if isinstance(content, list):
                    content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
                names = COMMAND.findall(content) if isinstance(content, str) else []
            for name in filter(None, names):
                name = name.split(":")[-1]
                counts[name] = counts.get(name, 0) + 1
                last[name] = max(last.get(name, 0), ts or 0)
    return {"counts": counts, "last": last, "first": first}


def report(bases, settings_paths, project=None, root=None, days=30, now=None):
    now = now or time.time()
    skills = discover(bases)
    overrides = overrides_in(settings_paths)
    used = usage(project, root)
    span_days = (now - used["first"]) / 86400 if used["first"] else 0
    cutoff = now - days * 86400
    for s in skills:
        s["override"] = overrides.get(s["name"])
        s["listed_chars"] = listed_chars(s, overrides)
        s["uses"] = used["counts"].get(s["name"], 0)
        s["last_used"] = used["last"].get(s["name"]) or None
        s["prune"] = bool(span_days >= days and s["scope"] != "synced" and s["listed_chars"] > len(s["name"]) and not s["override"]
                          and (s["last_used"] or 0) < cutoff)
    return {"skills": skills, "listing_chars": sum(s["listed_chars"] for s in skills),
            "history_days": round(span_days, 1), "days": days, "findings": findings(skills)}


def findings(skills):
    out = []
    listing = 0
    for s in skills:
        listing += s.get("listed_chars", len(s["name"]) + min(s["desc_chars"], MAX_DESC) if s["model_invocable"] else 0)
        if s["desc_chars"] > MAX_DESC:
            out.append(("warn", "prefix", "%s description is %d characters (cap %d; the rest is cut from the listing)"
                        % (s["path"], s["desc_chars"], MAX_DESC)))
        if s["model"] and s["model"] != "inherit":
            out.append(("warn", "cache", "%s sets model: %s; invoking it switches model and rebuilds the cache"
                        % (s["path"], s["model"])))
        if s["kind"] == "skill" and s["body_lines"] > MAX_BODY_LINES:
            out.append(("warn", "skills", "%s has %d lines (keep SKILL.md under %d; move reference material into "
                        "files it links to, which load only when read)" % (s["path"], s["body_lines"],
                                                                             MAX_BODY_LINES)))
        elif s["body_tokens"] > REINJECT_TOKENS:
            out.append(("info", "skills", "%s body is about %d tokens; after compaction only the first %d are "
                        "re-injected" % (s["path"], s["body_tokens"], REINJECT_TOKENS)))
    if listing > LISTING_FALLBACK_CHARS:
        out.append(("info", "skills", "skill listing is about %d characters; past the listing budget (1%% of the "
                    "context window, 8,000-character fallback) the least-used skills lose their descriptions. "
                    "Run adtok skills to see which are unused" % listing))
    return [{"level": a, "layer": b, "message": c} for a, b, c in out]


def prune_plan(target, user, days, mode, root=None):
    base = os.path.join(target, ".claude")
    settings = [os.path.join(base, "settings.json")]
    if not user:
        home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        settings = [os.path.join(home, "settings.json"), os.path.join(base, "settings.json"),
                    os.path.join(base, "settings.local.json")]
    result = report([("user" if user else "project", base)], settings, None if user else target, root, days)
    return {s["name"]: mode for s in result["skills"] if s["prune"]}
