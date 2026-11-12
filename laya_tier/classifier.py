import time

from .questions import QUESTION_ID, QUESTIONS, TIERS, state_for


class TierClassifier:
    def __init__(self, agent, min_confidence: float = 0.0):
        self.agent = agent
        self.min_confidence = min_confidence

    @classmethod
    def load(cls, model: str = "convaiinnovations/laya", device=None, min_confidence: float = 0.0, **kwargs):
        import laya

        return cls(laya.load(model, device=device, **kwargs), min_confidence=min_confidence)

    def _result(self, answer, elapsed_ms):
        probs = {t: float(answer["probabilities"].get(t, 0.0)) for t in TIERS}
        tier = max(probs, key=probs.get)
        confidence = probs[tier]
        escalated = False
        if confidence < self.min_confidence and tier != TIERS[-1]:
            tier = TIERS[TIERS.index(tier) + 1]
            escalated = True
        return {"tier": tier, "confidence": confidence, "probabilities": probs,
                "escalated": escalated, "latency_ms": elapsed_ms}

    def classify(self, task: str) -> dict:
        start = time.perf_counter()
        out = self.agent.predict(state_for(task), QUESTIONS)
        elapsed = (time.perf_counter() - start) * 1000
        return self._result(out["answers"][QUESTION_ID], elapsed)

    def classify_batch(self, tasks, batch_size: int = 16) -> list:
        start = time.perf_counter()
        outs = self.agent.predict_batch([state_for(t) for t in tasks], QUESTIONS, batch_size=batch_size)
        elapsed = (time.perf_counter() - start) * 1000 / max(1, len(tasks))
        return [self._result(o["answers"][QUESTION_ID], elapsed) for o in outs]
