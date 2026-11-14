"""Small supervised priority model trained only on confirmed fixed reports.

No code execution or automatic severity changes. A chronological holdout must
beat the majority baseline before suggestions are exposed. No external API.
"""
import math
import re
from collections import Counter, defaultdict


def tokens(report):
    text = f"{report.title or ''} {report.detail or ''} {report.page or ''}"[:2000]
    return Counter(re.findall(r"[a-z][a-z_]{2,30}", text.lower()))


class PriorityModel:
    def __init__(self, rows):
        self.classes = Counter(r.severity for r in rows)
        self.words = defaultdict(Counter)
        for row in rows:
            self.words[row.severity].update(tokens(row))
        self.vocab = set().union(*(set(v) for v in self.words.values()))

    def predict(self, row):
        words = tokens(row)
        scores = {}
        for label, count in self.classes.items():
            total = sum(self.words[label].values()) + len(self.vocab)
            scores[label] = math.log(count / sum(self.classes.values())) + sum(
                min(n, 3) * math.log((self.words[label][word] + 1) / max(total, 1))
                for word, n in words.items() if word in self.vocab)
        return max(scores, key=scores.get)


def learn(reports):
    fixed = sorted([r for r in reports if r.status == "fixed" and r.resolution
                    and r.severity in {"blocker", "high", "normal", "low"}], key=lambda r:r.id)
    result = dict(status="insufficient_verified_fixes", training_rows=len(fixed),
                  validation_rows=0, accuracy=None, baseline=None, suggestions=[])
    if len(fixed) < 20 or len({r.severity for r in fixed}) < 2:
        return result
    split = int(len(fixed) * .8)
    train, test = fixed[:split], fixed[split:]
    model = PriorityModel(train)
    accuracy = sum(model.predict(r) == r.severity for r in test) / len(test)
    majority = Counter(r.severity for r in train).most_common(1)[0][0]
    baseline = sum(r.severity == majority for r in test) / len(test)
    result.update(validation_rows=len(test), accuracy=accuracy, baseline=baseline,
                  status="validation_failed")
    if accuracy < .6 or accuracy <= baseline:
        return result
    model = PriorityModel(fixed)
    result.update(status="validated", suggestions=[dict(id=r.id,
        suggested_severity=model.predict(r)) for r in reports if r.status == "open"][:100])
    return result
