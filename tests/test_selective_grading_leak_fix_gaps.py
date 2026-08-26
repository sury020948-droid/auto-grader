"""Independent, additional coverage for the bug fix: "unanswered questions
leaking into selective-grading (answered_only) results."

Bug: with answered_only=True, a question the student left blank was never
graded (it's excluded from that submission's own `total`/`percent`) -- but
it still showed up as if it *had* been graded in every display aggregate
downstream: the results-screen correct/incorrect/unanswered chips, its
wrong-question list, the finished-session history detail screen, and the
per-workbook "frequently missed" widget.

Where the actual fix lives, and why this file only exercises one of the
three sites:

  * `app/static/js/app.js` -- `viewAttempt()` (results screen right after
    submission) and `viewSessionDetail()` (finished-session history
    screen) now each derive a `displayResults` array that drops
    `status === 'unanswered'` rows *only when this submission/session was
    actually narrowed by answered_only* (`total < results.length`), and
    build the correct/incorrect/unanswered chips and the wrong-question
    list from that instead of the raw `results`. This is genuinely NOT
    reachable from this Python test suite -- there is no JS runtime/DOM
    here, only the HTTP API app.js calls (same limitation documented in
    tests/test_results_finish_split_gaps.py for an earlier client-only
    chunk). Confirmed independently, not just asserted: the backend
    response those two functions consume is *unchanged* by this fix --
    `results/first_results` still carries one row per answer-key number,
    'unanswered' included, exactly as tests/test_answered_only.py::
    test_results_still_lists_every_question_when_total_is_shrunk and
    tests/test_quiz_resume_screen_gaps.py already pin -- so there is no
    HTTP-observable difference this suite could assert on for those two
    call sites; the fix is 100% client-side there.

  * `app/db.py` -- `top_missed()` (backs GET /api/workbooks/{wid}/stats'
    "frequently missed" widget) is the one site of this fix that IS a
    real, independently-testable backend change: an answered_only-skipped
    (never graded) number must not inflate `count`, and `given`'s
    correlated subquery must not surface such a row's (blank) answer
    either. This file's new coverage targets exactly that, deliberately
    disjoint from the 3 tests the implementation itself already added
    (tests/test_sessions_api_layer_gaps.py::
    TestTopMissedExcludesAnsweredOnlySkips):
      * multiple wrong numbers *and* multiple answered_only-skipped
        numbers in the same submission, asserting the exact surviving
        set/count/given per row, not just "the one wrong number survives";
      * the total=0 boundary -- a submission where *every* question was
        left blank under answered_only (`a.total(0) >= row_count`) must
        contribute nothing at all, proving the fix's `>=` comparison holds
        at its edge rather than merely for a partial skip;
      * the two behaviors coexisting correctly within one workbook: an
        ordinary (non-answered_only) unanswered question in one section
        must still be counted (this fix must not overreach into excluding
        *every* 'unanswered' row, only ones a real answered_only narrowing
        actually excluded from grading) while an answered_only-skipped
        number in a sibling section of the same workbook is excluded --
        the single test that most directly checks the fix is scoped
        precisely rather than broadened by accident;
      * that ordinary cross-session count *accumulation* for a repeated,
        actually-graded miss (unrelated to answered_only) still sums
        correctly after the WHERE clause was restructured to add the new
        exclusion branch.
"""

import pytest

DAY_SAMPLE = (
    "Day 01\n1. 3 2. 4 3. 1 4. 5 5. 2\n"
    "Day 02\n1. 2 2. 3 3. 4 4. 1 5. 5"
)

DAY01_CORRECT = {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"}
DAY02_CORRECT = {"1": "2", "2": "3", "3": "4", "4": "1", "5": "5"}


def _import_headers(client, wid, sample=DAY_SAMPLE):
    preview = client.post("/api/extract-text", json={"raw_text": sample}).json()
    entries = [
        {"number": e["number"], "answer": e["answer"], "line": e.get("line", 0)}
        for e in preview["entries"]
    ]
    return client.post(
        f"/api/workbooks/{wid}/sections/import",
        json={
            "structure": "headers",
            "header_type": "day",
            "entries": entries,
            "headers": preview["headers"],
        },
    )


@pytest.fixture()
def wb(client):
    r = client.post("/api/workbooks", json={"title": "선택 채점 결과 누출 수정 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def section(client, wb):
    """A single 5-question section (Day 01), key {1:3, 2:4, 3:1, 4:5, 5:2}."""
    return _import_headers(client, wb).json()["sections"][0]["id"]


@pytest.fixture()
def two_sections(client, wb):
    secs = _import_headers(client, wb).json()["sections"]
    return wb, secs[0]["id"], secs[1]["id"]


def _top_missed(client, wid):
    return client.get(f"/api/workbooks/{wid}/stats").json()["top_missed"]


class TestTopMissedFullyExcludesAnsweredOnlySkips:
    """New scenarios beyond the implementation's own
    TestTopMissedExcludesAnsweredOnlySkips (test_sessions_api_layer_gaps.py)."""

    def test_multiple_wrong_and_multiple_skipped_numbers_in_one_submission(
        self, client, section, wb
    ):
        """Q1/Q3 answered wrong, Q2 answered correct, Q4/Q5 left blank under
        answered_only -- both never-graded numbers (4, 5) must be fully
        absent, and both real misses (1, 3) must survive with their own
        exact count/given, not merely as a single collapsed row."""
        att = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "9", "2": "4", "3": "7"},
                "answered_only": True,
            },
        ).json()
        assert att["total"] == 3
        assert sorted(att["wrong_numbers"]) == [1, 3]
        assert sorted(att["unanswered_numbers"]) == [4, 5]
        client.post(f"/api/sessions/{att['session_id']}/finish")

        top = _top_missed(client, wb)
        assert {m["number"] for m in top} == {1, 3}
        by_num = {m["number"]: m for m in top}
        assert by_num[1]["count"] == 1
        assert by_num[1]["given"] == "9"
        assert by_num[3]["count"] == 1
        assert by_num[3]["given"] == "7"
        # tie on count -> ORDER BY count DESC, number ASC
        assert [m["number"] for m in top] == [1, 3]

    def test_fully_blank_answered_only_submission_contributes_nothing(
        self, client, section, wb
    ):
        """Every question left blank under answered_only narrows `total`
        all the way to 0 -- the boundary of the fix's `a.total >= row
        count` guard (0 >= 5). None of the 5 numbers may appear."""
        att = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {}, "answered_only": True},
        ).json()
        assert att["total"] == 0
        assert sorted(att["unanswered_numbers"]) == [1, 2, 3, 4, 5]
        client.post(f"/api/sessions/{att['session_id']}/finish")

        assert _top_missed(client, wb) == []

    def test_ordinary_unanswered_miss_still_counted_alongside_an_excluded_answered_only_skip(
        self, client, two_sections
    ):
        """The fix must be scoped to answered_only-narrowed submissions
        only. In the SAME workbook: section 1's Day 01 Q5 is left blank in
        an ordinary (non-selective) submission -- a real, gradeable miss
        that must still surface, exactly as before this fix. Section 2's
        Day 02 Q2 is left blank under answered_only -- never graded, must
        be fully absent. Day 02 Q1 is answered wrong and must survive."""
        wid, sec1, sec2 = two_sections

        att1 = client.post(
            "/api/attempts",
            json={
                "section_id": sec1,
                "answers": {k: v for k, v in DAY01_CORRECT.items() if k != "5"},
            },
        ).json()
        assert att1["total"] == 5  # ordinary mode: total is never narrowed
        assert att1["unanswered_numbers"] == [5]
        client.post(f"/api/sessions/{att1['session_id']}/finish")

        att2 = client.post(
            "/api/attempts",
            json={
                "section_id": sec2,
                "answers": {
                    "1": "9",
                    **{k: v for k, v in DAY02_CORRECT.items() if k not in ("1", "2")},
                },
                "answered_only": True,
            },
        ).json()
        assert att2["total"] == 4
        assert att2["unanswered_numbers"] == [2]
        assert att2["wrong_numbers"] == [1]
        client.post(f"/api/sessions/{att2['session_id']}/finish")

        top = _top_missed(client, wid)
        assert len(top) == 2
        by_key = {(m["section_label"], m["number"]): m for m in top}
        assert set(by_key) == {("Day 01", 5), ("Day 02", 1)}
        assert by_key[("Day 01", 5)]["given"] == ""  # real miss, blank given
        assert by_key[("Day 02", 1)]["given"] == "9"
        assert ("Day 02", 2) not in by_key  # never-graded skip: fully absent

    def test_repeated_ordinary_miss_across_two_sessions_still_sums_count(
        self, client, section, wb
    ):
        """Unrelated to answered_only, but the fix restructured this
        query's WHERE clause -- confirm normal cross-session accumulation
        for a genuinely repeated miss still sums to 2, not 1, and `given`
        still tracks the most recent of the two."""
        att_a = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {**DAY01_CORRECT, "1": "9"}},
        ).json()
        client.post(f"/api/sessions/{att_a['session_id']}/finish")

        att_b = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {**DAY01_CORRECT, "1": "8"}},
        ).json()
        client.post(f"/api/sessions/{att_b['session_id']}/finish")

        top = _top_missed(client, wb)
        assert len(top) == 1
        assert top[0]["number"] == 1
        assert top[0]["count"] == 2
        assert top[0]["given"] == "8"
