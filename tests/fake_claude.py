import json
import os
import sys
import urllib.request


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def main():
    prompt = arg("-p") or ""
    base = os.environ.get("ANTHROPIC_BASE_URL")
    print(json.dumps({"type": "system", "subtype": "init", "apiKeySource": "none"}), flush=True)
    reply = {"model": "claude-opus-5-5"}
    if base:
        body = {"model": "claude-opus-5-5", "max_tokens": 100, "metadata": {"user_id": "fake_session"},
                "system": [{"type": "text", "text": "fake agent " + (arg("--append-system-prompt") or "")}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(base + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "x-api-key": "fake"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            reply = json.loads(resp.read())
    settings = os.path.join(".claude", "settings.local.json")
    if os.path.exists(settings):
        with open("seen_settings.json", "w") as f:
            f.write(open(settings).read())
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo"}},
        {"type": "tool_use", "id": "t2", "name": "Agent", "input": {}}]}}), flush=True)
    if "FIX" in prompt:
        with open("done.txt", "w") as f:
            f.write("ok")
    if "TAMPER" in prompt:
        with open(os.path.join("tests", "guard.txt"), "a") as f:
            f.write("changed")
    usage = {"claude-opus-5-5": {"inputTokens": 1000, "outputTokens": 200, "cacheReadInputTokens": 5000,
                                 "cacheCreationInputTokens": 300, "costUSD": 0.0123},
             "claude-haiku-5-5": {"inputTokens": 400, "outputTokens": 50, "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 0, "costUSD": 0.0001}}
    print(json.dumps({"type": "result", "subtype": "success", "num_turns": 2, "is_error": False,
                      "total_cost_usd": 0.0124, "duration_ms": 10, "result": reply.get("model"),
                      "modelUsage": usage}), flush=True)


if __name__ == "__main__":
    main()
