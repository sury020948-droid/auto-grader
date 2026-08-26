"""Independent, additional coverage for the "Quiz screen: server-driven
retry & resume with previous-answer display" chunk.

app/static/js/app.js's viewSolve(sid) was rewritten to delete the
sessionStorage-based retry_numbers/retry_section_id/retry_base_attempt
mechanism entirely and instead derive *everything* about resume mode from a
single GET /sections/{sid}/session call: which numbers still need retrying
(status != 'correct'), what was given last time (`given`, driving the new
per-input "이전 답: ..." hint / "(미응답)" fallback), and the banner's
submission_count. It also always POSTs /attempts as a bare
{section_id, answers, answered_only} now -- no merge_attempt_id -- relying
entirely on server-side open-session auto-detection, and it removes the
'전체 문항으로 풀기' cancel-retry escape hatch on the reasoning that no
correct client-only implementation exists for it under this model.

The backend contract itself (merge_answers, auto-retry-detection, session
lifecycle) already has extensive coverage in tests/test_sessions.py,
tests/test_sessions_api_layer_gaps.py, tests/test_answered_only.py, and
friends -- see those files' own docstrings for what they each close. This
file targets only the specific angles the new client-side logic depends on
that no existing test happens to exercise:

  * GET /sections/{sid}/session's `submission_count` field is never
    asserted anywhere on the *open*-session endpoint itself (only on the
    finished-session detail endpoint, GET /sessions/{id}) -- but it's
    exactly what drives the new "이어서 채점 중 -- 지금까지 N번
    제출했고..." banner text.
  * The "all caught up" edge case: a retry that fixes every remaining
    wrong/unanswered number leaves the session OPEN (not auto-finished)
    with zero remaining non-'correct' results -- the exact condition the
    new `allCaughtUp` branch renders a finish-the-session card for instead
    of an empty grid. The closest existing test
    (test_retry_auto_detected_and_first_submission_score_stays_frozen in
    test_sessions.py) only checks history/stats after such a retry, never
    re-fetches GET .../session to see this endpoint's own post-retry shape.
  * Once finished from that all-caught-up state, GET .../session 404s
    again (fresh) and the *next* POST /attempts opens a genuinely new
    session (different session_id) -- the only correct way to "start
    fresh" now that the client-only cancel-retry escape hatch is gone.
  * A full re-submission (every number in the section, not just the ones
    the resume screen would actually show as retry-worthy) while a session
    is open still merges onto that SAME session rather than starting a new
    one -- the exact server behavior the removed cancel-retry button's
    "no correct client-only implementation" reasoning rests on.
  * The `given` value surfaced per-number for the retry hint line: a
    never-answered number comes back as "" (falsy, driving the JS's
    '(미응답)' fallback), and a wrongly-answered number comes back with the
    verbatim value the user typed -- both read directly off
    latest_attempt.results, exactly as the new per-input hint does.
  * An `answered_only` first submission that skips some numbers still
    surfaces those numbers as retry-worthy in the *next*
    GET .../session -- results/remaining is never scoped down by
    answered_only's shrunken total, only the score/percent are.
  * Two independent resume fetches around a retry, mirroring both "retry
    immediately after seeing results" and "come back later" -- since the
    whole point of this chunk is that both are the exact same
    server-state-driven code path, with nothing client-side carried
    between them.
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
    r = client.post("/api/workbooks", json={"title": "퀴즈 재개 화면 갭 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def section(client, wb):
    """A single 5-question section (Day 01), key {1:3, 2:4, 3:1, 4:5, 5:2}."""
    return _import_headers(client, wb).json()["sections"][0]["id"]


class TestSubmissionCountOnOpenSessionEndpoint:
    """Drives the new banner's "지금까지 N번 제출했고..." text."""

    def test_submission_count_increments_across_retries_on_the_open_endpoint(
        self, client, section
    ):
        base = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"1": "3", "2": "9"}},
        ).json()
        open_sess = client.get(f"/api/sections/{section}/session").json()
        assert open_sess["session_id"] == base["session_id"]
        assert open_sess["submission_count"] == 1

        client.post("/api/attempts", json={"section_id": section, "answers": {"2": "8"}})
        open_sess2 = client.get(f"/api/sections/{section}/session").json()
        assert open_sess2["submission_count"] == 2
        assert open_sess2["session_id"] == base["session_id"]


class TestAllCaughtUpWhileStillOpen:
    """The new `allCaughtUp` branch's exact trigger condition."""

    def test_zero_remaining_after_a_perfect_retry_but_session_stays_open(
        self, client, section
    ):
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},  # Q5 unanswered
            },
        ).json()
        assert base["score"] == 3

        retry = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"2": "4", "5": "2"}},
        ).json()
        assert retry["score"] == 5
        assert retry["percent"] == 100.0

        # A perfect retry never auto-finishes the session -- GET .../session
        # must still 200 (open), not 404.
        open_sess = client.get(f"/api/sections/{section}/session")
        assert open_sess.status_code == 200
        body = open_sess.json()
        assert body["session_id"] == base["session_id"]
        assert body["submission_count"] == 2
        remaining = [
            r["number"]
            for r in body["latest_attempt"]["results"]
            if r["status"] != "correct"
        ]
        assert remaining == []  # exactly the allCaughtUp trigger condition

        # And it must not have silently shown up in finished history either.
        assert client.get(f"/api/sections/{section}/sessions").json() == []

    def test_finishing_from_all_caught_up_then_a_fresh_session_starts_clean(
        self, client, section
    ):
        base = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "9"}}
        ).json()
        client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"},
            },
        )

        finish = client.post(f"/api/sessions/{base['session_id']}/finish")
        assert finish.status_code == 200

        # Fresh again -- no client-only "cancel retry" needed; finishing IS
        # the only correct way to start over under the new model.
        assert client.get(f"/api/sections/{section}/session").status_code == 404

        again = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        ).json()
        assert again["session_id"] != base["session_id"]
        assert again["is_first_submission"] is True
        assert again["submission_seq"] == 1


class TestFullResubmissionMergesOntoSameOpenSession:
    def test_resubmitting_every_number_stays_on_the_same_session(
        self, client, section
    ):
        """The removed '전체 문항으로 풀기' escape hatch had no correct
        client-only implementation precisely because of this: POSTing a
        fresh full answer set covering every number in the section, while a
        session is open, is auto-detected as just another retry on that
        SAME session -- never a new one -- regardless of how many numbers
        the payload happens to cover."""
        base = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "9"}}
        ).json()
        assert base["is_first_submission"] is True
        assert base["submission_seq"] == 1

        full_again = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"},
            },
        ).json()
        assert full_again["session_id"] == base["session_id"]
        assert full_again["is_first_submission"] is False
        assert full_again["submission_seq"] == 2
        assert full_again["score"] == 5

        # Still just one (open) session -- no phantom second one was created.
        assert client.get(f"/api/sections/{section}/sessions").json() == []


class TestGivenValueForRetryHint:
    def test_unanswered_given_is_empty_string_and_wrong_given_is_verbatim(
        self, client, section
    ):
        client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9"},  # Q3/4/5 left unanswered
            },
        )
        body = client.get(f"/api/sections/{section}/session").json()
        results = body["latest_attempt"]["results"]
        by_num = {r["number"]: r for r in results}

        assert by_num[2]["status"] == "incorrect"
        assert by_num[2]["given"] == "9"  # verbatim -> JS renders "이전 답: 9"

        for n in (3, 4, 5):
            assert by_num[n]["status"] == "unanswered"
            assert by_num[n]["given"] == ""  # falsy -> JS renders "(미응답)"

        remaining_numbers = {r["number"] for r in results if r["status"] != "correct"}
        assert remaining_numbers == {2, 3, 4, 5}
        assert 1 not in remaining_numbers  # correctly-answered Q1 stays hidden


class TestAnsweredOnlyDoesNotHideNumbersFromResume:
    def test_answered_only_first_submission_still_surfaces_skipped_numbers(
        self, client, section
    ):
        """A first submission made with answered_only=True shrinks `total`
        to exclude untouched numbers, but the *next* GET .../session must
        still list those numbers as retry-worthy -- results/remaining is
        never scoped down by answered_only's shrunken total, only the
        score percentage is."""
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9"},
                "answered_only": True,
            },
        ).json()
        assert base["total"] == 2  # Q3/4/5 excluded from *this attempt's* total

        body = client.get(f"/api/sections/{section}/session").json()
        remaining = sorted(
            r["number"]
            for r in body["latest_attempt"]["results"]
            if r["status"] != "correct"
        )
        assert remaining == [2, 3, 4, 5]  # Q3-5 still surfaced despite being outside `total`


class TestRepeatedResumeRoundTrips:
    def test_two_independent_resume_fetches_mirror_immediate_and_later_retry(
        self, client, section
    ):
        """The new viewSolve has exactly one code path for both 'retry
        immediately after seeing results' and 'come back later': both are
        just a fresh GET /sections/{sid}/session reacting to whatever the
        server currently holds, with nothing client-side carried over
        between visits. Simulate two separate 'visits' as two independent
        GETs with real work happening in between, and confirm each visit's
        snapshot is self-consistent."""
        base = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"1": "3", "2": "9", "3": "8"}},
        ).json()  # Q4/5 unanswered too

        # "Visit" 1 -- immediately after seeing results.
        visit1 = client.get(f"/api/sections/{section}/session").json()
        remaining1 = sorted(
            r["number"]
            for r in visit1["latest_attempt"]["results"]
            if r["status"] != "correct"
        )
        assert remaining1 == [2, 3, 4, 5]
        assert visit1["submission_count"] == 1

        client.post(
            "/api/attempts", json={"section_id": section, "answers": {"2": "4"}}
        )  # only Q2 fixed on this retry

        # "Visit" 2 -- "come back later": a brand new GET, no client state
        # carried over from visit 1 beyond what the server itself now holds.
        visit2 = client.get(f"/api/sections/{section}/session").json()
        remaining2 = sorted(
            r["number"]
            for r in visit2["latest_attempt"]["results"]
            if r["status"] != "correct"
        )
        assert remaining2 == [3, 4, 5]
        assert visit2["submission_count"] == 2
        assert visit2["session_id"] == visit1["session_id"] == base["session_id"]
