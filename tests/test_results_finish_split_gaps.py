"""Independent, additional coverage for the "Results screen Finish/Back
split + workbook section-card resume buttons" chunk.

That chunk is overwhelmingly client-side (app/static/js/app.js,
app/static/index.html, app/static/css/app.css): a new distinct '채점
끝내기' button on the results screen, giving '목록으로' real
leave-without-finishing semantics, a retry-vs-recorded-score note, the
'섹션 삭제' relabel, the workbook section-card branch on open_session_id
into two buttons, and the import-conflict dialog copy fix. None of that is
reachable from this Python test suite -- there is no JS runtime or DOM
here, only the HTTP API app.js calls.

The one genuinely new, backend-observable surface this chunk adds is
`AttemptResult.first_percent` (routers/attempts.py's serialize_attempt),
which the new "첫 제출 기준 점수: ..." note depends on to tell a retry's
own round apart from the session's permanently-recorded score.

Everything else this chunk's own summary touches server-side already
existed before it and is already exercised elsewhere -- not re-tested here:
  * `session_finished`, `session_id`, `is_first_submission`,
    `submission_seq` on AttemptResult -- tests/test_sessions.py,
    tests/test_sessions_api_layer_gaps.py::TestSessionFinishedFlag.
  * POST /sessions/{id}/finish itself (idempotency, 404s) and the
    session-detail shape's own pre-existing `first_percent` field --
    tests/test_sessions.py::TestSessionDetailEndpoint, tests/
    test_sessions_api_layer_gaps.py::TestFinishSessionIdempotency.
  * `open_session_id` / `session_count` on list_sections / GET
    /workbooks/{wid} -- tests/test_sessions_api_layer_gaps.py::
    TestOpenSessionIdOnWorkbookDetail, TestWorkbookStatsSessionCountRename.

This file closes the one gap none of those touch: `first_percent` on the
AttemptResult shape itself (distinct from the session-detail shape, which
had its own same-named field before this chunk) --
  * present and equal to `percent` on a section's first-ever submission;
  * on a retry, already correct while the session is still open
    (in_progress) -- the normal state right after a retry, and exactly
    when the results screen needs it, not only once someone later finishes;
  * proven in BOTH directions (a retry that improves on the frozen score,
    and one that makes it worse) so a max()/min()-of-submissions bug would
    fail at least one of them;
  * still the FIRST submission's score, not the immediately-preceding
    one's, after a third submission;
  * survives an independent re-fetch (GET /attempts/{id}) for both the
    base and the retry attempt, not just as an artifact of the POST
    response, and survives the session later being finished;
  * also correct through GET /sections/{sid}/session's `latest_attempt`,
    serialize_attempt's third live call site;
  * `None` -- not 0.0, not a crash -- for an attempt with no owning session
    at all (`session_id` NULL), exercising the documented `session=None`
    fallback branch.
"""

import pytest

from app import db as dal

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
    r = client.post("/api/workbooks", json={"title": "결과 화면 종료 분리 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def section(client, wb):
    """A single 5-question section (Day 01), key {1:3, 2:4, 3:1, 4:5, 5:2}."""
    return _import_headers(client, wb).json()["sections"][0]["id"]


class TestFirstPercentOnFirstSubmission:
    def test_equals_own_percent(self, client, section):
        att = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},
            },
        ).json()
        assert att["is_first_submission"] is True
        assert att["percent"] == 60.0
        assert att["first_percent"] == 60.0


class TestFirstPercentFrozenThroughRetryWhileSessionStillOpen:
    """The results screen's retry note is rendered straight from a retry's
    own POST /attempts response -- normally well before anyone finishes the
    session -- so first_percent must already be right at that point, not
    only once the session is later finished."""

    def test_improving_retry_keeps_the_original_frozen_score(self, client, section):
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},
            },
        ).json()
        assert base["percent"] == 60.0

        retry = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"2": "4", "5": "2"}},
        ).json()
        assert retry["is_first_submission"] is False
        assert retry["percent"] == 100.0
        assert retry["first_percent"] == 60.0  # frozen at the FIRST submission
        assert retry["session_finished"] is False  # true right after a retry, unfinished

    def test_worsening_retry_still_keeps_the_original_frozen_score(self, client, section):
        """The opposite direction: a retry that makes things WORSE than the
        frozen first submission must still report the original score, not
        its own (lower) one -- proves first_percent isn't accidentally a
        max()/latest-of-the-session value."""
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"},
            },
        ).json()
        assert base["percent"] == 100.0

        retry = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"1": "9"}},  # was correct, now wrong
        ).json()
        assert retry["percent"] == 80.0
        assert retry["first_percent"] == 100.0

    def test_survives_independent_refetch_for_both_base_and_retry(self, client, section):
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},
            },
        ).json()
        retry = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"2": "4", "5": "2"}},
        ).json()

        refetched_base = client.get(f"/api/attempts/{base['id']}").json()
        assert refetched_base["percent"] == 60.0
        assert refetched_base["first_percent"] == 60.0

        refetched_retry = client.get(f"/api/attempts/{retry['id']}").json()
        assert refetched_retry["percent"] == 100.0
        assert refetched_retry["first_percent"] == 60.0  # still frozen, not 100.0

    def test_third_submission_still_reflects_the_first_not_the_second(self, client, section):
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},
            },
        ).json()
        assert base["percent"] == 60.0

        second = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"2": "4"}}
        ).json()
        assert second["submission_seq"] == 2
        assert second["percent"] == 80.0

        third = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"5": "2"}}
        ).json()
        assert third["submission_seq"] == 3
        assert third["percent"] == 100.0
        assert third["first_percent"] == 60.0  # not 80.0 (the 2nd try), still the 1st


class TestFirstPercentSurvivesFinish:
    def test_stays_frozen_and_session_finished_flips_true(self, client, section):
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},
            },
        ).json()
        retry = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"2": "4", "5": "2"}},
        ).json()

        client.post(f"/api/sessions/{base['session_id']}/finish")

        refetched_retry = client.get(f"/api/attempts/{retry['id']}").json()
        assert refetched_retry["session_finished"] is True
        assert refetched_retry["percent"] == 100.0
        assert refetched_retry["first_percent"] == 60.0


class TestFirstPercentOnOpenSessionLatestAttempt:
    """GET /sections/{sid}/session's `latest_attempt` is serialize_attempt's
    third live call site (besides POST /attempts and GET /attempts/{id}) --
    it must carry the same frozen value too."""

    def test_latest_attempt_first_percent_matches(self, client, section):
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},
            },
        ).json()
        client.post("/api/attempts", json={"section_id": section, "answers": {"2": "4"}})

        open_sess = client.get(f"/api/sections/{section}/session").json()
        assert open_sess["latest_attempt"]["is_first_submission"] is False
        assert open_sess["latest_attempt"]["percent"] == 80.0
        assert open_sess["latest_attempt"]["first_percent"] == base["percent"] == 60.0


class TestFirstPercentNullWithoutASession:
    def test_none_for_an_attempt_never_linked_to_a_session(
        self, client, section, device_id
    ):
        """The real state the `session=None` branch guards against: an
        attempt row with session_id NULL (pre-sessions/legacy data, or any
        row a direct-DAL caller leaves unlinked, exactly as
        test_sessions_dal_more.py's own unlinked-attempt test constructs
        one) -- first_percent must read None, not 0.0 and not crash."""
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            aid = dal.create_attempt(
                conn,
                uid,
                section,
                2,
                5,
                40.0,
                [
                    {"number": 1, "given": "3", "expected": "3", "status": "correct"},
                    {"number": 2, "given": "9", "expected": "4", "status": "incorrect"},
                ],
            )
            conn.commit()
        finally:
            conn.close()

        fetched = client.get(f"/api/attempts/{aid}").json()
        assert fetched["session_id"] is None
        assert fetched["session_finished"] is False
        assert fetched["first_percent"] is None
        assert fetched["percent"] == 40.0
