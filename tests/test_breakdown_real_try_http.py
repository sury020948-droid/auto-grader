"""HTTP/DB round-trip regression coverage for compute_breakdown's real-try-
index semantics (interpretation B: a question's 1st/2nd/3rd+ bucket is
driven by how many times it was actually given a real, non-blank answer,
not by the raw submission_seq of the round it happened to land in).

Every existing test touching this distinction -- both
TestComputeBreakdownRealTryIndexNotRawSeq in test_sessions_api_layer_gaps.py
and the renamed test in test_sessions.py -- calls compute_breakdown()
directly with hand-built `attempts` lists. None of them prove the actual
end-to-end pipeline (POST /attempts -> merge_answers -> grade() ->
dal.create_attempt -> dal.list_session_attempts -> GET /sessions/{id}'s
compute_breakdown() call) produces the same 'unanswered'-skips-a-real-try
result on a genuine skip-then-answer-later retry flow through the real
database. That's the gap this file closes: the two scenarios interpretation
A and B disagree on, driven through real HTTP retries against a real
section, exactly as the quiz screen would produce them.
"""

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


def _section(client):
    """A single 5-question section (Day 01), key {1:3, 2:4, 3:1, 4:5, 5:2}."""
    r = client.post("/api/workbooks", json={"title": "real-try-index http test"})
    wid = r.json()["id"]
    return _import_headers(client, wid).json()["sections"][0]["id"]


class TestBreakdownRealTryIndexOverRealRetryFlow:
    def test_skipped_then_immediately_correct_is_first_try_not_second(self, client):
        """Q2 is left blank on round 1 (only Q1 is submitted), then
        answered correctly the very first time it's actually attempted, on
        round 2. Under interpretation A this would land in second_try
        (first correct at raw submission_seq 2); under B it belongs in
        first_try, since round 2 is the first round Q2 ever got a real
        answer at all."""
        sid = _section(client)

        base = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"1": "3"}},  # Q2-5 skipped
        ).json()
        assert base["unanswered_numbers"] == [2, 3, 4, 5]

        retry = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"2": "4"}},  # Q2's first real try
        ).json()
        assert retry["session_id"] == base["session_id"]
        assert retry["submission_seq"] == 2
        by_num = {r["number"]: r for r in retry["results"]}
        assert by_num[2]["status"] == "correct"

        client.post(f"/api/sessions/{base['session_id']}/finish")
        detail = client.get(f"/api/sessions/{base['session_id']}").json()

        bd = detail["breakdown"]
        assert bd["total_questions"] == 5
        assert sorted(bd["first_try"]["numbers"]) == [1, 2]
        assert bd["second_try"]["numbers"] == []
        # Q3/4/5 never answered in any round -> still third_plus.
        assert sorted(bd["third_plus"]["numbers"]) == [3, 4, 5]

    def test_skipped_then_wrong_then_correct_is_second_try_not_third_plus(
        self, client
    ):
        """Q2: unanswered in round 1 (skipped), wrong on its first real try
        (round 2), correct on its second real try (round 3). Under
        interpretation A this would land in third_plus (first correct at
        raw submission_seq 3); under B it belongs in second_try, since
        round 3 is only Q2's *second* real answer attempt."""
        sid = _section(client)

        base = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"1": "3"}},  # Q2-5 skipped
        ).json()

        wrong = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"2": "9"}},  # Q2 wrong, real try 1
        ).json()
        assert wrong["submission_seq"] == 2
        by_num = {r["number"]: r for r in wrong["results"]}
        assert by_num[2]["status"] == "incorrect"

        fixed = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"2": "4"}},  # Q2 correct, real try 2
        ).json()
        assert fixed["submission_seq"] == 3
        by_num = {r["number"]: r for r in fixed["results"]}
        assert by_num[2]["status"] == "correct"
        # Q1 was carried forward untouched into every round -- still
        # correct, just not exercised by this test's own assertions below.

        client.post(f"/api/sessions/{base['session_id']}/finish")
        detail = client.get(f"/api/sessions/{base['session_id']}").json()

        bd = detail["breakdown"]
        assert bd["total_questions"] == 5
        assert bd["first_try"]["numbers"] == [1]  # Q1 correct every round from try 1
        assert bd["second_try"]["numbers"] == [2]
        assert sorted(bd["third_plus"]["numbers"]) == [3, 4, 5]
        assert bd["second_try"]["count"] == 1
        assert bd["second_try"]["percent"] == 20.0
