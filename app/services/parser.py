import re
from itertools import pairwise
from typing import Any

from .normalizer import clean_text, normalize_answer

MARKER = re.compile(r"(?<![\w.\-(])(\d{1,3})\s*[.)\]:：]")
MAX_ANSWER_LEN = 24
_GLUE = re.compile(r"^(\d)([^\s.):\]]{1,2})\s+(?=\d{1,3}\s*[.)\]:：])")

HEADER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("day", re.compile(r"(?i)^\s*(?:day|days)\s*\.?\s*0*(\d{1,3})")),
    ("day", re.compile(r"^\s*0*(\d{1,3})\s*일차")),
    ("chapter", re.compile(r"(?i)^\s*chapter\s*\.?\s*0*(\d{1,3})")),
    ("chapter", re.compile(r"^\s*제?\s*0*(\d{1,3})\s*장")),
    ("unit", re.compile(r"(?i)^\s*unit\s*\.?\s*0*(\d{1,3})")),
    ("unit", re.compile(r"^\s*제?\s*0*(\d{1,3})\s*단원")),
    ("lesson", re.compile(r"(?i)^\s*lesson\s*\.?\s*0*(\d{1,3})")),
    ("lesson", re.compile(r"^\s*제?\s*0*(\d{1,3})\s*과\s*$")),
    ("step", re.compile(r"(?i)^\s*step\s*\.?\s*0*(\d{1,3})")),
]


def detect_header(line: str) -> tuple[str, str, int] | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 40:
        return None
    for htype, pattern in HEADER_PATTERNS:
        m = pattern.match(stripped)
        if m:
            return htype, stripped[:40], int(m.group(1))
    return None


def _scan_line(line: str) -> list[tuple[int, str]]:
    pairs = _scan_line_raw(line)
    m = _GLUE.match(line)
    if m:
        alt = _scan_line_raw(f"{m.group(1)}. {m.group(2)} {line[m.end():]}")
        if len(alt) > len(pairs):
            return alt
    return pairs


def _scan_line_raw(line: str) -> list[tuple[int, str]]:
    # Markers numbered 0 cannot be real question numbers ("0." typically comes
    # from a decimal answer such as "0.75"); exclude them from boundaries so
    # they stay part of the preceding answer text.
    matches = [m for m in MARKER.finditer(line) if int(m.group(1)) != 0]
    pairs = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        ans = line[m.end() : end].strip().strip(",;")
        num = int(m.group(1))
        if not ans or len(ans) > MAX_ANSWER_LEN:
            continue
        pairs.append((num, ans))
    return pairs


def _scoped_segments(
    entries: list[dict[str, Any]], headers: list[dict[str, Any]]
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Bucket entries into printed sections by header line boundaries."""
    ordered = sorted(headers, key=lambda h: h["line"])
    bounds = [(str(h["label"]), h["line"]) for h in ordered]
    segments: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for e in entries:
        label = "머리글 없음"
        for lbl, b in bounds:
            if e["line"] >= b:
                label = lbl
            else:
                break
        if label not in segments:
            segments[label] = []
            order.append(label)
        segments[label].append(e)
    return [(lbl, segments[lbl]) for lbl in order]


def detect_issues(
    entries: list[dict[str, Any]],
    headers: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Flag duplicate/gap problems WITHIN each printed section scope.

    Question numbers restarting at 1 in a new Day/Chapter are normal and must
    NOT be reported as duplicates; only collisions inside the same scope count.
    """
    issues: list[dict[str, str]] = []
    if headers:
        scoped = _scoped_segments(entries, headers)
    else:
        scoped = [("전체", entries)]

    for label, seg in scoped:
        counts: dict[int, int] = {}
        for e in seg:
            counts[e["number"]] = counts.get(e["number"], 0) + 1
        dupes = sorted(n for n, c in counts.items() if c > 1)
        if dupes:
            preview = ", ".join(str(n) for n in dupes[:8])
            prefix = f"[{label}] " if headers else ""
            issues.append(
                {
                    "kind": "duplicate",
                    "message": f"{prefix}중복 번호: {preview} — 나중 값이 우선합니다.",
                }
            )
        nums = sorted(counts)
        for a, b in pairwise(nums):
            if b - a >= 4:
                prefix = f"[{label}] " if headers else ""
                issues.append(
                    {
                        "kind": "gap",
                        "message": f"{prefix}번호 건너뜀 감지: {a} → {b} (누락 확인 필요)",
                    }
                )
    return issues


def parse_answer_key(raw_text: str) -> dict[str, Any]:
    text = clean_text(raw_text or "")
    entries: list[dict[str, Any]] = []
    headers: list[dict[str, Any]] = []
    skipped_markers = 0

    for li, line in enumerate(text.split("\n")):
        header = detect_header(line)
        if header:
            headers.append(
                {"type": header[0], "label": header[1], "index": header[2], "line": li}
            )
            continue
        for num, raw_ans in _scan_line(line):
            canonical = normalize_answer(raw_ans)
            if not canonical:
                skipped_markers += 1
                continue
            entries.append(
                {
                    "number": num,
                    "answer_display": raw_ans,
                    "answer": canonical,
                    "line": li,
                }
            )

    issues = detect_issues(entries)
    if skipped_markers:
        issues.append(
            {"kind": "noise", "message": f"객관식/숫자 형식이 아닌 항목 {skipped_markers}개 제외"}
        )

    return {
        "entries": entries,
        "headers": headers,
        "issues": issues,
        "raw_text": text,
    }
