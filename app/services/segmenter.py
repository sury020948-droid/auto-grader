from itertools import pairwise
from typing import Any


def _group_by_headers(
    entries: list[dict[str, Any]], headers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ordered = sorted(headers, key=lambda h: h["line"])
    bounds = [h["line"] for h in ordered]
    groups: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    orphan = {"label": "머리글 없음", "type": "", "items": []}
    for e in entries:
        gid = -1
        for i, b in enumerate(bounds):
            if e["line"] >= b:
                gid = i
            else:
                break
        if gid < 0:
            orphan["items"].append(e)
        else:
            if gid not in groups:
                h = ordered[gid]
                groups[gid] = {
                    "label": h["label"],
                    "type": h["type"],
                    "items": [],
                }
                order.append(gid)
            groups[gid]["items"].append(e)
    result = []
    if orphan["items"]:
        result.append(orphan)
    result.extend(groups[i] for i in sorted(order))
    return result


def _chunk_numbers(numbers: list[int], size: int) -> list[dict[str, Any]]:
    groups = []
    for start in range(0, len(numbers), size):
        chunk = numbers[start : start + size]
        label = f"{chunk[0]}~{chunk[-1]}" if chunk[-1] != chunk[0] else str(chunk[0])
        groups.append({"label": label, "type": "range", "items": []})
    return groups


def recommend(parsed: dict[str, Any]) -> dict[str, Any]:
    entries = parsed["entries"]
    headers = parsed["headers"]
    numbers = sorted({e["number"] for e in entries})

    header_groups = (
        _group_by_headers(entries, headers)
        if headers
        else [{"label": "전체", "type": "", "items": []}]
    )
    covered = sum(len(g["items"]) for g in header_groups if g.get("type"))
    coverage = covered / len(entries) if entries else 0.0

    if len(header_groups) >= 1 and coverage >= 0.6:
        types = {g["type"] for g in header_groups}
        htype = next(t for t in ["day", "chapter", "unit", "lesson", "step"] if t in types)
        type_ko = {
            "day": "Day(일차)",
            "chapter": "Chapter(장)",
            "unit": "Unit(단원)",
            "lesson": "Lesson(과)",
            "step": "Step",
        }[htype]
        return {
            "structure": "headers",
            "header_type": htype,
            "groups": [
                {
                    "label": g["label"],
                    "numbers": [e["number"] for e in g["items"]],
                }
                for g in header_groups
            ],
            "chunk_size": None,
            "confidence": min(0.97, 0.65 + 0.04 * len(header_groups) + 0.1 * coverage),
            "rationale": f"{type_ko} 헤더가 {len(header_groups)}개 감지되어 이 구조를 추천합니다.",
            "alternatives": [
                {"structure": "chunks", "chunk_size": 10, "label": "10문제씩 나누기"},
                {"structure": "chunks", "chunk_size": 20, "label": "20문제씩 나누기"},
                {"structure": "chunks", "chunk_size": 0, "label": "하나로 묶기"},
            ],
        }

    span = numbers[-1] - numbers[0] + 1 if numbers else 0
    density = len(numbers) / span if span else 0
    big_gaps = [(a, b) for a, b in pairwise(numbers) if b - a >= 5]

    notes = []
    if big_gaps[:3]:
        notes.append("큰 번호 간격 존재")
    if density >= 0.9 and len(numbers) > 12:
        size = 10 if len(numbers) <= 100 else 25
        rationale = (
            f"연속 번호 {len(numbers)}개가 균일하게 배치되어 {size}문제 단위 분할을 추천합니다."
        )
        confidence = 0.62
    else:
        size = max(5, round(len(numbers) / 4 / 5) * 5 or 5)
        rationale = "헤더가 없어 문항 수 기준 자동 분할을 제안합니다."
        confidence = 0.45
    if notes:
        rationale += f" ({', '.join(notes)} — 저장 전 확인하세요)"

    alt_sizes = [s for s in [5, 10, 20, 25] if s != size]
    alternatives = [
        {"structure": "chunks", "chunk_size": s, "label": f"{s}문제씩 나누기"}
        for s in alt_sizes
    ]
    alternatives.append({"structure": "chunks", "chunk_size": 0, "label": "하나로 묶기"})
    if headers:
        alternatives.insert(
            0,
            {
                "structure": "headers",
                "header_type": headers[0]["type"],
                "label": f"감지된 헤더({len(headers)}개) 기준 묶기",
            },
        )

    return {
        "structure": "chunks",
        "header_type": None,
        "groups": [],
        "chunk_size": size,
        "confidence": confidence,
        "rationale": rationale,
        "alternatives": alternatives,
    }


def _resolve(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for e in items:
        seen[int(e["number"])] = e
    return [seen[n] for n in sorted(seen)]


def build_groups(
    entries: list[dict[str, int | str]],
    structure: str,
    headers: list[dict[str, Any]] | None,
    chunk_size: int | None,
) -> list[dict[str, Any]]:
    rows = [dict(e) for e in entries]

    if structure == "headers":
        hs = headers or []
        bounds = [h.get("line", 0) for h in hs]
        buckets: list[list[dict[str, Any]]] = [[] for _ in hs]
        orphan: list[dict[str, Any]] = []
        for e in rows:
            gid = -1
            for i, b in enumerate(bounds):
                if int(e.get("line", 0)) >= b:
                    gid = i
                else:
                    break
            if gid < 0:
                orphan.append(e)
            else:
                buckets[gid].append(e)
        out = []
        if orphan:
            out.append({"label": "머리글 없음", "items": _resolve(orphan)})
        for h, items in zip(hs, buckets, strict=False):
            if items:
                out.append({"label": str(h["label"]), "items": _resolve(items)})
        if not out:
            out = [{"label": "전체", "items": _resolve(rows)}]
        return out

    resolved = _resolve(rows)
    numbers = [int(e["number"]) for e in resolved]
    size = chunk_size or 0
    if size <= 0:
        label = f"{numbers[0]}~{numbers[-1]}" if len(numbers) > 1 else str(numbers[0])
        return [{"label": label, "items": resolved}]
    out = []
    for start in range(0, len(resolved), size):
        chunk = resolved[start : start + size]
        lo = int(chunk[0]["number"])
        hi = int(chunk[-1]["number"])
        out.append({"label": f"{lo}~{hi}", "items": chunk})
    return out
