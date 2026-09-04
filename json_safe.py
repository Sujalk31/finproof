"""
JSON-safety utility.

pandas/numpy naturally produce NaN for missing merges — e.g. a transaction
with no matching settlement, bank credit, or tax record (exactly the rows
that end up EXCEPTION / HUMAN_REVIEW / AI_REVIEW, since a genuine gap in
the data is often *why* they're an exception in the first place).

Python's `json` module happily writes and reads bare `NaN` / `Infinity`
tokens — it's a non-standard extension it allows by default. But browsers'
`JSON.parse()` (which `fetch(...).json()` calls internally) treats those as
invalid JSON and throws a SyntaxError, which silently kills whatever
awaited it with no visible error to the user. This is why "Why?" did
nothing specifically on EXCEPTION/AI_REVIEW/HUMAN_REVIEW rows — those are
the rows most likely to carry a NaN somewhere in their evidence.

Every dict/list that originated from a pandas DataFrame (`.to_dict()`,
`row.to_dict()`, etc.) and might reach `json.dump()` or a FastAPI response
must be passed through `json_safe()` first.
"""

import math


def json_safe(obj):
    """Recursively replace NaN/Infinity with None so the result is valid,
    standards-compliant JSON that JavaScript's JSON.parse() can read."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj
