"""Independent, additional coverage for the "Session history detail view
(1st/2nd/3rd+ try breakdown)" chunk.

That chunk is overwhelmingly client-side (app/static/js/app.js, app/static/
css/app.css): a new #/session/:id route rendering the frozen first-
submission score ring (reusing viewAttempt's score-ring markup), the first
submission's wrong-answer review list, and three new try-count breakdown
blocks: '1차에 정답' / '2차에 정답' / '3차 이상 (미해결 포함)'; plus rewiring the
workbook section card's "응시 기록 보기" panel from the removed
GET /sections/{sid}/attempts to GET /sections/{sid}/sessions, with each
row's link retargeted from #/attempt/{id} to #/session/{id}.

Both backing endpoints (GET /api/sessions/{id}, GET /api/sections/{sid}/
sessions) and compute_breakdown() already existed before this chunk and
have extensive coverage in tests/test_sessions.py,
tests/test_sessions_api_layer_gaps.py, and tests/test_sessions_chunk_gaps.py
-- not re-tested here (see those files' own docstrings for what each
closes).

This file closes the two gaps none of those touch, both load-bearing for
this specific chunk's new rendering:

  * The one scenario the chunk's whole "label both explicitly so they don't
    read as contradictory" UI note exists for: breakdown.total_questions
    (the section's full answer-key count) actually diverging from
    first_total (the score ring's own denominator) when the first
    submission used answered_only to skip some questions. No existing test
    combines answered_only with the finished-session-detail endpoint --
    tests/test_answered_only.py never finishes a session or reads
    breakdown, and every breakdown test in test_sessions.py /
    test_sessions_chunk_gaps.py uses ordinary (non-answered_only)
    submissions where the two denominators happen to always coincide,
    which would silently hide a regression that collapsed them into one.
  * The exact field set GET /sections/{sid}/sessions (the workbook panel's
    new data source) returns per entry -- session_id / finished_at /
    first_score / first_total / first_percent -- which the rewired panel
    row markup now reads by name. Existing coverage of this endpoint only
    ever asserts list length and first_percent; a field renamed or dropped
    on the *list* endpoint specifically (independent of the single-session
    detail/finish responses, which share the same _serialize_session
    helper but are exercised by different tests) would pass every existing
    test while silently breaking every history-panel row.
"""

import pytest

DAY_SAMPLE = (
    "Day 01\n1. 3 2. 4 3. 1 4. 5 5. 2\n"
    "Day 02\n1. 2 2. 3 3. 4 4. 1 5. 5"
)


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
    r = client.post("/api/workbooks", json={"title": "세션 상세 화면 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def section(client, wb):
    """A single 5-question section (Day 01), key {1:3, 2:4, 3:1, 4:5, 5:2}."""
    return _import_headers(client, wb).json()["sections"][0]["id"]


# ---------------------------------------------------------------------------
# breakdown.total_questions vs. first_total: the two denominators the new
# detail screen explicitly labels so they don't read as contradictory.
# ---------------------------------------------------------------------------


class TestBreakdownDenominatorDivergesFromFirstTotalUnderAnsweredOnly:
    def test_answered_only_first_submission_leaves_breakdown_on_full_key_count(
        self, client, section
    ):
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                # Only Q1/Q2 answered (Q1 correct, Q2 wrong); Q3/4/5 skipped
                # entirely -- exactly what narrows this attempt's own total.
                "answers": {"1": "3", "2": "9"},
                "answered_only": True,
            },
        ).json()
        assert base["total"] == 2  # answered_only narrows THIS attempt's total
        assert base["is_first_submission"] is True

        client.post(f"/api/sessions/{base['session_id']}/finish")
        detail = client.get(f"/api/sessions/{base['session_id']}").json()

        # The frozen headline score's denominator stays the narrowed 2 --
        # exactly what answered_only produced on the first submission.
        assert detail["first_total"] == 2
        assert detail["first_score"] == 1
        assert detail["first_percent"] == 50.0

        # ... while breakdown's denominator is the section's FULL key count
        # (5), not the narrowed 2 -- the two really do diverge, not just in
        # theory.
        bd = detail["breakdown"]
        assert bd["total_questions"] == 5
        assert bd["total_questions"] != detail["first_total"]

        # Q1 correct on try 1; Q2/3/4/5 never correct in this session at all
        # (Q2 wrong, Q3-5 never even answered) -> all land in third_plus,
        # per spec ("never correct" includes "never answered").
        assert bd["first_try"]["numbers"] == [1]
        assert bd["second_try"]["numbers"] == []
        assert bd["third_plus"]["numbers"] == [2, 3, 4, 5]

        # Same numerator (the one correct-on-try-1 question), two
        # legitimately different percentages off two legitimately different
        # denominators -- exactly the pair the UI labels separately so they
        # never read as contradictory.
        assert bd["first_try"]["count"] == 1
        assert bd["first_try"]["percent"] == 20.0  # 1 / 5 (full section key)
        assert detail["first_percent"] == 50.0  # 1 / 2 (answered-only total)

        # The wrong-answer review list (first_results) still carries all 5
        # questions, including the 3 answered_only left out of `total` --
        # exactly what the wrong-card list needs to show them as unanswered
        # rather than silently dropping them.
        assert len(detail["first_results"]) == 5
        by_num = {r["number"]: r for r in detail["first_results"]}
        assert by_num[1]["status"] == "correct"
        assert by_num[2]["status"] == "incorrect"
        for n in (3, 4, 5):
            assert by_num[n]["status"] == "unanswered"

    def test_ordinary_full_submission_has_matching_denominators(
        self, client, section
    ):
        """Sanity check contrasting the divergence test above: with an
        ordinary (non-answered_only) first submission that answers every
        question, first_total and breakdown.total_questions naturally
        coincide -- confirming the divergence above really does come from
        answered_only, not from some other, unrelated source."""
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"},
            },
        ).json()
        client.post(f"/api/sessions/{base['session_id']}/finish")
        detail = client.get(f"/api/sessions/{base['session_id']}").json()
        assert detail["first_total"] == detail["breakdown"]["total_questions"] == 5


# ---------------------------------------------------------------------------
# GET /sections/{sid}/sessions field shape -- the workbook panel's new data
# source, read by field name in the rewired row markup.
# ---------------------------------------------------------------------------


class TestFinishedSessionsListFieldShape:
    def test_list_entry_has_the_fields_the_panel_row_reads(self, client, section):
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},  # Q5 unanswered
            },
        ).json()
        assert base["score"] == 3
        client.post(f"/api/sessions/{base['session_id']}/finish")

        [entry] = client.get(f"/api/sections/{section}/sessions").json()
        for field in (
            "session_id",
            "section_id",
            "status",
            "started_at",
            "finished_at",
            "first_score",
            "first_total",
            "first_percent",
        ):
            assert field in entry, f"missing {field!r} on a session-history list entry"

        assert entry["session_id"] == base["session_id"]
        assert entry["section_id"] == section
        assert entry["status"] == "finished"
        assert entry["finished_at"]  # non-null/non-empty -- the panel's date column
        assert entry["first_score"] == 3
        assert entry["first_total"] == 5
        assert entry["first_percent"] == 60.0

    def test_list_entry_fields_match_the_click_through_detail_endpoint(
        self, client, section
    ):
        """The panel links each row straight to #/session/{id} -- confirm
        the list entry's own score fields (not just first_percent, already
        covered in test_sessions.py) agree with what that click-through
        actually renders, so a row and its detail page can never disagree."""
        base = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"1": "3", "2": "9"}},
        ).json()
        client.post(f"/api/sessions/{base['session_id']}/finish")

        [entry] = client.get(f"/api/sections/{section}/sessions").json()
        detail = client.get(f"/api/sessions/{entry['session_id']}").json()
        assert entry["first_score"] == detail["first_score"]
        assert entry["first_total"] == detail["first_total"]
        assert entry["finished_at"] == detail["finished_at"]

    def test_multiple_finished_sessions_each_keep_their_own_scores_in_the_list(
        self, client, section
    ):
        """Two finished sessions in the same section's list must not have
        their scores conflated -- each row must be independently correct,
        since the panel renders every row from this one array."""
        first = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        ).json()
        client.post(f"/api/sessions/{first['session_id']}/finish")

        second = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"1": "3", "2": "4"}},
        ).json()
        client.post(f"/api/sessions/{second['session_id']}/finish")

        entries = client.get(f"/api/sections/{section}/sessions").json()
        assert len(entries) == 2
        by_id = {e["session_id"]: e for e in entries}
        assert by_id[first["session_id"]]["first_score"] == 1
        assert by_id[first["session_id"]]["first_total"] == 5
        assert by_id[second["session_id"]]["first_score"] == 2
        assert by_id[second["session_id"]]["first_total"] == 5
