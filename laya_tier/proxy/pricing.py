DEFAULT_PRICES = {
    "claude-fable-5-2": {"input": 8.0, "output": 40.0, "cache_read": 0.20},
    "claude-mythos-5-2": {"input": 8.0, "output": 40.0, "cache_read": 0.20},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-mythos-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "long_threshold": 100000,
                         "long_input": 0.50, "long_output": 2.50},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}


def base_model(model):
    return (model or "").split("[")[0].strip().lower()


def matches(model, names):
    m = base_model(model)
    return any(m == n or m.startswith(n + "-") for n in names)


class Pricing:
    def __init__(self, overrides=None):
        self.table = dict(DEFAULT_PRICES)
        for name, price in (overrides or {}).items():
            self.table[name] = dict(self.table.get(name, {}), **price)

    def lookup(self, model):
        m = base_model(model)
        best = None
        for key in self.table:
            if (m == key or m.startswith(key + "-")) and (best is None or len(key) > len(best)):
                best = key
        return self.table.get(best) if best else None

    def rank(self, model):
        price = self.lookup(model)
        return price["input"] if price else None

    def cost(self, model, usage):
        price = self.lookup(model)
        if not price or not usage:
            return None
        fresh = int(usage.get("input_tokens") or 0)
        write = int(usage.get("cache_creation_input_tokens") or 0)
        read = int(usage.get("cache_read_input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        long = "long_threshold" in price and fresh + write + read > price["long_threshold"]
        p_in = price["long_input"] if long else price["input"]
        p_out = price["long_output"] if long else price["output"]
        p_read = p_in * 0.1 if long else price.get("cache_read", p_in * 0.1)
        split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        write_1h = int(split.get("ephemeral_1h_input_tokens") or 0)
        write_5m = write - write_1h if split else write
        p_write = price.get("cache_write", p_in * 1.25)
        p_write_1h = price.get("cache_write_1h", p_in * 2.0)
        return (fresh * p_in + write_5m * p_write + write_1h * p_write_1h + read * p_read + out * p_out) / 1e6
