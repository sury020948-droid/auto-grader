"""Tests for the "grade only answered questions" opt-in mode.

Covers gaps not already exercised by the grader/API tests added alongside the
feature itself: whitespace-only answers, extra/out-of-range inputs, the note
formatting when multiple reasons combine, the answered_only x merge-retry
interaction, and that the full per-question results list is preserved even
when total/percent shrink.
"""

import pytest

from app.services.grader import grade

DAY_SAMPLE = "Day 01\n1. 3 2. 4 3. 1 4. 5 5. 2\nDay 02\n1. 2 2. 3 3. 4 4. 1 5. 5"


def _keys(spec):
    canonical, display = {}, {}
    for num, (c, d) in spec.items():
        canonical[num] = c
        display[num] = d
    return canonical, display


@pytest.fixture()
def wb(client):
    r = client.post("/api/workbooks", json={"title": "선택 채점 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


def _import_headers(client, wid, sample=DAY_SAMPLE):
    preview = client.post("/api/extract-text", json={"raw_text": sample}).json()
    entries = [
        {"number": e["number"], "answer": e["answer"], "line": e.get("line", 0)}
        for e in preview["entries"]
    ]
    headers = preview["headers"]
    return client.post(
        f"/api/workbooks/{wid}/sections/import",
        json={
            "structure": "headers",
            "header_type": "day",
            "entries": entries,
            "headers": headers,
        },
    )


class TestGraderAnsweredOnlyEdgeCases:
    """Unit-level edge cases in app/services/grader.py not covered elsewhere."""

    def test_whitespace_only_answer_is_excluded_like_blank(self):
        kc, kd = _keys({1: ("3", "3"), 2: ("1", "1")})
        out = grade(kc, kd, {"1": "3", "2": "   "}, answered_only=True)
        assert out["total"] == 1
        assert out["score"] == 1
        assert out["percent"] == 100.0
        assert out["unanswered_numbers"] == [2]

    def test_extra_inputs_do_not_affect_total(self):
        """Inputs for numbers outside the key set are ignored either way."""
        kc, kd = _keys({1: ("1", "1")})
        out = grade(kc, kd, {"1": "1", "99": "5"}, answered_only=True)
        assert out["total"] == 1
        assert out["extra_ignored"] == [99]

    def test_explicit_false_matches_omitted_default(self):
        kc, kd = _keys({1: ("3", "3"), 2: ("1", "1")})
        omitted = grade(kc, kd, {"1": "3"})
        explicit_false = grade(kc, kd, {"1": "3"}, answered_only=False)
        assert omitted == explicit_false

    def test_score_and_wrong_numbers_identical_regardless_of_mode(self):
        """Core requirement: unanswered must never count as wrong. score/
        wrong_numbers (the numerator side) must be identical in both modes;
        only total (the denominator) should move."""
        kc, kd = _keys({1: ("3", "3"), 2: ("1", "1"), 3: ("4", "4")})
        answers = {"1": "3", "2": "9"}  # one correct, one wrong, one skipped
        out_default = grade(kc, kd, answers)
        out_answered_only = grade(kc, kd, answers, answered_only=True)
        assert out_default["score"] == out_answered_only["score"] == 1
        assert out_default["wrong_numbers"] == out_answered_only["wrong_numbers"] == [2]
        assert out_default["total"] == 3
        assert out_answered_only["total"] == 2


class TestApiAnsweredOnly:
    """API-level behavior of POST /api/attempts with answered_only."""

    def test_results_still_lists_every_question_when_total_is_shrunk(self, client, wb):
        """The UI needs the full per-question list even though total/percent
        only reflect the answered subset."""
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        att = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"1": "3"}, "answered_only": True},
        ).json()
        assert att["total"] == 1
        assert len(att["results"]) == 5
        statuses = {row["number"]: row["status"] for row in att["results"]}
        assert statuses[1] == "correct"
        assert all(statuses[n] == "unanswered" for n in (2, 3, 4, 5))

    def test_no_note_when_answered_only_excludes_nothing(self, client, wb):
        """Checking the box shouldn't fabricate an exclusion note when every
        question was actually answered."""
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        full_answers = {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"}
        att = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": full_answers, "answered_only": True},
        ).json()
        assert att["total"] == 5
        assert att["percent"] == 100.0
        assert "note" not in att

    def test_note_combines_extra_ignored_and_answered_only_reasons(self, client, wb):
        """Both note reasons can fire on one submission and must coexist."""
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        att = client.post(
            "/api/attempts",
            json={
                "section_id": sid,
                "answers": {"1": "3", "999": "1"},  # 999 extra; 2-5 unanswered
                "answered_only": True,
            },
        ).json()
        assert att["total"] == 1
        assert att["note"] == (
            "목록에 없는 문항 1개는 무시했습니다. 4문항은 미응답으로 채점에서 제외했습니다."
        )

    def test_answered_only_all_unanswered_returns_zero_not_error(self, client, wb):
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        resp = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {}, "answered_only": True},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["total"] == 0
        assert body["score"] == 0
        assert body["percent"] == 0.0

    def test_retry_merge_recomputes_total_from_full_merged_answer_set(self, client, wb):
        """A retry must exclude only questions still unanswered after merging
        with the session's latest submission, not just the numbers present
        in the retry's own payload. No merge_attempt_id needed -- the open
        session created by the first POST is auto-detected."""
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        base = client.post(
            "/api/attempts",
            json={
                "section_id": sid,
                "answers": {"1": "3", "2": "9"},
                "answered_only": True,
            },
        ).json()
        assert base["total"] == 2
        assert set(base["unanswered_numbers"]) == {3, 4, 5}

        retry = client.post(
            "/api/attempts",
            json={
                "section_id": sid,
                "answers": {"3": "1"},  # fill in one previously-skipped question
                "answered_only": True,
            },
        ).json()
        # merged answers are now {1: "3", 2: "9", 3: "1"} -> 4 and 5 still unanswered
        assert retry["is_first_submission"] is False
        assert retry["submission_seq"] == 2
        assert retry["total"] == 3
        assert retry["score"] == 2  # Q1 and Q3 correct, Q2 wrong
        assert set(retry["unanswered_numbers"]) == {4, 5}
        assert retry["percent"] == 66.7

        full = client.get(f"/api/attempts/{retry['id']}").json()
        assert full["total"] == 3
        assert full["percent"] == 66.7

    def test_answered_only_false_explicit_same_as_omitted(self, client, wb):
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        answers = {"1": "3", "2": "9"}
        omitted = client.post("/api/attempts", json={"section_id": sid, "answers": answers}).json()
        explicit = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": answers, "answered_only": False},
        ).json()
        assert omitted["total"] == explicit["total"] == 5
        assert omitted["percent"] == explicit["percent"]
