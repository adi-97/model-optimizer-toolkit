import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from .extract import digest, human_text, last_human_turn, session_key
from .pricing import matches

FORCED_TOOL_REJECTED = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5-2",
                        "claude-mythos-5-1", "claude-mythos-5-2")
FAST_MODE_OK = ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-fable-5-2", "claude-mythos-5-2")
NO_DISABLED_THINKING = ("claude-opus-5-5", "claude-fable-5", "claude-mythos-5")
CONTEXT_EDITING_BETA = "context-management-2025-06-27"


@dataclass
class Decision:
    session: str
    requested_model: str | None = None
    target_model: str | None = None
    tier: str | None = None
    confidence: float | None = None
    source: str = "none"
    role: str = "main"
    thread: str = ""
    effort: str | None = None
    reason: str = ""
    prompt_hash: str | None = None
    prompt: str | None = None
    classify_ms: float = 0.0
    context_editing: bool = False

    @property
    def switches(self):
        return bool(self.target_model) and self.target_model != self.requested_model


def system_text(body):
    system = body.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b.get("text") or "" for b in system if isinstance(b, dict))
    return ""


def signature(body):
    tools = sorted(str(t.get("name") or t.get("type")) for t in body.get("tools") or [] if isinstance(t, dict))
    return digest(system_text(body) + "\x00" + json.dumps(tools))


def first_human_text(messages):
    return next((t for t in (human_text(m) for m in messages) if t), "")


class Router:
    def __init__(self, config, classifier, pricing, max_sessions=2000, max_threads=256, max_cache=8192):
        self.config = config
        self.classifier = classifier
        self.pricing = pricing
        self.max_sessions = max_sessions
        self.max_threads = max_threads
        self.max_cache = max_cache
        self._lock = threading.Lock()
        self._model_lock = threading.Lock()
        self._sessions = OrderedDict()
        self._cache = OrderedDict()

    def _remember(self, store, key, value, limit):
        store[key] = value
        store.move_to_end(key)
        while len(store) > limit:
            store.popitem(last=False)

    def classify(self, text):
        key = digest(text)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit, 0.0
        start = time.perf_counter()
        with self._model_lock:
            result = self.classifier.classify(text)
        elapsed = (time.perf_counter() - start) * 1000
        with self._lock:
            self._remember(self._cache, key, result, self.max_cache)
        return result, elapsed

    def _session(self, key):
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = {"main_sig": None, "main_sigs": set(), "main_first": None, "threads": OrderedDict()}
            self._remember(self._sessions, key, session, self.max_sessions)
            return session

    def _thread(self, body, decision, session):
        sig = signature(body)
        messages = body.get("messages") or []
        main_candidate = bool(body.get("tools")) and not matches(decision.requested_model,
                                                                 self.config.never_upgrade_from)
        first = digest(first_human_text(messages))
        with self._lock:
            if session["main_sig"] is None and main_candidate:
                session["main_sig"] = sig
                session["main_first"] = first
                session["main_sigs"].add(sig)
            if sig in session["main_sigs"]:
                return "main", "main"
            if main_candidate and first == session["main_first"]:
                session["main_sigs"].add(sig)
                return "main", "main"
        return "branch", sig + ":" + first

    def decide(self, body, force_tier=None, headers=None):
        headers = headers or {}
        requested = body.get("model")
        decision = Decision(session=session_key(body, headers), requested_model=requested, target_model=requested)
        session = self._session(decision.session)
        messages = body.get("messages") or []
        request_class = headers.get("x-claude-code-request-class")
        if request_class:
            if self._decide_from_hints(body, decision, messages, headers, request_class):
                return decision
        else:
            decision.role, decision.thread = self._thread(body, decision, session)
        if decision.role == "branch" and self._is_utility(body):
            decision.role, decision.tier, decision.source = "utility", self.config.utility_tier, "utility"
            self._route(body, decision)
            return decision
        self._decide_locked(body, decision, session, messages, force_tier)
        return decision

    def _decide_from_hints(self, body, decision, messages, headers, request_class):
        agent_type = headers.get("x-claude-code-agent-type") or ""
        agent_id = headers.get("x-claude-code-agent-id") or ""
        if request_class == "main":
            decision.role, decision.thread = "main", "main"
            return False
        if request_class == "auxiliary":
            decision.role, decision.thread = "utility", "auxiliary"
            if self.config.utility_tier:
                decision.tier, decision.source = self.config.utility_tier, "auxiliary"
                self._route(body, decision)
            else:
                decision.source, decision.reason = "auxiliary", "utility_routing_off"
            return True
        if request_class == "compaction" or agent_type == "fork":
            decision.role, decision.thread = request_class if request_class == "compaction" else "fork", agent_id
            decision.source, decision.reason = decision.role, "keeps_parent_model"
            return True
        decision.role, decision.thread = "branch", "agent:" + (agent_id or digest(first_human_text(messages)))
        if self.config.respect_native_subagents:
            decision.source, decision.reason = "native_subagent", "native_model_respected"
            decision.tier = None
            return True
        return False

    def _is_utility(self, body):
        if not self.config.utility_tier or body.get("tools"):
            return False
        size = len(system_text(body)) + len(json.dumps(body.get("messages") or [], ensure_ascii=False))
        return size <= self.config.utility_max_chars

    def _decide_locked(self, body, decision, session, messages, force_tier):
        with self._lock:
            lock = session["threads"].get(decision.thread)
        if lock and lock["requested"] != decision.requested_model:
            lock = {"requested": decision.requested_model, "tier": None, "confidence": None,
                    "target": decision.requested_model, "effort": None, "source": "manual_switch", "reason": ""}
            with self._lock:
                self._remember(session["threads"], decision.thread, lock, self.max_threads)
        if lock:
            decision.tier, decision.confidence = lock["tier"], lock["confidence"]
            decision.target_model, decision.effort = lock["target"], lock["effort"]
            decision.source = "locked" if lock["source"] != "manual_switch" else "manual_switch"
            decision.reason = lock["reason"]
        else:
            text = last_human_turn(messages)[1] if decision.role == "main" else first_human_text(messages)
            if not text:
                decision.reason = "no_prompt"
                return
            if decision.role == "main" and len(text.split()) < self.config.lock_min_words:
                decision.source, decision.reason = "pre_lock", "prompt_too_short_to_lock"
                return
            self._classify_into(decision, text)
            if decision.tier:
                self._route(body, decision)
            with self._lock:
                self._remember(session["threads"], decision.thread,
                               {"requested": decision.requested_model, "tier": decision.tier,
                                "confidence": decision.confidence, "target": decision.target_model,
                                "effort": decision.effort, "source": decision.source, "reason": decision.reason},
                               self.max_threads)
        if force_tier and decision.tier != force_tier:
            decision.tier, decision.source = force_tier, decision.source + "+budget"
            self._route(body, decision)
        decision.context_editing = (decision.tier in self.config.context_editing_tiers
                                    and "context_management" not in body)

    def _classify_into(self, decision, text):
        decision.prompt_hash = digest(text)
        if self.config.store_prompts:
            decision.prompt = text[:300]
        if not self.config.classifier:
            decision.tier, decision.source = None, "classifier_off"
            decision.target_model, decision.reason = decision.requested_model, "classifier_off"
            return
        result, decision.classify_ms = self.classify(text)
        decision.tier, decision.confidence = result["tier"], result["confidence"]
        decision.source = "classifier" if decision.classify_ms else "cache"

    def _route(self, body, decision):
        requested = decision.requested_model
        route = self.config.routes[decision.tier]
        target = route.model or requested
        decision.reason = ""
        if decision.role == "main" and not self.config.main_routing:
            decision.target_model, decision.effort, decision.reason = requested, None, "main_routing_off"
            return
        if target != requested:
            blocked = self.blocker(body, requested, target)
            if blocked:
                decision.reason = blocked
                target = requested
        decision.target_model = target
        decision.effort = route.effort if target != requested else None

    def blocker(self, body, requested, target):
        if self.pricing.rank(requested) is None:
            return "unknown_requested_model"
        if self.pricing.rank(target) is None:
            return "unknown_target_model"
        if matches(requested, self.config.never_upgrade_from) and self.pricing.rank(target) > self.pricing.rank(requested):
            return "never_upgrade"
        choice = body.get("tool_choice") or {}
        if isinstance(choice, dict) and choice.get("type") in ("any", "tool") and matches(target, FORCED_TOOL_REJECTED):
            return "forced_tool_choice"
        if body.get("speed") and not matches(target, FAST_MODE_OK):
            return "fast_mode"
        if "fallbacks" in body:
            return "fallbacks"
        return ""


def adapt_thinking(body, target, effort):
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        return
    kind = thinking.get("type")
    off_allowed = effort in (None, "low", "medium", "high")
    if kind == "enabled":
        body["thinking"] = {"type": "adaptive", **({"display": thinking["display"]} if "display" in thinking else {})}
    elif kind in ("disabled", "between_tools"):
        if matches(target, NO_DISABLED_THINKING) or not off_allowed:
            body.pop("thinking")
        elif matches(target, ("claude-sonnet-5-5",)):
            body["thinking"] = {"type": "between_tools"}
        else:
            body["thinking"] = {"type": "disabled"}


def rewrite(body, decision, shadow=False):
    if shadow:
        return body, False
    out = dict(body)
    changed = False
    if decision.switches:
        out["model"] = decision.target_model
        if decision.effort:
            out["output_config"] = dict(out.get("output_config") or {}, effort=decision.effort)
        adapt_thinking(out, decision.target_model, (out.get("output_config") or {}).get("effort"))
        changed = True
    if decision.context_editing:
        out["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
        changed = True
    return out, changed
