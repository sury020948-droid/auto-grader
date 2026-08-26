"""Independent, additional coverage for the "Celebration animation on 100%
first-submission accuracy" chunk.

That chunk is entirely client-side (app/static/js/app.js, app/static/css/
app.css): a self-contained Canvas 2D confetti burst (no CDN/library, no new
dependency), wired into two existing render functions at the same
requestAnimationFrame sync point that already animates the score ring:

  * viewAttempt() -- the screen shown right after ANY submission, first or
    retry -- calls celebrate() only when
    `attempt.is_first_submission === true && percent === 100`, deliberately
    NOT on `percent === 100` alone, since this screen also renders retries
    and a retry recovering to 100% is not the session's recorded
    first-submission accuracy.
  * viewSessionDetail() -- the finished-session history screen -- calls
    celebrate() on `percent === 100` alone, where `percent` reads
    `Number(detail.first_percent || 0)` off GET /sessions/{id}; no extra
    is_first_submission gate is needed there because that endpoint only
    ever exposes the session's frozen first submission, never a later
    retry.

None of that DOM/canvas logic is reachable from this Python test suite --
no JS runtime or browser here, only the HTTP API app.js itself calls (and
no test in this repo fetches or parses the static JS/CSS/HTML, confirmed by
grep). What IS backend-observable, and load-bearing for those two gates to
fire correctly (and not mis-fire), is the exact combination of
is_first_submission / percent / first_percent values the two call sites
read off POST /attempts and GET /sessions/{id}. Existing tests
(test_sessions.py, test_results_finish_split_gaps.py, test_grader.py,
test_answered_only.py, test_session_detail_view_gaps.py) each cover pieces
of this in isolation -- e.g. that a retry keeps first_percent frozen, or
that grade() returns 0.0 (never 100.0) for a 0/0 submission -- but never
assert the specific combinations these two gates actually branch on, in
one place, framed around what would make celebrate() fire or not fire.
This file closes exactly those combinations.
"""

import pytest

DAY_SAMPLE = (
    "Day 01\n1. 3 2. 4 3. 1 4. 5 5. 2\n"
    "Day 02\n1. 2 2. 3 3. 4 4. 1 5. 5"
)

FULL_CORRECT = {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"}


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
    r = client.post("/api/workbooks", json={"title": "축하 애니메이션 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def section(client, wb):
    """A single 5-question section (Day 01), key {1:3, 2:4, 3:1, 4:5, 5:2}."""
    return _import_headers(client, wb).json()["sections"][0]["id"]


# ---------------------------------------------------------------------------
# viewAttempt()'s gate: attempt.is_first_submission === true && percent === 100
# ---------------------------------------------------------------------------


class TestViewAttemptGateFirstSubmissionPerfectScore:
    """A genuine (non-retry) first submission that's fully correct must
    satisfy BOTH halves of viewAttempt's gate together, on the very
    response object the results screen renders from."""

    def test_full_correct_first_submission_satisfies_both_halves_of_the_gate(
        self, client, section
    ):
        att = client.post(
            "/api/attempts", json={"section_id": section, "answers": FULL_CORRECT}
        ).json()
        assert att["is_first_submission"] is True
        assert att["percent"] == 100.0
        assert att["score"] == att["total"] == 5

    def test_answered_only_first_submission_perfect_on_narrowed_subset_still_gates_true(
        self, client, section
    ):
        """The gate only reads is_first_submission/percent, not total -- a
        first submission that used answered_only to skip some questions but
        got every ANSWERED one right must still satisfy the gate, even
        though its own total (2) is far short of the section's full
        5-question key."""
        att = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "3": "1"},  # Q2/4/5 left unanswered
                "answered_only": True,
            },
        ).json()
        assert att["is_first_submission"] is True
        assert att["total"] == 2
        assert att["percent"] == 100.0


class TestViewAttemptGateRetryReaching100NeverSatisfiesIt:
    """A retry that reaches 100% must fail the `is_first_submission ===
    true` half of viewAttempt's gate, even though it passes the
    `percent === 100` half on its own -- the exact case the JS comment
    calls out by name ("a retry that also happens to score 100% is not the
    session's recorded first-submission accuracy")."""

    def test_retry_percent_100_is_first_submission_false_first_percent_frozen_imperfect(
        self, client, section
    ):
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                # Q1-4 correct, Q5 wrong (given "9", key is "2") -> 80%.
                "answers": {"1": "3", "2": "4", "3": "1", "4": "5", "5": "9"},
            },
        ).json()
        assert base["percent"] == 80.0

        retry = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"5": "2"}},
        ).json()
        # Passes ONE half of the gate...
        assert retry["percent"] == 100.0
        # ...but fails the other -- so `is_first_submission === true &&
        # percent === 100` as a whole is false, and viewAttempt must not
        # celebrate on this response.
        assert retry["is_first_submission"] is False
        # The session's own frozen score (what viewSessionDetail will later
        # read) never became 100 either.
        assert retry["first_percent"] == 80.0

    def test_is_first_submission_stays_false_at_every_retry_on_the_way_to_100(
        self, client, section
    ):
        """Reached gradually over two retries -- proves is_first_submission
        isn't merely false right after the first retry, but stays false for
        every later submission too, however many it takes to reach 100%."""
        base = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        ).json()
        assert base["is_first_submission"] is True
        assert base["percent"] == 20.0

        second = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"2": "4", "3": "1"}},
        ).json()
        assert second["is_first_submission"] is False
        assert second["percent"] == 60.0  # not yet 100 -- gate correctly stays closed
        assert second["first_percent"] == 20.0

        third = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"4": "5", "5": "2"}},
        ).json()
        assert third["percent"] == 100.0
        assert third["is_first_submission"] is False  # still false -- gate stays closed
        assert third["first_percent"] == 20.0  # frozen at the ORIGINAL first submission


# ---------------------------------------------------------------------------
# viewSessionDetail()'s gate: percent === 100, reading detail.first_percent
# off GET /sessions/{id} -- no is_first_submission check needed there, since
# that endpoint only ever reflects the session's frozen first submission.
# ---------------------------------------------------------------------------


class TestViewSessionDetailGateFrozenFirstPercent:
    def test_first_submission_perfect_then_finished_reports_first_percent_100(
        self, client, section
    ):
        base = client.post(
            "/api/attempts", json={"section_id": section, "answers": FULL_CORRECT}
        ).json()
        assert base["is_first_submission"] is True
        client.post(f"/api/sessions/{base['session_id']}/finish")

        detail = client.get(f"/api/sessions/{base['session_id']}").json()
        assert detail["status"] == "finished"
        assert detail["first_percent"] == 100.0
        assert detail["first_score"] == detail["first_total"] == 5
        # first_results is what the wrong-answer list reads -- confirm
        # there's genuinely nothing wrong to show alongside the celebration.
        assert len(detail["first_results"]) == 5
        assert all(r["status"] == "correct" for r in detail["first_results"])

    def test_session_only_reaching_100_via_retry_never_reports_first_percent_100(
        self, client, section
    ):
        """The companion case: an imperfect first submission fixed entirely
        by a retry must NOT make GET /sessions/{id} -- the exact endpoint
        viewSessionDetail renders from -- report first_percent==100.
        Neither test_finished_session_detail_has_breakdown_and_first_results
        (test_sessions.py, which never actually reaches 100%) nor
        test_retry_auto_detected_and_first_submission_score_stays_frozen
        (which checks the /finish response and the list endpoint, but never
        calls this single-session detail route) covers this exact route in
        this exact reaches-100%-via-retry scenario."""
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "4", "3": "1", "4": "5", "5": "9"},
            },
        ).json()
        assert base["percent"] == 80.0
        client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"5": "2"}},
        )  # retry brings the SESSION's latest attempt to 100%, but only as a retry

        client.post(f"/api/sessions/{base['session_id']}/finish")
        detail = client.get(f"/api/sessions/{base['session_id']}").json()
        assert detail["first_percent"] == 80.0
        assert detail["first_percent"] != 100.0


# ---------------------------------------------------------------------------
# The zero-total edge case: grade() already guarantees percent==0.0 (never
# 100.0) when total==0 (test_grader.py::
# test_answered_only_all_unanswered_gives_zero_total), at the pure-function
# level. Both celebration gates read percent off the session/attempt API
# layer, not grade() directly -- this re-proves the same invariant all the
# way through POST /attempts and GET /sessions/{id}, so a future refactor
# of create_attempt/create_session can't silently let a nothing-was-ever-
# answered submission read as a "perfect" 100% and wrongly fire confetti.
# ---------------------------------------------------------------------------


class TestNeitherGateEverFiresOnTheZeroTotalEdgeCase:
    def test_all_unanswered_answered_only_first_submission_percent_is_zero_not_100(
        self, client, section
    ):
        att = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {}, "answered_only": True},
        ).json()
        # Passes HALF of viewAttempt's gate (it IS the first submission)...
        assert att["is_first_submission"] is True
        assert att["total"] == 0
        assert att["score"] == 0
        # ...but this half stays false, so `percent === 100` never holds and
        # the gate as a whole never fires on a nothing-was-answered attempt.
        assert att["percent"] == 0.0

    def test_finished_zero_total_session_detail_first_percent_is_zero_not_100(
        self, client, section
    ):
        base = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {}, "answered_only": True},
        ).json()
        client.post(f"/api/sessions/{base['session_id']}/finish")

        detail = client.get(f"/api/sessions/{base['session_id']}").json()
        assert detail["first_total"] == 0
        assert detail["first_percent"] == 0.0
