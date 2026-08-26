"""Independent, additional coverage for the "Per-workbook frequently-missed
filter tab with answer detail" chunk -- written on top of the
implementation's own additions to tests/test_sessions.py, in the same spirit
as tests/test_sessions_dal_more.py / tests/test_sessions_chunk_gaps.py /
tests/test_sessions_api_layer_gaps.py did for the prior chunks in this
series: close specific gaps rather than duplicate what the implementation's
own tests already assert.

This chunk's actual code change is `top_missed()` growing two new columns:
`expected` (an INNER JOIN onto the *live* answer_keys table) and `given` (a
correlated subquery for the most recent qualifying miss). The
implementation's own new assertions in tests/test_sessions.py compare
`top_missed`'s `expected` against the *submitting attempt's own* frozen
`results[...]["expected"]` snapshot -- which would pass identically even for
a regression that quietly read the frozen attempt_answers.expected column
instead of the live answer_keys table, since nothing in those tests ever
changes the key in between grading and querying stats. Angles targeted here
that the implementation's own tests never touch:

  * `expected` tracks the *live* answer_keys.answer_display, proven by
    actually editing the key (via the real re-import/overwrite endpoint)
    *after* the miss was recorded and observing `top_missed` follow the
    edit rather than stay pinned to what was frozen on the attempt -- while
    the old attempt's own frozen `results[...]["expected"]` stays exactly
    as it was, proving the two really are different columns now.
  * A once-missed number whose key entry is later deleted entirely (a
    re-import that narrows the section) is silently excluded from
    top_missed -- a direct, previously-nonexistent consequence of this
    chunk's new INNER JOIN -- rather than a 500 or a row with null fields.
  * `given` is the most recent *qualifying* (still-wrong) submission's
    answer, not simply the most recent attempt regardless of correctness:
    a later finished session that got the number right must not blank out
    or otherwise disturb the earlier finished session's still-frozen miss.
  * `given` surfaces over HTTP as `""` (never `null`/absent) for a
    qualifying miss that was left blank -- the exact falsy contract the
    detail modal's '(미응답)' fallback in app.js depends on.
"""

import pytest

DAY01 = "Day 01\n1. 3 2. 4 3. 1 4. 5 5. 2"
_ALL_CORRECT = {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"}


def _import(client, wid, raw_text=DAY01, resolutions=None):
    preview = client.post("/api/extract-text", json={"raw_text": raw_text}).json()
    entries = [
        {"number": e["number"], "answer": e["answer"], "line": e.get("line", 0)}
        for e in preview["entries"]
    ]
    payload = {
        "structure": "headers",
        "header_type": "day",
        "entries": entries,
        "headers": preview["headers"],
    }
    if resolutions is not None:
        payload["resolutions"] = resolutions
    return client.post(f"/api/workbooks/{wid}/sections/import", json=payload)


def _overwrite(client, wid, sid, raw_text):
    """Re-import raw_text onto the SAME section id, replacing its answer
    key set in place -- the real HTTP path behind `dal.replace_section_keys`
    (a corrected/re-extracted key), exercised via the same
    "Day 01" label + overwrite resolution the extraction UI itself sends."""
    return _import(
        client,
        wid,
        raw_text,
        resolutions=[
            {"incoming_label": "Day 01", "action": "overwrite", "target_section_id": sid}
        ],
    )


@pytest.fixture()
def wb(client):
    r = client.post("/api/workbooks", json={"title": "오답 상세 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture()
def section(client, wb):
    """A single 5-question section (Day 01), key {1:3, 2:4, 3:1, 4:5, 5:2}."""
    r = _import(client, wb)
    assert r.status_code == 201
    return r.json()["sections"][0]["id"]


class TestExpectedTracksLiveAnswerKey:
    """`expected` is documented (docs/API.md, app/db.py's own docstring) to
    read the *current* answer_keys row, not the value frozen on the attempt
    at grading time -- distinct enough behavior that it deserves its own
    proof, since every existing assertion on `expected` would hold just as
    well against the frozen column."""

    def test_expected_follows_a_key_correction_made_after_the_miss(
        self, client, wb, section
    ):
        att = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {**_ALL_CORRECT, "1": "9"}},
        ).json()
        assert att["results"][0]["expected"] == "3"  # the key as it stood at submit time
        client.post(f"/api/sessions/{att['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert top[0]["expected"] == "3"
        assert top[0]["given"] == "9"

        # Correct the key after the fact: Day 01's Q1 answer "3" -> "7".
        resp = _overwrite(client, wb, section, "Day 01\n1. 7 2. 4 3. 1 4. 5 5. 2")
        assert resp.status_code == 201

        top2 = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert len(top2) == 1
        assert top2[0]["expected"] == "7"  # follows the corrected key...
        assert top2[0]["given"] == "9"  # ...while the student's own wrong answer is untouched

        # The OLD attempt's own frozen snapshot must be untouched by the
        # correction -- proof top_missed reads answer_keys live, distinct
        # from this column, rather than the two having quietly become the
        # same value by coincidence.
        old = client.get(f"/api/attempts/{att['id']}").json()
        assert old["results"][0]["expected"] == "3"

    def test_omits_a_row_whose_key_entry_was_later_deleted(self, client, wb, section):
        att = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {**_ALL_CORRECT, "1": "9"}},
        ).json()
        client.post(f"/api/sessions/{att['session_id']}/finish")
        pre = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert [m["number"] for m in pre] == [1]  # sanity: the miss is there first

        # Re-import narrows the section to questions 2-5 -- number 1's
        # answer_keys row is gone entirely (DELETE + re-INSERT under the hood).
        resp = _overwrite(client, wb, section, "Day 01\n2. 4 3. 1 4. 5 5. 2")
        assert resp.status_code == 201
        assert resp.json()["sections"][0]["problem_count"] == 4

        stats = client.get(f"/api/workbooks/{wb}/stats")
        assert stats.status_code == 200  # the new INNER JOIN must not 500 on the missing match
        assert stats.json()["top_missed"] == []  # silently excluded, not surfaced with a null key


class TestGivenIsTheMostRecentQualifyingMissOnly:
    """`given`'s correlated subquery is filtered to `status != 'correct'`
    same as the outer query -- it must track the latest *wrong* first
    submission of a finished session, not merely the latest attempt id
    touching that number regardless of whether it was right."""

    def test_given_ignores_a_later_session_that_got_it_right(
        self, client, wb, section
    ):
        att1 = client.post(
            "/api/attempts",
            json={"section_id": section, "answers": {**_ALL_CORRECT, "1": "9"}},
        ).json()
        client.post(f"/api/sessions/{att1['session_id']}/finish")

        att2 = client.post(
            "/api/attempts", json={"section_id": section, "answers": _ALL_CORRECT}
        ).json()
        assert att2["score"] == 5  # this later session gets Q1 (and everything) right
        client.post(f"/api/sessions/{att2['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert len(top) == 1
        assert top[0]["count"] == 1  # the correct 2nd session isn't itself a miss...
        assert top[0]["given"] == "9"  # ...and `given` still reports the one real miss, not "3"

    def test_given_is_empty_string_not_null_for_a_blank_qualifying_miss(
        self, client, wb, section
    ):
        answers = dict(_ALL_CORRECT)
        del answers["1"]  # Q1 left unanswered entirely
        att = client.post(
            "/api/attempts", json={"section_id": section, "answers": answers}
        ).json()
        assert att["results"][0]["status"] == "unanswered"
        client.post(f"/api/sessions/{att['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wb}/stats").json()["top_missed"]
        assert len(top) == 1
        assert top[0]["given"] == ""
        assert top[0]["given"] is not None
