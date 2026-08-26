"""Independent, additional coverage for the "Session-aware submission,
finish, and history endpoints" chunk (api_layer) -- written on top of the
already-substantial coverage the implementation itself added to
tests/test_sessions.py, tests/test_api.py, tests/test_auth.py, and
tests/test_answered_only.py, in the same spirit as
tests/test_sessions_dal_more.py / tests/test_sessions_chunk_gaps.py did for
the prior (schema/DAL) chunk: close specific gaps rather than duplicate what
is already asserted.

Angles targeted here that the implementation's own tests never touch:
  * `session_finished` on the AttemptResult shape -- introduced by this
    chunk, never asserted anywhere else -- flips False -> True once the
    owning session is finished, observed through GET /attempts/{id}.
  * `is_full_attempt` / `merged_from` are genuinely gone from that shape,
    not merely unasserted by omission.
  * POST /attempts/from-misses is actually gone at the HTTP layer (only
    its *replacement* is exercised elsewhere), and FromMissesPayload /
    AttemptCreate.merge_attempt_id are gone at the schema layer -- plus a
    stray merge_attempt_id in a payload (an old cached frontend, since
    app.js itself isn't updated until chunk 4) must be silently ignored,
    not a 422.
  * GET /sections/{sid}/session 404s for a section that has *never* had any
    attempt at all -- existing coverage only exercises the "session existed
    then finished" 404 path.
  * GET/POST on the section-scoped session routes 404 for a section that
    doesn't exist at all (not just "no open session").
  * POST /sessions/{id}/finish is truly idempotent (byte-for-byte identical
    second response, not just "doesn't error") and 404s on an unknown id.
  * GET /sessions/{id} 404s for a *finished* session owned by a different
    device -- the cross-device auth test only ever exercises the list/
    attempt-fetch routes, never this detail route.
  * All four of this chunk's new routes reuse the project's bounded-int
    Path validator -- tests/test_api.py::test_huge_id_404_not_500 only ever
    exercised /workbooks and /attempts, never these.
  * db.py's new `open_session_id` column on list_sections, end-to-end
    through GET /workbooks/{wid} (null / real id / null again) and directly
    at the DAL level -- completely unexercised anywhere else, even though
    chunk 4 depends on it being correct.
  * `attempt_count` is genuinely absent from GET /workbooks/{wid}/stats'
    per-section dict -- the *router's* own hand-built dict, distinct from
    the DAL-level list_sections check test_sessions_dal_more.py already has.
  * compute_breakdown's `seq >= 3` arm of third_plus (a question actually
    corrected on the 3rd try) -- every existing third_plus assertion, at
    both the pure-function and HTTP layers, only ever exercises the "never
    correct" arm of that same bucket.
  * merge_answers' whitespace handling (a whitespace-only value is treated
    as blank on both sides of the merge) and that a stored value is kept
    verbatim (not re-stripped) -- edge cases the existing "verbatim port"
    unit tests don't reach.
  * top_missed actually working end-to-end once a session created by a real
    POST /attempts is *finished* through the real HTTP flow -- the only
    existing coverage for this is the two pre-existing, out-of-scope xfails
    in tests/test_sessions.py, which never call finish at all.
"""

import pytest

from app import db as dal
from app.services.sessions import compute_breakdown, merge_answers

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
    r = client.post("/api/workbooks", json={"title": "세션 API 계층 갭 테스트"})
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


# ---------------------------------------------------------------------------
# Pure-function edge cases: merge_answers.
# ---------------------------------------------------------------------------


class TestMergeAnswersWhitespaceEdgeCases:
    def test_whitespace_only_new_answer_retracts_like_explicit_blank(self):
        latest_results = [{"number": 1, "given": "3", "status": "correct"}]
        assert merge_answers(latest_results, {"1": "   "}) == {}

    def test_whitespace_only_given_in_latest_results_is_not_carried_forward(self):
        latest_results = [{"number": 1, "given": "   ", "status": "unanswered"}]
        assert merge_answers(latest_results, {}) == {}

    def test_new_answer_value_is_stored_unstripped_when_non_blank(self):
        """Only the truthiness check strips -- the stored value itself keeps
        its original (unstripped) form, exactly as the old inline
        _merged_answers did; downstream grading re-strips when reading it."""
        assert merge_answers([], {"1": " 4 "}) == {"1": " 4 "}


# ---------------------------------------------------------------------------
# Pure-function edge case: compute_breakdown's "seq >= 3" arm of third_plus,
# as distinct from its "never correct" arm (the only one exercised
# elsewhere).
# ---------------------------------------------------------------------------


class TestComputeBreakdownThirdTryActuallyCorrect:
    def test_third_submission_correct_lands_in_third_plus_via_seq_not_never(self):
        attempts = [
            {"submission_seq": 1, "results": [{"number": 1, "status": "incorrect"}]},
            {"submission_seq": 2, "results": [{"number": 1, "status": "incorrect"}]},
            {"submission_seq": 3, "results": [{"number": 1, "status": "correct"}]},
        ]
        out = compute_breakdown([1], attempts)
        assert out["third_plus"]["numbers"] == [1]
        assert out["first_try"]["numbers"] == []
        assert out["second_try"]["numbers"] == []

    def test_actually_third_try_and_never_correct_land_in_the_same_bucket(self):
        """third_plus is one bucket for two different causes (spec: seq>=3
        OR never correct) -- a genuinely-corrected-on-try-3 number and a
        never-correct number must land there side by side."""
        attempts = [
            {
                "submission_seq": 1,
                "results": [
                    {"number": 1, "status": "incorrect"},
                    {"number": 2, "status": "incorrect"},
                ],
            },
            {
                "submission_seq": 2,
                "results": [
                    {"number": 1, "status": "incorrect"},
                    {"number": 2, "status": "incorrect"},
                ],
            },
            {"submission_seq": 3, "results": [{"number": 1, "status": "correct"}]},
        ]
        out = compute_breakdown([1, 2], attempts)
        assert sorted(out["third_plus"]["numbers"]) == [1, 2]

    def test_first_correct_occurrence_wins_even_if_a_later_submission_reverts(self):
        """The spec records the seq of the *first* correct occurrence -- a
        number correct on try 1 must stay bucketed there even if a later
        submission's row for that same number (e.g. after an explicit
        retraction) is no longer 'correct'."""
        attempts = [
            {"submission_seq": 1, "results": [{"number": 1, "status": "correct"}]},
            {"submission_seq": 2, "results": [{"number": 1, "status": "unanswered"}]},
        ]
        out = compute_breakdown([1], attempts)
        assert out["first_try"]["numbers"] == [1]
        assert out["third_plus"]["numbers"] == []


# ---------------------------------------------------------------------------
# session_finished on the AttemptResult shape.
# ---------------------------------------------------------------------------


class TestSessionFinishedFlag:
    def test_flips_false_to_true_once_the_owning_session_is_finished(
        self, client, section
    ):
        att = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        ).json()
        assert att["session_finished"] is False
        assert client.get(f"/api/attempts/{att['id']}").json()["session_finished"] is False

        client.post(f"/api/sessions/{att['session_id']}/finish")

        refetched = client.get(f"/api/attempts/{att['id']}").json()
        assert refetched["session_finished"] is True

    def test_true_for_every_submission_in_a_now_finished_session_not_just_the_first(
        self, client, section
    ):
        base = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        ).json()
        retry = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"2": "4"}}
        ).json()
        assert retry["session_finished"] is False

        client.post(f"/api/sessions/{base['session_id']}/finish")

        assert client.get(f"/api/attempts/{base['id']}").json()["session_finished"] is True
        assert client.get(f"/api/attempts/{retry['id']}").json()["session_finished"] is True


class TestAttemptResultShapeDropsRetiredFields:
    def test_is_full_attempt_and_merged_from_are_gone(self, client, section):
        att = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        ).json()
        assert "is_full_attempt" not in att
        assert "merged_from" not in att

        fetched = client.get(f"/api/attempts/{att['id']}").json()
        assert "is_full_attempt" not in fetched
        assert "merged_from" not in fetched


# ---------------------------------------------------------------------------
# POST /attempts/from-misses and its schema/field are genuinely gone.
# ---------------------------------------------------------------------------


class TestFromMissesRemoved:
    def test_from_misses_route_gone(self, client, section):
        r = client.post(
            "/api/attempts/from-misses",
            json={"section_id": section, "base_attempt_id": 1},
        )
        assert r.status_code in (404, 405)

    def test_from_misses_payload_class_no_longer_exists(self):
        import app.schemas as schemas

        assert not hasattr(schemas, "FromMissesPayload")

    def test_merge_attempt_id_no_longer_a_declared_field(self):
        from app.schemas import AttemptCreate

        assert "merge_attempt_id" not in AttemptCreate.model_fields

    def test_stray_merge_attempt_id_in_payload_is_silently_ignored(self, client, section):
        """An old cached frontend (app.js itself isn't rewritten until chunk
        4) may still send merge_attempt_id -- it must not turn into a 422,
        and retry auto-detection must proceed exactly as if the field were
        never sent at all, off the open session alone."""
        base = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"1": "3", "2": "9"}},
        ).json()
        r = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"2": "4"},
                "merge_attempt_id": base["id"],
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["is_first_submission"] is False
        assert body["submission_seq"] == 2
        assert body["session_id"] == base["session_id"]
        by_num = {row["number"]: row for row in body["results"]}
        assert by_num[1]["status"] == "correct"  # preserved from submission 1
        assert by_num[2]["status"] == "correct"  # fixed by this retry


# ---------------------------------------------------------------------------
# GET /sections/{sid}/session and GET /sections/{sid}/sessions: unknown
# section, and a section that has genuinely never had any attempt.
# ---------------------------------------------------------------------------


class TestSessionRoutesUnknownSection404:
    def test_open_session_unknown_section_404(self, client):
        assert client.get("/api/sections/999999/session").status_code == 404

    def test_finished_sessions_list_unknown_section_404(self, client):
        assert client.get("/api/sections/999999/sessions").status_code == 404


class TestOpenSessionEndpointNeverAttempted:
    def test_404_for_a_freshly_imported_section_with_no_attempts_at_all(
        self, client, section
    ):
        assert client.get(f"/api/sections/{section}/session").status_code == 404
        # ... as opposed to the finished-history list, which is simply empty
        # rather than 404ing, for the same never-attempted section.
        assert client.get(f"/api/sections/{section}/sessions").json() == []


# ---------------------------------------------------------------------------
# POST /sessions/{id}/finish: true idempotency, unknown id 404.
# ---------------------------------------------------------------------------


class TestFinishSessionIdempotency:
    def test_second_finish_call_is_a_true_no_op(self, client, section):
        att = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        ).json()
        first = client.post(f"/api/sessions/{att['session_id']}/finish")
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["status"] == "finished"

        second = client.post(f"/api/sessions/{att['session_id']}/finish")
        assert second.status_code == 200
        assert second.json() == first_body  # identical, including finished_at

        hist = client.get(f"/api/sections/{section}/sessions").json()
        assert len(hist) == 1  # still exactly one finished session, not two

    def test_finish_unknown_session_404(self, client):
        assert client.post("/api/sessions/999999/finish").status_code == 404


# ---------------------------------------------------------------------------
# GET /sessions/{id}: unknown id, and cross-device isolation for a finished
# (as opposed to in-progress, already covered elsewhere) session.
# ---------------------------------------------------------------------------


class TestSessionDetailUnknownAndCrossDevice:
    def test_unknown_id_404(self, client):
        assert client.get("/api/sessions/999999").status_code == 404

    def test_finished_session_detail_404s_for_a_different_device(
        self, client, other_device_client, section
    ):
        att = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        ).json()
        client.post(f"/api/sessions/{att['session_id']}/finish")

        assert client.get(f"/api/sessions/{att['session_id']}").status_code == 200
        assert (
            other_device_client.get(f"/api/sessions/{att['session_id']}").status_code
            == 404
        )


# ---------------------------------------------------------------------------
# All four new routes reuse the project's bounded-int Path validator.
# ---------------------------------------------------------------------------


class TestSessionRoutesHugeIdValidation:
    HUGE = 999999999999999999999

    def test_get_open_session_huge_id_422(self, client):
        assert client.get(f"/api/sections/{self.HUGE}/session").status_code == 422

    def test_finish_huge_id_422(self, client):
        assert client.post(f"/api/sessions/{self.HUGE}/finish").status_code == 422

    def test_finished_sessions_list_huge_id_422(self, client):
        assert client.get(f"/api/sections/{self.HUGE}/sessions").status_code == 422

    def test_session_detail_huge_id_422(self, client):
        assert client.get(f"/api/sessions/{self.HUGE}").status_code == 422


# ---------------------------------------------------------------------------
# db.py's new open_session_id column on list_sections.
# ---------------------------------------------------------------------------


class TestOpenSessionIdOnWorkbookDetail:
    def test_null_then_open_id_then_null_again_after_finish(self, client, wb, section):
        def _section_row():
            detail = client.get(f"/api/workbooks/{wb}").json()
            return next(s for s in detail["sections"] if s["id"] == section)

        sec = _section_row()
        assert "open_session_id" in sec
        assert sec["open_session_id"] is None

        att = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        ).json()
        assert _section_row()["open_session_id"] == att["session_id"]

        client.post(f"/api/sessions/{att['session_id']}/finish")
        assert _section_row()["open_session_id"] is None  # finished isn't "open"

    def test_independent_per_section(self, client, two_sections):
        wid, s1, s2 = two_sections
        att1 = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "3"}}
        ).json()

        detail = client.get(f"/api/workbooks/{wid}").json()
        by_id = {s["id"]: s for s in detail["sections"]}
        assert by_id[s1]["open_session_id"] == att1["session_id"]
        assert by_id[s2]["open_session_id"] is None


class TestOpenSessionIdDalLevel:
    def test_list_sections_open_session_id_direct(self, client, wb, device_id):
        sid = _import_headers(client, wb).json()["sections"][0]["id"]
        conn = dal.connect()
        try:
            uid = dal.get_or_create_device_user(conn, device_id)["id"]
            secs = {s["id"]: s for s in dal.list_sections(conn, wb, uid)}
            assert secs[sid]["open_session_id"] is None

            session_id = dal.create_session(conn, uid, sid, 1, 1, 100.0)
            conn.commit()
            secs = {s["id"]: s for s in dal.list_sections(conn, wb, uid)}
            assert secs[sid]["open_session_id"] == session_id

            dal.finish_session(conn, session_id, uid)
            conn.commit()
            secs = {s["id"]: s for s in dal.list_sections(conn, wb, uid)}
            assert secs[sid]["open_session_id"] is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# attempt_count is genuinely absent from workbook_stats' own dict (the
# router's hand-built dict, not just the DAL layer).
# ---------------------------------------------------------------------------


class TestWorkbookStatsSessionCountRename:
    def test_attempt_count_absent_session_count_present(self, client, section, wb):
        client.post("/api/attempts", json={"section_id": section, "answers": {"1": "3"}})
        stats = client.get(f"/api/workbooks/{wb}/stats").json()
        sec = next(s for s in stats["sections"] if s["section_id"] == section)
        assert "session_count" in sec
        assert "attempt_count" not in sec


# ---------------------------------------------------------------------------
# End-to-end: a question actually corrected on the 3rd submission (as
# opposed to never answered) lands in third_plus, through three real
# POST /attempts rounds and a real GET /sessions/{id}.
# ---------------------------------------------------------------------------


class TestBreakdownThirdTryEndToEnd:
    def test_question_corrected_on_third_submission_lands_in_third_plus(
        self, client, section
    ):
        # Q1 correct immediately; Q2 wrong twice then fixed on the 3rd try;
        # Q3/4/5 never answered at all.
        client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"1": "3", "2": "9"}},
        )
        session_id = client.get(f"/api/sections/{section}/session").json()["session_id"]

        client.post("/api/attempts", json={"section_id": section, "answers": {"2": "8"}})
        third = client.post(
            "/api/attempts", json={"section_id": section, "answers": {"2": "4"}}
        ).json()
        assert third["submission_seq"] == 3
        assert {r["number"]: r["status"] for r in third["results"]}[2] == "correct"

        client.post(f"/api/sessions/{session_id}/finish")
        detail = client.get(f"/api/sessions/{session_id}").json()
        assert detail["submission_count"] == 3

        bd = detail["breakdown"]
        assert bd["first_try"]["numbers"] == [1]
        assert bd["second_try"]["numbers"] == []
        assert sorted(bd["third_plus"]["numbers"]) == [2, 3, 4, 5]
        assert bd["first_try"]["percent"] == 20.0
        assert bd["third_plus"]["percent"] == 80.0


# ---------------------------------------------------------------------------
# top_missed actually working end-to-end once a session is finished through
# the real HTTP flow -- the only existing coverage is the two pre-existing,
# out-of-scope xfails in tests/test_sessions.py, which never call finish.
# ---------------------------------------------------------------------------


# Every question but Q1 answered *correctly* on submission 1 -- top_missed
# counts any non-'correct' status (including 'unanswered'), so leaving Q2-5
# blank would itself put all five questions in `top_missed` and obscure the
# thing actually under test below.
_ALL_BUT_Q1_CORRECT = {"1": "9", "2": "4", "3": "1", "4": "5", "5": "2"}


class TestTopMissedWorksOnceSessionIsActuallyFinished:
    def test_reports_the_miss_only_after_finish_not_while_still_open(
        self, client, section, wb
    ):
        att = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": _ALL_BUT_Q1_CORRECT},
        ).json()

        assert client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"] == []

        client.post(f"/api/sessions/{att['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert len(top) == 1
        assert top[0]["number"] == 1
        assert top[0]["section_id"] == section
        assert top[0]["workbook_id"] == wb

    def test_reads_only_the_frozen_first_submission_not_a_later_fixed_retry(
        self, client, section, wb
    ):
        att = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": _ALL_BUT_Q1_CORRECT},
        ).json()  # only Q1 wrong on submission 1
        client.post(
            "/api/attempts", json={"section_id": section, "answers": {"1": "3"}}
        )  # retry fixes it on submission 2
        client.post(f"/api/sessions/{att['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert len(top) == 1
        assert top[0]["number"] == 1  # still counted as missed -- frozen to submission 1


# ---------------------------------------------------------------------------
# Bug fix: "unanswered questions leaking into selective-grading results".
# A number an answered_only first submission left unanswered was never
# actually graded (excluded from that submission's own `total`, exactly
# like it's excluded from the results screen's own breakdown/wrong-list in
# app.js) and must not appear in top_missed either -- covers both db.py's
# outer WHERE (`count`) and the correlated `given` subquery, which need the
# identical exclusion or they can disagree about the same row.
# ---------------------------------------------------------------------------


class TestTopMissedExcludesAnsweredOnlySkips:
    def test_answered_only_skipped_numbers_never_appear_in_top_missed(
        self, client, section, wb
    ):
        """Q1 answered and wrong, Q2 answered and correct, Q3-5 left blank
        under answered_only -- only Q1 (actually graded, actually wrong)
        may surface; Q3-5 were never graded and must be absent entirely,
        not merely uncounted."""
        att = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "9", "2": "4"},
                "answered_only": True,
            },
        ).json()
        assert att["total"] == 2  # narrowed: this submission IS answered_only-affected
        assert set(att["unanswered_numbers"]) == {3, 4, 5}
        client.post(f"/api/sessions/{att['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert [m["number"] for m in top] == [1]

    def test_answered_only_with_nothing_actually_skipped_is_unaffected(
        self, client, section, wb
    ):
        """answered_only with every question actually answered never
        narrows `total` at all (grade() only subtracts real skips) -- this
        must behave exactly as if answered_only had been off."""
        full_answers = {"1": "9", "2": "4", "3": "1", "4": "5", "5": "2"}
        att = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": full_answers, "answered_only": True},
        ).json()
        assert att["total"] == 5  # nothing narrowed -- nothing was left blank
        client.post(f"/api/sessions/{att['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert [m["number"] for m in top] == [1]

    def test_given_ignores_a_later_answered_only_skip_and_keeps_the_real_miss(
        self, client, section, wb
    ):
        """Two finished sessions touch the same number: session A actually
        answers it wrong (a real miss); a later session B (answered_only)
        leaves it unanswered entirely -- a higher attempt id, so it would
        win `given`'s own `ORDER BY a2.id DESC` if its row weren't excluded
        the same way `count`'s row is. Both clauses need the identical
        exclusion or `count` (still 1, from session A) and `given` (would
        wrongly go blank) disagree about the same row."""
        att_a = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": _ALL_BUT_Q1_CORRECT},
        ).json()  # Q1 wrong ("9"), rest correct
        client.post(f"/api/sessions/{att_a['session_id']}/finish")

        att_b = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"2": "4", "3": "1", "4": "5", "5": "2"},  # Q1 left blank
                "answered_only": True,
            },
        ).json()
        assert att_b["total"] == 4
        assert att_b["unanswered_numbers"] == [1]
        client.post(f"/api/sessions/{att_b['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert len(top) == 1
        assert top[0]["number"] == 1
        assert top[0]["count"] == 1  # only session A's real miss counts
        assert top[0]["given"] == "9"  # session A's real answer, not session B's blank
