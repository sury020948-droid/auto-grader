"""Independent, additional coverage for the "Hide skipped questions in retry
mode + scope breakdown to the answered-only subset" chunk.

That chunk is two related changes, both about questions an earlier
submission never answered (skipped) at all:

  (a) Frontend (app/static/js/app.js, viewSolve()): in retry/resume mode, a
      number the *latest* submission left 'unanswered' under answered_only
      grading (`latest.total < latest.results.length`) is now dropped from
      `remainingResults` entirely -- no `<li>`/`<input>` is rendered for it
      at all, vs. the old behavior of still rendering an empty input for it
      as a retry target. This half is client-only and has no DOM/JS runner
      in this repo (confirmed: no package.json, no *.test.js anywhere) --
      unreachable from this Python suite, exactly as
      tests/test_quiz_resume_screen_gaps.py's own docstring already notes
      for the identical reason. It can only be verified by reading app.js
      directly, which is how this change was confirmed correct: viewSolve's
      `narrowedByAnsweredOnly` filter removes `status === 'unanswered'` rows
      from `remainingResults` before `numbers`/`inputsHtml`/`allCaughtUp` are
      derived from it, and the `<input>` markup itself is only ever produced
      by mapping over `numbers` -- so a filtered-out number produces no
      element, not an empty one, and the submit handler only ever reads back
      DOM inputs that were actually rendered (`grid.querySelectorAll`), so
      nothing downstream references a hidden number either.

  (b) Backend (app/routers/sessions.py's read_session_detail(), calling
      app/services/sessions.py's compute_breakdown()): when the session's
      FIRST submission used answered_only and actually left some numbers
      unanswered, the whole number universe passed to compute_breakdown()
      -- and hence `total_questions` and every try-count bucket, not just
      `first_total` -- narrows to exactly the numbers that first submission
      answered. This half *is* backend/HTTP-testable and is what this file
      actually exercises.

The single-submission version of (b) -- one answered_only first submission,
nothing else -- is already covered by
tests/test_session_detail_view_gaps.py::
TestBreakdownDenominatorMatchesFirstTotalUnderAnsweredOnly (rewritten from
the old, now-reversed TestBreakdownDenominatorDivergesFromFirstTotalUnder
AnsweredOnly). This file closes the two multi-submission angles that
single-round test can't reach, both explicitly called out as deliberate
in read_session_detail()'s own comment but never previously exercised
end-to-end:

  * A number the first submission skipped, then genuinely answered --
    even correctly -- on a *later* retry within the same session, must
    stay excluded from the breakdown forever: the narrowed subset is frozen
    to round 1 and never grows, exactly mirroring how `first_total` itself
    is permanently frozen and never recomputed on a retry. Without this,
    such a number would leak back into a bucket (most likely first_try,
    since it was just answered correctly) the moment it's actually
    attempted, silently widening `total_questions` back past `first_total`.
  * The narrowing is keyed off the FIRST submission specifically, not off
    "any submission in the session" or "the latest submission" (which is
    what part (a)'s own client-side filter uses instead, deliberately
    differently -- see viewSolve()'s comment). An ordinary (non-
    answered_only) first submission that leaves numbers blank does NOT
    narrow the breakdown, even if a *later* retry in the same session
    turns on answered_only and genuinely narrows that retry's own `total`.

Both tests below hand-trace every bucket, not just total_questions, so a
regression that gets the denominator right but corrupts real-try-index
bookkeeping (e.g. by skipping the frozen-out numbers' rows entirely instead
of merely excluding them from the final bucket lists) would still be
caught.
"""

import pytest

from app.services.sessions import compute_breakdown

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
    r = client.post("/api/workbooks", json={"title": "재도전 숨김 + 집계 범위 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def section(client, wb):
    """A single 5-question section (Day 01), key {1:3, 2:4, 3:1, 4:5, 5:2}."""
    return _import_headers(client, wb).json()["sections"][0]["id"]


class TestBreakdownStaysFrozenAcrossLaterRetries:
    def test_a_number_answered_correctly_on_retry_stays_excluded_from_every_bucket(
        self, client, section
    ):
        """Round 1 (answered_only): Q1 correct, Q2 wrong, Q3/4/5 never
        touched -- narrows to the {1, 2} subset, same as the single-round
        test in test_session_detail_view_gaps.py. Round 2 then goes back
        and answers Q2/3/4/5 -- ALL correctly this time, including the three
        numbers round 1 never even saw. Despite that fresh, genuinely
        correct real-try data existing for Q3/4/5 in the raw attempts list
        compute_breakdown() scans, they must never surface in
        total_questions or in ANY bucket (not even as a third_plus
        leftover) -- fully absent from this session's aggregated data,
        exactly per spec, because the answered-subset is frozen to round 1
        and never grows."""
        base = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"1": "3", "2": "9"},
                "answered_only": True,
            },
        ).json()
        assert base["total"] == 2
        assert base["is_first_submission"] is True

        retry = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                # Fixes Q2 AND genuinely answers Q3/4/5 for the first time,
                # all correctly.
                "answers": {"2": "4", "3": "1", "4": "5", "5": "2"},
            },
        ).json()
        assert retry["session_id"] == base["session_id"]
        assert retry["is_first_submission"] is False
        assert retry["score"] == 5
        assert retry["total"] == 5  # this round wasn't answered_only-narrowed

        client.post(f"/api/sessions/{base['session_id']}/finish")
        detail = client.get(f"/api/sessions/{base['session_id']}").json()

        # first_* stays frozen to round 1's own narrowed 2 -- untouched by
        # round 2 answering the rest.
        assert detail["first_total"] == 2
        assert detail["first_score"] == 1
        assert detail["first_percent"] == 50.0

        bd = detail["breakdown"]
        assert bd["total_questions"] == 2 == detail["first_total"]

        # Q1: correct from real try 1 (round 1) -> first_try.
        # Q2: wrong on real try 1 (round 1), correct on real try 2 (round 2)
        #     -> second_try.
        assert bd["first_try"]["numbers"] == [1]
        assert bd["second_try"]["numbers"] == [2]
        assert bd["third_plus"]["numbers"] == []

        # Q3/4/5 must not appear ANYWHERE in the breakdown, despite being
        # correctly answered in round 2's real, stored results.
        every_bucketed_number = (
            bd["first_try"]["numbers"]
            + bd["second_try"]["numbers"]
            + bd["third_plus"]["numbers"]
        )
        assert 3 not in every_bucketed_number
        assert 4 not in every_bucketed_number
        assert 5 not in every_bucketed_number

        assert bd["first_try"]["percent"] == 50.0  # 1 / 2
        assert bd["second_try"]["percent"] == 50.0  # 1 / 2
        assert bd["third_plus"]["percent"] == 0.0

        # The wrong-answer review list is untouched by any of this -- still
        # every key number from round 1's own snapshot.
        assert len(detail["first_results"]) == 5


class TestBreakdownNarrowingKeyedOnlyOffFirstSubmission:
    def test_answered_only_narrowing_on_a_later_retry_does_not_narrow_breakdown(
        self, client, section
    ):
        """Round 1 is an ORDINARY (non-answered_only) submission that
        happens to leave Q3/4/5 blank -- `total` stays the full 5 (blanks
        just count as 'unanswered' misses, nothing narrows), so this
        session's frozen first_total is 5, not 2. Round 2 then turns
        answered_only ON and only actually answers Q3 (leaving Q4/5 still
        blank), which genuinely narrows THAT round's own `total` to 3 -- but
        that narrowing must have zero effect on the breakdown, since
        read_session_detail() only ever inspects the FIRST submission's own
        total/results to decide whether to narrow, never the latest or any
        other round's."""
        base = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {"1": "3", "2": "9"}},
        ).json()  # Q3/4/5 left blank, ordinary grading
        assert base["total"] == 5
        assert base["is_first_submission"] is True

        retry = client.post(
            "/api/attempts",
            json={
                "section_id": section,
                "answers": {"3": "1"},  # only Q3 answered this round
                "answered_only": True,
            },
        ).json()
        assert retry["session_id"] == base["session_id"]
        assert retry["is_first_submission"] is False
        assert retry["total"] == 3  # THIS round's own total IS narrowed...

        client.post(f"/api/sessions/{base['session_id']}/finish")
        detail = client.get(f"/api/sessions/{base['session_id']}").json()

        # ...but first_total (frozen to round 1, which was never narrowed)
        # and breakdown.total_questions both stay at the section's full 5.
        assert detail["first_total"] == 5
        assert detail["breakdown"]["total_questions"] == 5

        bd = detail["breakdown"]
        # Q1: correct from real try 1 -> first_try.
        # Q3: unanswered (not a real try) in round 1, correct on its first
        #     real try in round 2 -> first_try too (real-try index, not raw
        #     submission_seq).
        assert sorted(bd["first_try"]["numbers"]) == [1, 3]
        assert bd["second_try"]["numbers"] == []
        # Q2 wrong and never retried; Q4/Q5 never answered in any round.
        assert sorted(bd["third_plus"]["numbers"]) == [2, 4, 5]
        assert bd["first_try"]["count"] == 2
        assert bd["first_try"]["percent"] == 40.0  # 2 / 5
        assert bd["third_plus"]["percent"] == 60.0  # 3 / 5


class TestComputeBreakdownIgnoresNumbersOutsideAllNumbers:
    """Pure-function-level companion to the two HTTP tests above: pins
    compute_breakdown()'s own (unchanged) contract that now matters in a new
    way -- `all_numbers` is the caller's entire choice of universe, and a
    number with real, even 'correct', data in `attempts` but absent from
    `all_numbers` must never surface in any output bucket. This is what lets
    read_session_detail() (routers/sessions.py) safely pass a narrowed
    subset and trust the excluded numbers vanish completely, independent of
    the HTTP/DB round trip the two tests above exercise."""

    def test_a_correct_number_outside_all_numbers_never_appears_in_any_bucket(self):
        all_numbers = [1, 2]
        attempts = [
            {
                "submission_seq": 1,
                "results": [
                    {"number": 1, "status": "correct"},
                    {"number": 2, "status": "incorrect"},
                    # Number 3 has real, correct data but is NOT part of
                    # this breakdown's universe.
                    {"number": 3, "status": "correct"},
                ],
            }
        ]
        out = compute_breakdown(all_numbers, attempts)
        assert out["total_questions"] == 2
        assert out["first_try"]["numbers"] == [1]
        assert out["second_try"]["numbers"] == []
        assert out["third_plus"]["numbers"] == [2]
        # Every bucket's numbers, combined, must total exactly len(all_numbers)
        # -- number 3 contributes to none of them.
        combined = (
            out["first_try"]["numbers"]
            + out["second_try"]["numbers"]
            + out["third_plus"]["numbers"]
        )
        assert sorted(combined) == [1, 2]
        assert (
            out["first_try"]["count"]
            + out["second_try"]["count"]
            + out["third_plus"]["count"]
            == 2
        )
