"""Pure, DB-free logic for the grading-session model.

Kept separate from app/db.py -- which already defines a `session()` context
manager (the sqlite transaction-scoped connection helper) unrelated to the
grading-session concept, see its own naming note -- and from the routers
themselves, so both functions below stay unit-testable with plain dicts and
lists, no database or HTTP client involved.
"""

from typing import Any


def merge_answers(
    latest_results: list[dict[str, Any]], new_answers: dict[str, str]
) -> dict[str, str]:
    """Overlay retry answers onto a session's latest submission.

    Previously given answers keep their original value (and thus their
    'correct'/'incorrect' status) unless the new payload explicitly touches
    that question number; only re-attempted numbers change. An explicit
    blank in `new_answers` retracts a previously given answer rather than
    leaving it untouched.

    Moved verbatim (identical overlay/retract semantics) from the old
    per-request `merge_attempt_id` handler in routers/attempts.py -- only
    the first parameter changed shape, from a whole base-attempt dict to
    just its `results` list, since the caller now always has "the session's
    latest submission's results" in hand rather than a client-supplied base
    attempt id.
    """
    merged: dict[str, str] = {}
    for r in latest_results:
        given = str(r.get("given") or "").strip()
        if given:
            merged[str(r["number"])] = given
    for num, val in new_answers.items():
        if str(val).strip():
            merged[str(num)] = str(val)
        else:
            merged.pop(str(num), None)  # explicit blank = retract answer
    return merged


def compute_breakdown(
    all_numbers: list[int], attempts: list[dict[str, Any]]
) -> dict[str, Any]:
    """1st/2nd/3rd+-try breakdown across every submission in a session.

    `all_numbers` is the section's FULL answer-key set -- not just the
    numbers the first submission happened to answer, since answered_only
    grading can narrow a submission's own results to a subset of the key.
    `attempts` is every submission in the session, each exposing its own
    per-question grading as `results: [{"number": ..., "status": ...}, ...]`
    (the shape dal.list_session_attempts already returns); scanned in
    submission_seq order (re-sorted here so callers don't have to guarantee
    it) to find, per question number, the submission_seq at which its
    status first became 'correct'.

    A number that's never correct in any submission -- including one never
    answered at all -- lands in third_plus, per spec: that bucket is
    literally "took 3+ tries, or never got there."
    """
    ordered = sorted(attempts, key=lambda a: a["submission_seq"])
    first_correct_seq: dict[int, int] = {}
    for att in ordered:
        seq = att["submission_seq"]
        for r in att.get("results", []):
            num = int(r["number"])
            if r["status"] == "correct" and num not in first_correct_seq:
                first_correct_seq[num] = seq

    first_try, second_try, third_plus = [], [], []
    for num in all_numbers:
        seq = first_correct_seq.get(num)
        if seq == 1:
            first_try.append(num)
        elif seq == 2:
            second_try.append(num)
        else:
            third_plus.append(num)

    total_questions = len(all_numbers)

    def _bucket(numbers: list[int]) -> dict[str, Any]:
        count = len(numbers)
        percent = round(count / total_questions * 100, 1) if total_questions else 0.0
        return {"numbers": sorted(numbers), "count": count, "percent": percent}

    return {
        "total_questions": total_questions,
        "first_try": _bucket(first_try),
        "second_try": _bucket(second_try),
        "third_plus": _bucket(third_plus),
    }
