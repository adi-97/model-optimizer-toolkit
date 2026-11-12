import os
import sqlite3
import threading
import time
from datetime import datetime

COLUMNS = ("ts", "session", "role", "thread", "tier", "confidence", "source", "reason", "requested_model", "target_model",
           "sent_model", "effort", "applied", "shadow", "retried", "stream", "status", "input_tokens",
           "cache_write_tokens", "cache_write_1h_tokens", "cache_read_tokens", "output_tokens", "cost_usd", "counterfactual_usd",
           "latency_ms", "classify_ms", "prompt_hash", "prompt")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    ts REAL, session TEXT, role TEXT, thread TEXT, tier TEXT, confidence REAL, source TEXT, reason TEXT,
    requested_model TEXT, target_model TEXT, sent_model TEXT, effort TEXT,
    applied INTEGER, shadow INTEGER, retried INTEGER, stream INTEGER, status INTEGER,
    input_tokens INTEGER, cache_write_tokens INTEGER, cache_write_1h_tokens INTEGER, cache_read_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL, counterfactual_usd REAL, latency_ms REAL, classify_ms REAL,
    prompt_hash TEXT, prompt TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_session ON requests(session);
"""

GROUPS = {
    "tier": "COALESCE(tier, '-')",
    "model": "COALESCE(sent_model, '-')",
    "route": "COALESCE(requested_model, '-') || ' -> ' || COALESCE(sent_model, '-')",
    "source": "COALESCE(source, '-')",
    "role": "COALESCE(role, '-')",
    "session": "session",
    "day": "date(ts, 'unixepoch', 'localtime')",
}


def start_of_day(now=None):
    now = datetime.fromtimestamp(now or time.time())
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Tracker:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            self.conn.close()

    def record(self, **row):
        row.setdefault("ts", time.time())
        values = [row.get(c) for c in COLUMNS]
        with self.lock:
            self.conn.execute("INSERT INTO requests (%s) VALUES (%s)" % (", ".join(COLUMNS), ", ".join("?" * len(COLUMNS))),
                              values)
            self.conn.commit()

    def spend(self, since=None, session=None):
        sql, args = "SELECT COALESCE(SUM(cost_usd), 0) FROM requests WHERE 1=1", []
        if since is not None:
            sql, args = sql + " AND ts >= ?", args + [since]
        if session is not None:
            sql, args = sql + " AND session = ?", args + [session]
        with self.lock:
            return float(self.conn.execute(sql, args).fetchone()[0])

    def summary(self, by="tier", since=None):
        group = GROUPS[by]
        sql = ("SELECT %s AS grp, COUNT(*), SUM(applied), "
               "COALESCE(SUM(input_tokens + cache_write_tokens + cache_read_tokens), 0), "
               "COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(output_tokens), 0), "
               "COALESCE(SUM(cost_usd), 0), COALESCE(SUM(counterfactual_usd), 0) "
               "FROM requests WHERE ts >= ? GROUP BY grp ORDER BY 7 DESC") % group
        with self.lock:
            rows = self.conn.execute(sql, [since or 0]).fetchall()
        keys = ("group", "requests", "routed", "prompt_tokens", "cache_read_tokens", "output_tokens", "cost_usd",
                "counterfactual_usd")
        return [dict(zip(keys, r)) for r in rows]

    def recent(self, limit=20):
        with self.lock:
            cur = self.conn.execute("SELECT %s FROM requests ORDER BY id DESC LIMIT ?" % ", ".join(COLUMNS), [limit])
            return [dict(zip(COLUMNS, r)) for r in cur.fetchall()]
