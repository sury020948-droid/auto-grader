"""Integration tests: retry merge, granular session deletion, duplicates."""

import pytest

from app.services.conflicts import detect_conflicts, labels_related, normalize_label
from app.services.sessions import compute_breakdown, merge_answers

DAY_SAMPLE = (
    "Day 01\n1. 3 2. 4 3. 1 4. 5 5. 2\n"
    "Day 02\n1. 2 2. 3 3. 4 4. 1 5. 5"
)

@pytest.fixture()
def two_sections(client):
    r = client.post("/api/workbooks", json={"title": "세션 관리 테스트"})
    wid = r.json()["id"]
    preview = client.post("/api/extract-text", json={"raw_text": DAY_SAMPLE}).json()
    body = {
        "structure": "headers",
        "header_type": "day",
        "entries": [
            {"number": e["number"], "answer": e["answer"], "line": e["line"]}
            for e in preview["entries"]
        ],
        "headers": preview["headers"],
    }
    secs = client.post(f"/api/workbooks/{wid}/sections/import", json=body).json()[
        "sections"
    ]
    return wid, secs[0]["id"], secs[1]["id"]


class TestRetryMerge:
    def test_retry_preserves_previous_correct_answers(self, client, two_sections):
        _, s1, _ = two_sections

        base = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},
            },
        ).json()
        assert base["score"] == 3
        assert set(base["wrong_numbers"]) == {2}
        assert set(base["unanswered_numbers"]) == {5}
        assert base["is_first_submission"] is True
        assert base["submission_seq"] == 1

        # The quiz screen derives which numbers still need retrying from the
        # open session's latest_attempt (status != correct) -- no separate
        # from-misses call needed any more.
        open_sess = client.get(f"/api/sections/{s1}/session").json()
        assert open_sess["session_id"] == base["session_id"]
        retry_numbers = [
            r["number"]
            for r in open_sess["latest_attempt"]["results"]
            if r["status"] != "correct"
        ]
        assert sorted(retry_numbers) == [2, 5]

        # A second POST to the same still-open section is auto-detected as
        # a retry -- no merge_attempt_id needed.
        retry = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"2": "④"},  # only the re-attempted question
            },
        )
        assert retry.status_code == 201
        merged = retry.json()

        assert merged["total"] == 5
        assert merged["score"] == 4  # Q1/Q3/Q4 preserved + fixed Q2; Q5 untouched
        by_num = {r["number"]: r for r in merged["results"]}
        assert by_num[1]["given"] == "3" and by_num[1]["status"] == "correct"
        assert by_num[3]["given"] == "1" and by_num[3]["status"] == "correct"
        assert by_num[4]["given"] == "5" and by_num[4]["status"] == "correct"
        assert by_num[2]["given"] == "④" and by_num[2]["status"] == "correct"
        assert by_num[5]["status"] == "unanswered"
        assert merged["is_first_submission"] is False
        assert merged["submission_seq"] == 2
        assert merged["session_id"] == base["session_id"]  # same session

        old = client.get(f"/api/attempts/{base['id']}").json()
        assert old["score"] == 3  # history untouched

    def test_blank_retry_answer_retracts(self, client, two_sections):
        _, s1, _ = two_sections
        client.post(
            "/api/attempts",
            json={"section_id": s1, "answers": {"1": "3", "2": "9"}},
        )
        merged = client.post(
            "/api/attempts",
            json={"section_id": s1, "answers": {"2": ""}},
        ).json()
        by_num = {r["number"]: r["status"] for r in merged["results"]}
        assert by_num[1] == "correct"
        assert by_num[2] == "unanswered"

    def test_retry_detection_is_scoped_per_section(self, client, two_sections):
        """An open session in one section must never be picked up as 'the
        latest submission to merge onto' by a POST to a different section --
        each section's retry detection is independent."""
        _, s1, s2 = two_sections
        client.post("/api/attempts", json={"section_id": s1, "answers": {"1": "3"}})

        first_s2 = client.post(
            "/api/attempts", json={"section_id": s2, "answers": {"1": "9"}}
        ).json()
        assert first_s2["is_first_submission"] is True
        assert first_s2["submission_seq"] == 1
        by_num = {r["number"]: r for r in first_s2["results"]}
        # If s1's answers had leaked in via a wrongly-shared session, Q1
        # would come back merged from there instead of this section's own.
        assert by_num[1]["given"] == "9"

    def test_retry_auto_detected_and_first_submission_score_stays_frozen(
        self, client, two_sections
    ):
        """A second POST /attempts to the same still-open section is
        auto-detected as a retry (no merge_attempt_id needed): it's saved
        as submission_seq=2 within the *same* session and regrades the full
        merged answer set -- but the session's frozen first_score/
        first_total/first_percent, and everything history/aggregates read
        from, stay exactly the first submission's, even after the retry
        improves on it. Supersedes the old is_full_attempt-based "partial
        retries are excluded from aggregates" mechanism with a different
        one: aggregates read a frozen score, not a filtered attempt set."""
        wid, s1, _ = two_sections

        base = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},
            },
        ).json()
        assert base["score"] == 3
        assert base["percent"] == 60.0
        assert base["is_first_submission"] is True
        session_id = base["session_id"]
        assert session_id is not None

        # Retry fixes both remaining misses (Q2, Q5) -> a perfect 5/5, 100%.
        retry = client.post(
            "/api/attempts",
            json={"section_id": s1, "answers": {"2": "4", "5": "2"}},
        ).json()
        assert retry["score"] == 5
        assert retry["percent"] == 100.0
        assert retry["is_first_submission"] is False
        assert retry["submission_seq"] == 2
        assert retry["session_id"] == session_id  # same session, not a new one

        # While the session stays open, it doesn't show up in finished
        # history or aggregates at all yet -- not even under the frozen
        # first score.
        assert client.get(f"/api/sections/{s1}/sessions").json() == []
        stats_open = client.get(f"/api/workbooks/{wid}/stats").json()
        sec_open = next(s for s in stats_open["sections"] if s["section_id"] == s1)
        assert sec_open["session_count"] == 0
        assert sec_open["latest_percent"] is None

        finish = client.post(f"/api/sessions/{session_id}/finish")
        assert finish.status_code == 200
        assert finish.json()["first_percent"] == 60.0  # frozen at the FIRST submission

        # (a) section session-history list now shows exactly one finished
        # session, carrying the frozen first-submission score -- not the
        # retry's 100%.
        hist_after = client.get(f"/api/sections/{s1}/sessions").json()
        assert len(hist_after) == 1
        assert hist_after[0]["first_percent"] == 60.0

        # (b) section + workbook aggregates read the same frozen score, even
        # though the retry's own percent (100%) beats it -- best/latest
        # must NOT silently move off the back of a retry within one session.
        stats_after = client.get(f"/api/workbooks/{wid}/stats").json()
        sec_after = next(s for s in stats_after["sections"] if s["section_id"] == s1)
        assert sec_after["session_count"] == 1
        assert sec_after["latest_percent"] == sec_after["best_percent"] == 60.0

        wb_after = client.get(f"/api/workbooks/{wid}").json()
        assert wb_after["latest_percent"] == 60.0

        # (c) the retry submission itself stays fully persisted and
        # fetchable by id -- it's frozen out of the aggregate, not dropped.
        fetched = client.get(f"/api/attempts/{retry['id']}").json()
        assert fetched["percent"] == 100.0
        assert fetched["is_first_submission"] is False


class TestSectionDeletion:
    def test_delete_only_target_session(self, client, two_sections):
        wid, s1, s2 = two_sections
        a1 = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "3"}}
        ).json()
        client.post("/api/attempts", json={"section_id": s2, "answers": {"1": "2"}})
        client.post(f"/api/sessions/{a1['session_id']}/finish")

        r = client.delete(f"/api/sections/{s2}")
        assert r.status_code == 204

        detail = client.get(f"/api/workbooks/{wid}").json()
        assert [s["id"] for s in detail["sections"]] == [s1]
        assert detail["problem_count"] == 5

        assert client.get(f"/api/sections/{s2}").status_code == 404
        assert client.get(f"/api/sections/{s1}").status_code == 200

        hist = client.get(f"/api/sections/{s1}/sessions").json()
        assert len(hist) == 1  # sibling data intact

        stats = client.get(f"/api/workbooks/{wid}/stats").json()
        assert [s["section_id"] for s in stats["sections"]] == [s1]
        assert all(m["section_label"] == "Day 01" for m in stats["top_missed"])

    def test_delete_missing_section_404(self, client, two_sections):
        assert client.delete("/api/sections/999999").status_code == 404


class TestSessionDetailEndpoint:
    def test_open_session_404_from_detail_endpoint(self, client, two_sections):
        """GET /sessions/{id} is deliberately disjoint from GET
        /sections/{sid}/session -- an in-progress session's own id 404s
        here even though it exists and is owned by the caller."""
        _, s1, _ = two_sections
        att = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "3"}}
        ).json()
        assert client.get(f"/api/sessions/{att['session_id']}").status_code == 404

    def test_finished_session_detail_has_breakdown_and_first_results(
        self, client, two_sections
    ):
        _, s1, _ = two_sections
        base = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"1": "3", "2": "9", "3": "1", "4": "5"},
            },
        ).json()  # Q1/3/4 correct on try 1; Q2 wrong; Q5 unanswered
        client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"2": "4"}}
        )  # Q2 fixed on try 2; Q5 still never answered
        client.post(f"/api/sessions/{base['session_id']}/finish")

        detail = client.get(f"/api/sessions/{base['session_id']}").json()
        assert detail["status"] == "finished"
        assert detail["session_id"] == base["session_id"]
        assert detail["submission_count"] == 2
        assert detail["first_percent"] == base["percent"]
        assert detail["first_results"] == base["results"]

        bd = detail["breakdown"]
        assert bd["total_questions"] == 5
        assert sorted(bd["first_try"]["numbers"]) == [1, 3, 4]
        assert bd["second_try"]["numbers"] == [2]
        assert bd["third_plus"]["numbers"] == [5]  # never answered correctly at all
        assert bd["third_plus"]["count"] == 1
        assert bd["first_try"]["percent"] == 60.0

    def test_history_entry_click_through_matches_detail_endpoint(
        self, client, two_sections
    ):
        """A history list entry (GET .../sessions) and its click-through
        detail (GET /sessions/{id}) must agree on the session's identity
        and frozen score."""
        _, s1, _ = two_sections
        base = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "3"}}
        ).json()
        client.post(f"/api/sessions/{base['session_id']}/finish")

        [entry] = client.get(f"/api/sections/{s1}/sessions").json()
        detail = client.get(f"/api/sessions/{entry['session_id']}").json()
        assert detail["session_id"] == entry["session_id"]
        assert detail["first_percent"] == entry["first_percent"]


class TestTopMissedWorkbookScoping:
    """Regression: `top_missed` must be scoped to the requested workbook only —
    the underlying query used to ignore `wid` and mix in misses from every
    workbook the caller owns."""

    def test_top_missed_excludes_other_workbooks(self, client, two_sections):
        wid, s1, _ = two_sections
        att = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "9"}}
        ).json()
        client.post(f"/api/sessions/{att['session_id']}/finish")

        other_wid = client.post(
            "/api/workbooks", json={"title": "다른 워크북"}
        ).json()["id"]
        preview = client.post("/api/extract-text", json={"raw_text": DAY_SAMPLE}).json()
        other_secs = client.post(
            f"/api/workbooks/{other_wid}/sections/import",
            json={
                "structure": "headers",
                "header_type": "day",
                "entries": [
                    {"number": e["number"], "answer": e["answer"], "line": e["line"]}
                    for e in preview["entries"]
                ],
                "headers": preview["headers"],
            },
        ).json()["sections"]
        other_sid = other_secs[0]["id"]
        other_att = client.post(
            "/api/attempts", json={"section_id": other_sid, "answers": {"1": "9"}}
        ).json()
        client.post(f"/api/sessions/{other_att['session_id']}/finish")

        stats = client.get(f"/api/workbooks/{wid}/stats").json()
        top = stats["top_missed"]
        assert top  # workbook A has its own miss on Q1
        assert all(t["workbook_id"] == wid for t in top)
        assert all(t["section_id"] != other_sid for t in top)
        assert top[0]["given"] == "9"

        other_stats = client.get(f"/api/workbooks/{other_wid}/stats").json()
        other_top = other_stats["top_missed"]
        assert other_top  # workbook B has its own (separate) miss on Q1
        assert all(t["workbook_id"] == other_wid for t in other_top)
        assert all(t["section_id"] != s1 for t in other_top)
        assert other_top[0]["given"] == "9"

    def test_top_missed_attributes_each_entry_to_its_own_section(
        self, client, two_sections
    ):
        """Two sections *of the same workbook* missing the same question number
        must stay two distinct rows (grouped by section, not merged), each
        carrying its own section_id/section_label alongside the shared
        workbook_id/workbook_title -- this per-row attribution is what the
        detail modal renders."""
        wid, s1, s2 = two_sections  # Day 01, Day 02 -- see DAY_SAMPLE above

        # one miss on Q2 in Day 01 (correct answer there is "4")
        att1 = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"2": "9"}}
        ).json()
        client.post(f"/api/sessions/{att1['session_id']}/finish")
        # two separate misses on Q2 in Day 02 (correct answer there is "3"),
        # each its own finished session -- a same-session retry would NOT
        # double the count, since only a session's first submission is ever
        # counted (see test_top_missed_excludes_a_miss_introduced_only_by_a_retry
        # below), so two independent sessions are needed to reach count=2.
        att2a = client.post(
            "/api/attempts", json={"section_id": s2, "answers": {"2": "9"}}
        ).json()
        client.post(f"/api/sessions/{att2a['session_id']}/finish")
        att2b = client.post(
            "/api/attempts", json={"section_id": s2, "answers": {"2": "8"}}
        ).json()
        client.post(f"/api/sessions/{att2b['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wid}/stats").json()["top_missed"]
        q2_rows = [t for t in top if t["number"] == 2]
        assert len(q2_rows) == 2  # kept separate per section, not collapsed together

        by_section = {t["section_id"]: t for t in q2_rows}
        assert set(by_section) == {s1, s2}
        assert by_section[s1]["count"] == 1
        assert by_section[s2]["count"] == 2
        assert by_section[s1]["section_label"] == "Day 01"
        assert by_section[s2]["section_label"] == "Day 02"

        # `given`/`expected` ride along per row: `given` is the student's own
        # verbatim wrong answer -- s2's is "8", the *most recent* (highest
        # attempts.id) of its two qualifying misses ("9" then "8"), not the
        # first. `expected` is the section's actual answer-key display for
        # that number, which genuinely differs between the two sections.
        assert by_section[s1]["given"] == "9"
        assert by_section[s2]["given"] == "8"
        exp_s1 = next(r["expected"] for r in att1["results"] if r["number"] == 2)
        exp_s2 = next(r["expected"] for r in att2a["results"] if r["number"] == 2)
        assert by_section[s1]["expected"] == exp_s1
        assert by_section[s2]["expected"] == exp_s2
        assert exp_s1 != exp_s2  # Day 01's Q2 key ("4") differs from Day 02's ("3")

        # same workbook throughout -- only the section differs
        assert by_section[s1]["workbook_id"] == wid
        assert by_section[s2]["workbook_id"] == wid
        assert (
            by_section[s1]["workbook_title"]
            == by_section[s2]["workbook_title"]
            == "세션 관리 테스트"
        )

        # ORDER BY count DESC, number: Day 02's row (count=2) sorts before
        # Day 01's (count=1), confirming the new columns ride along correctly
        # rather than the grouping/order being disturbed by the extra joins.
        assert top.index(by_section[s2]) < top.index(by_section[s1])

    def test_top_missed_excludes_a_miss_introduced_only_by_a_retry(
        self, client, two_sections
    ):
        """The session-model equivalent of the old is_full_attempt guarantee
        (a partial retry never counts toward aggregates): top_missed only
        ever reads a session's frozen FIRST submission. A wrong answer that
        exists only on a retry -- even one that overwrites a number the
        first submission had *correct* -- must never surface, since the
        row it would need (is_first_submission=1 for that number) was never
        wrong in the first place."""
        wid, s1, _ = two_sections
        base = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"},
            },
        ).json()
        assert base["score"] == 5  # everything correct on submission 1

        # Retry deliberately overwrites the already-correct Q1 with a wrong
        # answer -- allowed (a retry can freely re-edit any number), but
        # this mistake only ever exists in submission 2, never submission 1.
        retry = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "9"}}
        ).json()
        assert retry["results"][0]["status"] == "incorrect"  # Q1 wrong on try 2 only
        client.post(f"/api/sessions/{base['session_id']}/finish")

        top = client.get(f"/api/workbooks/{wid}/stats").json()["top_missed"]
        assert top == []  # the retry-only mistake never surfaces here


class TestConflictDetectionApi:
    def _incoming_headers(self, labels_lines):
        """Build a headers-mode import payload from [(label, line), ...]."""
        entries, headers = [], []
        line = 0
        for label in labels_lines:
            headers.append({"type": "day", "label": label, "index": line, "line": line})
            for n in (1, 2, 3):
                entries.append({"number": n, "answer": str(n % 5 or 5), "line": line})
                line += 1
        return {
            "structure": "headers",
            "header_type": "day",
            "entries": entries,
            "headers": headers,
        }

    def test_same_label_and_overlap_flagged(self, client, two_sections):
        wid, _, _ = two_sections
        payload = self._incoming_headers(["Day 01"])
        r = client.post(f"/api/workbooks/{wid}/sections/conflicts", json=payload)
        conflicts = r.json()["conflicts"]
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c["same_label"] is True
        assert c["existing_section"]["label"] == "Day 01"
        assert set(c["overlapping_numbers"]) == {1, 2, 3}

    def test_related_but_distinct_labels_no_conflict(self, client, two_sections):
        wid, _, _ = two_sections
        payload = self._incoming_headers(["Day 03"])
        r = client.post(f"/api/workbooks/{wid}/sections/conflicts", json=payload)
        assert r.json()["conflicts"] == []

    def test_case_insensitive_match(self, client, two_sections):
        wid, _, _ = two_sections
        payload = self._incoming_headers(["DAY 01"])
        conflicts = client.post(
            f"/api/workbooks/{wid}/sections/conflicts", json=payload
        ).json()["conflicts"]
        assert len(conflicts) == 1

    def test_disjoint_numbers_related_label_no_conflict(self, client, two_sections):
        wid, _, _ = two_sections
        payload = self._incoming_headers(["Day 01 - 보충"])  # related, not equal
        payload["entries"] = [
            {**e, "number": e["number"] + 50} for e in payload["entries"]
        ]
        conflicts = client.post(
            f"/api/workbooks/{wid}/sections/conflicts", json=payload
        ).json()["conflicts"]
        assert conflicts == []

    def test_equal_label_flags_even_with_disjoint_numbers(self, client, two_sections):
        """Matching identifier alone is enough — forces a conscious choice."""
        wid, _, _ = two_sections
        payload = self._incoming_headers(["Day 01"])
        payload["entries"] = [
            {**e, "number": e["number"] + 50} for e in payload["entries"]
        ]
        conflicts = client.post(
            f"/api/workbooks/{wid}/sections/conflicts", json=payload
        ).json()["conflicts"]
        assert len(conflicts) == 1
        assert conflicts[0]["same_label"] is True
        assert conflicts[0]["overlapping_numbers"] == []


class TestImportResolutions:
    def _payload_with_two_groups(self):
        entries, headers = [], []
        for li, label in enumerate(["Day 01", "Day 02"]):
            headers.append({"type": "day", "label": label, "index": li, "line": li * 2})
            for n in (1, 2):
                entries.append({"number": n, "answer": str(n + li), "line": li * 2 + 0})
        return {
            "structure": "headers",
            "header_type": "day",
            "entries": entries,
            "headers": headers,
        }

    def test_overwrite_replaces_in_place(self, client, two_sections):
        wid, s1, _ = two_sections
        payload = self._payload_with_two_groups()
        # Only the Day 01 group: overwrite existing section s1 with new answers.
        payload["entries"] = [e for e in payload["entries"] if e["line"] < 2]
        payload["headers"] = payload["headers"][:1]
        payload["resolutions"] = [
            {
                "incoming_label": "Day 01",
                "action": "overwrite",
                "target_section_id": s1,
            }
        ]
        r = client.post(f"/api/workbooks/{wid}/sections/import", json=payload)
        assert r.status_code == 201
        saved = r.json()["sections"][0]
        assert saved["id"] == s1  # same section reused (position/history kept)
        assert saved["overwritten"] is True

        sec = client.get(f"/api/sections/{s1}").json()
        assert sec["numbers"] == [1, 2]

        # New key is live: Day 01 answers were 3,4,1,5,2 -> now 1->1, 2->2
        att = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "1", "2": "2"}}
        ).json()
        assert att["score"] == 2

        detail = client.get(f"/api/workbooks/{wid}").json()
        assert [s["id"] for s in detail["sections"]] == [s1, two_sections[2]]

    def test_skip_incoming_discards_group(self, client, two_sections):
        wid, _, _ = two_sections
        payload = self._payload_with_two_groups()
        payload["resolutions"] = [
            {"incoming_label": "Day 01", "action": "skip_incoming"},
            {"incoming_label": "Day 02", "action": "skip_incoming"},
        ]
        r = client.post(f"/api/workbooks/{wid}/sections/import", json=payload)
        assert r.status_code == 422
        assert "모든 그룹이 폐기" in r.json()["detail"]

    def test_partial_resolution_mixed_with_append(self, client, two_sections):
        wid, _, _ = two_sections
        payload = self._payload_with_two_groups()
        payload["resolutions"] = [
            {"incoming_label": "Day 01", "action": "skip_incoming"}
        ]
        r = client.post(f"/api/workbooks/{wid}/sections/import", json=payload)
        assert r.status_code == 201
        labels = [s["label"] for s in r.json()["sections"]]
        assert labels == ["Day 02"]

    def test_overwrite_unknown_target_404(self, client, two_sections):
        wid, _, _ = two_sections
        payload = self._payload_with_two_groups()
        payload["entries"] = payload["entries"][:2]
        payload["headers"] = payload["headers"][:1]
        payload["resolutions"] = [
            {
                "incoming_label": "Day 01",
                "action": "overwrite",
                "target_section_id": 999999,
            }
        ]
        r = client.post(f"/api/workbooks/{wid}/sections/import", json=payload)
        assert r.status_code == 404


class TestSessionsServiceUnit:
    """Direct, DB-free coverage of app/services/sessions.py's two pure
    functions -- mirrors TestConflictsUnit below for services/conflicts.py."""

    def test_merge_answers_preserves_and_overlays(self):
        latest_results = [
            {"number": 1, "given": "3", "status": "correct"},
            {"number": 2, "given": "9", "status": "incorrect"},
            {"number": 3, "given": "", "status": "unanswered"},
        ]
        merged = merge_answers(latest_results, {"2": "4", "3": "1"})
        assert merged == {"1": "3", "2": "4", "3": "1"}

    def test_merge_answers_blank_retracts_previous_value(self):
        latest_results = [{"number": 1, "given": "3", "status": "correct"}]
        assert merge_answers(latest_results, {"1": ""}) == {}

    def test_merge_answers_ignores_blank_in_base_results(self):
        """A never-answered question in the base has nothing to carry
        forward -- it simply doesn't appear in the merged dict unless the
        new payload answers it."""
        latest_results = [{"number": 1, "given": "", "status": "unanswered"}]
        assert merge_answers(latest_results, {}) == {}

    def test_compute_breakdown_buckets_by_first_correct_submission_seq(self):
        all_numbers = [1, 2, 3, 4]
        attempts = [
            {
                "submission_seq": 1,
                "results": [
                    {"number": 1, "status": "correct"},
                    {"number": 2, "status": "incorrect"},
                    {"number": 3, "status": "unanswered"},
                    {"number": 4, "status": "incorrect"},
                ],
            },
            {
                "submission_seq": 2,
                "results": [
                    {"number": 2, "status": "correct"},
                    {"number": 4, "status": "incorrect"},
                ],
            },
        ]
        out = compute_breakdown(all_numbers, attempts)
        assert out["total_questions"] == 4
        assert out["first_try"] == {"numbers": [1], "count": 1, "percent": 25.0}
        assert out["second_try"] == {"numbers": [2], "count": 1, "percent": 25.0}
        # Q3 never answered at all, Q4 wrong in both submissions -> both
        # land in third_plus per spec ("never correct" includes "never
        # answered").
        assert out["third_plus"] == {"numbers": [3, 4], "count": 2, "percent": 50.0}

    def test_compute_breakdown_tolerates_unsorted_attempts_input(self):
        """attempts is re-sorted internally by submission_seq -- passing
        them out of order must not change the result."""
        all_numbers = [1]
        forward = compute_breakdown(
            all_numbers,
            [
                {"submission_seq": 1, "results": [{"number": 1, "status": "incorrect"}]},
                {"submission_seq": 2, "results": [{"number": 1, "status": "correct"}]},
            ],
        )
        reversed_input = compute_breakdown(
            all_numbers,
            [
                {"submission_seq": 2, "results": [{"number": 1, "status": "correct"}]},
                {"submission_seq": 1, "results": [{"number": 1, "status": "incorrect"}]},
            ],
        )
        assert forward == reversed_input == {
            "total_questions": 1,
            "first_try": {"numbers": [], "count": 0, "percent": 0.0},
            "second_try": {"numbers": [1], "count": 1, "percent": 100.0},
            "third_plus": {"numbers": [], "count": 0, "percent": 0.0},
        }

    def test_compute_breakdown_empty_key_does_not_divide_by_zero(self):
        assert compute_breakdown([], []) == {
            "total_questions": 0,
            "first_try": {"numbers": [], "count": 0, "percent": 0.0},
            "second_try": {"numbers": [], "count": 0, "percent": 0.0},
            "third_plus": {"numbers": [], "count": 0, "percent": 0.0},
        }


class TestConflictsUnit:
    def test_normalize_label(self):
        assert normalize_label("Day 01") == normalize_label("DAY  01")
        assert normalize_label("Chapter-2") == "chapter2"

    def test_labels_related_containment(self):
        assert labels_related("Day 01", "Day 01 - 수능")
        assert labels_related("01 힘과 운동", "01 힘과 운동")
        assert not labels_related("Day 01", "Day 02")

    def test_detect_pairs(self):
        existing = [{"id": 1, "label": "Day 01", "numbers": [1, 2, 3]}]
        incoming = [
            {"label": "Day 01", "numbers": [2, 3, 4]},
            {"label": "Day 09", "numbers": [1, 2]},
        ]
        conflicts = detect_conflicts(existing, incoming)
        assert len(conflicts) == 1
        assert conflicts[0]["existing_section"]["id"] == 1
        assert conflicts[0]["overlapping_numbers"] == [2, 3]
