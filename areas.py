"""Area values shared by imports and comparable-sale calculations, in m²."""
from __future__ import annotations

import math
import re


def square_metres(value) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().lower().replace(",", "")
    match = re.fullmatch(
        r"(?:approx\.?\s*|approximately\s*|~\s*)?"
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
        r"(m²|m2|m\^2|sqm|sq\.?\s*m|square met(?:re|er)s?|"
        r"ha|hectares?|acres?|sq\.?\s*ft|ft²|ft2|square feet)?"
        r"(?:\s*\(?approx\.?\)?)?", raw)
    if not match:
        return None
    number = float(match.group(1))
    unit = re.sub(r"[\s.]", "", match.group(2) or "")
    factor = (10000 if unit in {"ha", "hectare", "hectares"}
              else 4046.8564224 if unit in {"acre", "acres"}
              else 0.09290304 if unit in {"sqft", "ft²", "ft2", "squarefeet"}
              else 1)
    result = number * factor
    return result if math.isfinite(result) and result > 0 else None
