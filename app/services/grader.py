from typing import Any

from .normalizer import answers_equal, canonical_type


def grade(
    keys_canonical: dict[int, str],
    keys_display: dict[int, str],
    answers: dict[str, str],
    answered_only: bool = False,
) -> dict[str, Any]:
    results = []
    wrong_numbers = []
    unanswered_numbers = []
    extra_inputs = []
    score = 0
    total = len(keys_canonical)

    given_raw_by_num = {}
    for k, v in answers.items():
        try:
            num = int(k)
        except (TypeError, ValueError):
            continue
        val = str(v)
        given_raw_by_num[num] = val
        if val.strip() and num not in keys_canonical:
            extra_inputs.append(num)

    for num in sorted(keys_canonical):
        expected_c = keys_canonical.get(num, "")
        expected_d = keys_display.get(num, "")
        qtype = canonical_type(expected_c) if expected_c else "multiple_choice"
        given_raw = (given_raw_by_num.get(num) or "").strip()
        if not given_raw:
            status = "unanswered"
            unanswered_numbers.append(num)
        elif answers_equal(expected_c, given_raw):
            status = "correct"
            score += 1
        else:
            status = "incorrect"
            wrong_numbers.append(num)
        results.append(
            {
                "number": num,
                "qtype": qtype,
                "expected": expected_d,
                "given": given_raw,
                "status": status,
            }
        )

    if answered_only:
        total -= len(unanswered_numbers)
    percent = round(score / total * 100, 1) if total else 0.0
    return {
        "results": results,
        "score": score,
        "total": total,
        "percent": percent,
        "wrong_numbers": wrong_numbers,
        "unanswered_numbers": unanswered_numbers,
        "extra_ignored": sorted(extra_inputs),
    }
