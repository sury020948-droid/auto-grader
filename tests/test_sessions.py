"""Integration tests: retry merge, granular session deletion, duplicates."""

import pytest

from app.services.conflicts import detect_conflicts, labels_related, normalize_label

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

        misses = client.post(
            "/api/attempts/from-misses", json={"attempt_id": base["id"]}
        )
        assert misses.status_code == 201
        mdata = misses.json()
        assert mdata["attempt_id"] == base["id"]
        assert sorted(mdata["numbers"]) == [2, 5]

        retry = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"2": "④"},  # only the re-attempted question
                "merge_attempt_id": base["id"],
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
        assert merged["merged_from"] == base["id"]

        old = client.get(f"/api/attempts/{base['id']}").json()
        assert old["score"] == 3  # history untouched

    def test_blank_retry_answer_retracts(self, client, two_sections):
        _, s1, _ = two_sections
        base = client.post(
            "/api/attempts",
            json={"section_id": s1, "answers": {"1": "3", "2": "9"}},
        ).json()
        merged = client.post(
            "/api/attempts",
            json={
                "section_id": s1,
                "answers": {"2": ""},
                "merge_attempt_id": base["id"],
            },
        ).json()
        by_num = {r["number"]: r["status"] for r in merged["results"]}
        assert by_num[1] == "correct"
        assert by_num[2] == "unanswered"

    def test_merge_requires_same_section(self, client, two_sections):
        _, s1, s2 = two_sections
        base = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "3"}}
        ).json()
        r = client.post(
            "/api/attempts",
            json={"section_id": s2, "answers": {}, "merge_attempt_id": base["id"]},
        )
        assert r.status_code == 400

    def test_merge_unknown_attempt_404(self, client, two_sections):
        _, s1, _ = two_sections
        r = client.post(
            "/api/attempts",
            json={"section_id": s1, "answers": {}, "merge_attempt_id": 99999},
        )
        assert r.status_code == 404


class TestSectionDeletion:
    def test_delete_only_target_session(self, client, two_sections):
        wid, s1, s2 = two_sections
        client.post("/api/attempts", json={"section_id": s1, "answers": {"1": "3"}})
        client.post("/api/attempts", json={"section_id": s2, "answers": {"1": "2"}})

        r = client.delete(f"/api/sections/{s2}")
        assert r.status_code == 204

        detail = client.get(f"/api/workbooks/{wid}").json()
        assert [s["id"] for s in detail["sections"]] == [s1]
        assert detail["problem_count"] == 5

        assert client.get(f"/api/sections/{s2}").status_code == 404
        assert client.get(f"/api/sections/{s1}").status_code == 200

        hist = client.get(f"/api/sections/{s1}/attempts").json()
        assert len(hist) == 1  # sibling data intact

        stats = client.get(f"/api/workbooks/{wid}/stats").json()
        assert [s["section_id"] for s in stats["sections"]] == [s1]
        assert all(m["section_label"] == "Day 01" for m in stats["top_missed"])

    def test_delete_missing_section_404(self, client, two_sections):
        assert client.delete("/api/sections/999999").status_code == 404


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
