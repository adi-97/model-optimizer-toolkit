import hashlib
import json
import re

REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def human_text(message):
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    return REMINDER.sub("", text_of(message.get("content"))).strip()


def last_human_turn(messages):
    for i in range(len(messages) - 1, -1, -1):
        text = human_text(messages[i])
        if text:
            return i, text
    return None, ""


def session_key(body, headers=None):
    session_id = (headers or {}).get("x-claude-code-session-id")
    if session_id:
        return "s:" + digest(session_id)
    user_id = (body.get("metadata") or {}).get("user_id")
    if user_id:
        return "u:" + digest(str(user_id))
    system = body.get("system")
    system_text = system if isinstance(system, str) else json.dumps(system, sort_keys=True)[:4000]
    messages = body.get("messages") or []
    first = next((human_text(m) for m in messages if human_text(m)), "")
    return "h:" + digest((system_text or "") + "\x00" + first[:4000])
