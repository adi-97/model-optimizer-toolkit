import os
import tomllib
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TIERS = ("simple", "mid", "complex")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BUDGET_ACTIONS = ("warn", "downgrade", "block")


@dataclass
class Route:
    model: str | None = None
    effort: str | None = None


@dataclass
class Budget:
    daily_usd: float | None = None
    session_usd: float | None = None
    action: str = "warn"


def default_routes():
    return {
        "simple": Route("claude-haiku-5-5", "low"),
        "mid": Route("claude-sonnet-5-5", "medium"),
        "complex": Route("claude-opus-5-5", None),
    }


@dataclass
class Config:
    upstream: str = "https://api.anthropic.com"
    host: str = "127.0.0.1"
    port: int = 8787
    classifier_model: str = "models/tier-full-v1"
    classifier_device: str | None = None
    min_confidence: float = 0.6
    shadow: bool = False
    never_upgrade_from: list = field(default_factory=lambda: ["claude-haiku-4-5", "claude-haiku-5-5"])
    utility_tier: str | None = "simple"
    utility_max_chars: int = 20000
    session_lock: bool = True
    lock_min_words: int = 5
    follow_up_max_words: int = 4
    retry_original_on_error: bool = True
    context_editing_tiers: list = field(default_factory=list)
    routes: dict = field(default_factory=default_routes)
    budget: Budget = field(default_factory=Budget)
    prices: dict = field(default_factory=dict)
    db_path: str = "~/.laya-tier/usage.sqlite"
    store_prompts: bool = False

    def validate(self):
        if set(self.routes) != set(TIERS):
            raise ValueError("routes must define exactly %s, got %s" % (TIERS, sorted(self.routes)))
        for tier, route in self.routes.items():
            if route.effort is not None and route.effort not in EFFORTS:
                raise ValueError("routes.%s.effort must be one of %s" % (tier, EFFORTS))
        if self.budget.action not in BUDGET_ACTIONS:
            raise ValueError("budget.action must be one of %s" % (BUDGET_ACTIONS,))
        if self.utility_tier and self.utility_tier not in TIERS:
            raise ValueError("utility_tier must be one of %s or empty" % (TIERS,))
        unknown = set(self.context_editing_tiers) - set(TIERS)
        if unknown:
            raise ValueError("context_editing_tiers has unknown tiers %s" % sorted(unknown))
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")


def load_config(path=None, **overrides):
    data = {}
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    data.update({k: v for k, v in overrides.items() if v is not None})
    cfg = Config()
    for key, value in data.items():
        if key == "routes":
            routes = default_routes()
            for tier, route in value.items():
                routes[tier] = Route(**{k: (v or None) for k, v in route.items()})
            cfg.routes = routes
        elif key == "budget":
            cfg.budget = Budget(**value)
        elif hasattr(cfg, key):
            setattr(cfg, key, value)
        else:
            raise ValueError("unknown config key %r" % key)
    cfg.db_path = os.path.expanduser(cfg.db_path)
    if not os.path.isabs(cfg.classifier_model) and not os.path.exists(cfg.classifier_model):
        candidate = os.path.join(ROOT, cfg.classifier_model)
        if os.path.exists(candidate):
            cfg.classifier_model = candidate
    cfg.validate()
    return cfg
