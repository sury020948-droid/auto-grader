"""Duplicate-session detection between existing sections and incoming groups.

Question numbers restart at 1 in every Day/Chapter of a workbook, so number
ranges alone can never prove duplication. Conflicts therefore trigger on
*label identity*: normalized equality (case/space-insensitive) or containment
(e.g. "Day 01" vs "Day 01 - 수능"), combined with an actual overlap of the
question-number ranges. Distinct days never collide.
"""

import re
from typing import Any

_NORM_RE = re.compile(r"[\s\-_.·]+")


def normalize_label(label: str) -> str:
    return _NORM_RE.sub("", str(label or "")).casefold()


def labels_related(a: str, b: str) -> bool:
    na, nb = normalize_label(a), normalize_label(b)
    if not na or not nb:
        return False
    return na == nb or na.startswith(nb) or nb.startswith(na)


def _overlap(a: list[int], b: list[int]) -> list[int]:
    sa, sb = set(a), set(b)
    return sorted(sa & sb)


def detect_conflicts(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare incoming groups against existing sections.

    existing: [{id, label, numbers}] · incoming: [{label, numbers}]
    Returns one entry per (incoming group, existing section) collision pair.
    """
    conflicts: list[dict[str, Any]] = []
    for inc in incoming:
        for ex in existing:
            if not labels_related(inc["label"], ex["label"]):
                continue
            inter = _overlap(inc["numbers"], ex["numbers"])
            same = normalize_label(inc["label"]) == normalize_label(ex["label"])
            if not inter and not same:
                continue
            conflicts.append(
                {
                    "incoming_label": inc["label"],
                    "incoming_numbers": sorted(inc["numbers"]),
                    "existing_section": {
                        "id": ex["id"],
                        "label": ex["label"],
                        "numbers": sorted(ex["numbers"]),
                    },
                    "overlapping_numbers": inter,
                    "same_label": same,
                }
            )
    return conflicts
