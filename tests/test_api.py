import pytest

from app.services import gemini as gemini_service

DAY_SAMPLE = (
    "Day 01\n1. 3 2. 4 3. 1 4. 5 5. 2\n"
    "Day 02\n1. 2 2. 3 3. 4 4. 1 5. 5"
)
FLAT_SAMPLE = "\n".join(f"{i}. {i % 5}" for i in range(1, 13))

TABLE_PAYLOAD = {
    "workbook_title": "쎈 미적분",
    "groups": [
        {
            "main_category": "Day 01",
            "sub_category": None,
            "items": [
                {"number": 2, "type": "multiple_choice", "answer": "4"},
                {"number": 4, "type": "numeric", "answer": "-1.50"},
                {"number": 1, "type": "multiple_choice", "answer": "①③"},
                {"number": 3, "type": "numeric", "answer": "1,234"},
            ],
        }
    ],
    "notes": [],
}

MULTI_CHAPTER_PAYLOAD = {
    "workbook_title": "반복 번호 워크북",
    "groups": [
        {
            "main_category": "Day 01",
            "sub_category": None,
            "items": [
                {"number": 1, "type": "numeric", "answer": "10"},
                {"number": 2, "type": "numeric", "answer": "20"},
            ],
        },
        {
            "main_category": "Day 02",
            "sub_category": None,
            "items": [
                {"number": 1, "type": "numeric", "answer": "30"},
                {"number": 2, "type": "numeric", "answer": "40"},
            ],
        },
    ],
    "notes": [],
}


@pytest.fixture()
def wb(client):
    r = client.post("/api/workbooks", json={"title": "테스트 문제집"})
    assert r.status_code == 201
    return r.json()["id"]


def _import_headers(client, wid, sample=DAY_SAMPLE):
    preview = client.post("/api/extract-text", json={"raw_text": sample}).json()
    entries = [
        {"number": e["number"], "answer": e["answer"], "line": e.get("line", 0)}
        for e in preview["entries"]
    ]
    headers = preview["headers"]
    return client.post(
        f"/api/workbooks/{wid}/sections/import",
        json={
            "structure": "headers",
            "header_type": "day",
            "entries": entries,
            "headers": headers,
        },
    )


class TestHealth:
    def test_health(self, client):
        data = client.get("/api/health").json()
        assert data["status"] == "ok"
        assert isinstance(data["gemini_available"], bool)
        assert data["model"]


class TestWorkbookCrud:
    def test_create_and_list(self, client):
        client.post("/api/workbooks", json={"title": "쎈 미적분"})
        items = client.get("/api/workbooks").json()
        assert len(items) == 1
        assert items[0]["title"] == "쎈 미적분"

    def test_blank_title_rejected(self, client):
        assert client.post("/api/workbooks", json={"title": "   "}).status_code == 400

    def test_missing_workbook_404(self, client):
        assert client.get("/api/workbooks/999").status_code == 404

    def test_delete_cascades(self, client, wb):
        _import_headers(client, wb)
        assert client.delete(f"/api/workbooks/{wb}").status_code == 204
        assert client.get(f"/api/workbooks/{wb}").status_code == 404


class TestExtraction:
    def test_paste_day_preview(self, client):
        r = client.post("/api/extract-text", json={"raw_text": DAY_SAMPLE})
        assert r.status_code == 200
        p = r.json()
        assert p["engine"] == "paste"
        assert len(p["entries"]) == 10
        assert len(p["headers"]) == 2
        rec = p["recommendation"]
        assert rec["structure"] == "headers"
        assert len(rec["groups"]) == 2

    def test_flat_preview_recommends_chunks(self, client):
        p = client.post("/api/extract-text", json={"raw_text": FLAT_SAMPLE}).json()
        rec = p["recommendation"]
        assert rec["structure"] == "chunks"
        assert rec["chunk_size"] > 0
        assert any(a["chunk_size"] == 0 for a in rec["alternatives"])

    def test_no_input_400(self, client):
        assert client.post("/api/extract").status_code in (400, 422)

    def test_garbage_text_422(self, client):
        r = client.post("/api/extract-text", json={"raw_text": "정답 없는 텍스트"})
        assert r.status_code == 422

    def test_unsupported_file_type(self, client):
        r = client.post(
            "/api/extract",
            files={"file": ("a.txt", b"hello", "text/plain")},
        )
        assert r.status_code == 415

    def _patch_gemini_client(self, monkeypatch, payload, model="gemini-test"):
        import json

        class FakeResponse:
            text = json.dumps(payload)

        class FakeModels:
            def __init__(self):
                self.calls = []

            def generate_content(self, **kwargs):
                self.calls.append(kwargs)
                return FakeResponse()

        class FakeClient:
            def __init__(self):
                self.models = FakeModels()

        client_holder = FakeClient()
        monkeypatch.setattr(gemini_service.config, "GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(gemini_service.config, "GEMINI_MODEL", model)
        monkeypatch.setattr(gemini_service, "_client", lambda key: client_holder)
        return client_holder

    def test_image_extract_via_gemini(self, client, monkeypatch):
        holder = self._patch_gemini_client(monkeypatch, TABLE_PAYLOAD)
        png = b"\x89PNG\r\n\x1a\nfake"
        r = client.post(
            "/api/extract",
            files={"file": ("key.png", png, "image/png")},
        )
        assert r.status_code == 200
        p = r.json()
        assert p["engine"] == "gemini-vision"
        assert p["model"] == "gemini-test"
        assert p["workbook_title"] == "쎈 미적분"
        assert p["headers"] == [
            {"type": "day", "label": "Day 01", "index": 0, "line": 0}
        ]
        rec = p["recommendation"]
        assert rec["structure"] == "headers"
        nums = [e["number"] for e in p["entries"]]
        answers = [e["answer"] for e in p["entries"]]
        qtypes = [e["qtype"] for e in p["entries"]]
        assert nums == [1, 2, 3, 4]
        assert answers == ["1,3", "4", "1234", "-1.5"]
        assert set(qtypes) <= {"multiple_choice", "numeric"}
        # prompt + image actually reached the SDK
        call = holder.models.calls[0]
        assert call["model"] == "gemini-test"
        part = call["contents"][0]
        assert part.inline_data.mime_type == "image/png"
        assert "multiple_choice" in gemini_service.SYSTEM_PROMPT
        assert "numeric" in gemini_service.SYSTEM_PROMPT

    def test_image_extract_without_key_503(self, client, monkeypatch):
        monkeypatch.setattr(gemini_service.config, "GEMINI_API_KEY", "")
        r = client.post(
            "/api/extract",
            files={"file": ("key.png", b"img", "image/png")},
        )
        assert r.status_code == 503
        assert "GEMINI_API_KEY" in r.json()["detail"]

    def test_gemini_no_entries_502(self, client, monkeypatch):
        self._patch_gemini_client(monkeypatch, {"entries": [], "notes": ["전부 손글씨"]})
        r = client.post(
            "/api/extract",
            files={"file": ("key.png", b"img", "image/png")},
        )
        assert r.status_code == 502
        assert "손글씨" in r.json()["detail"]

    def test_multi_chapter_repeated_numbers_no_collision(self, client, monkeypatch):
        """Numbers restarting at 1 per chapter must not overwrite each other."""
        self._patch_gemini_client(monkeypatch, MULTI_CHAPTER_PAYLOAD)
        png = b"\x89PNG\r\n\x1a\nfake"
        p = client.post(
            "/api/extract", files={"file": ("key.png", png, "image/png")}
        ).json()

        # Preview must NOT warn about duplicates across section boundaries.
        dupes = [i for i in p["issues"] if i["kind"] == "duplicate"]
        assert dupes == []
        assert len(p["headers"]) == 2
        assert p["recommendation"]["structure"] == "headers"

        wid = client.post("/api/workbooks", json={"title": "다중 챕터"}).json()["id"]
        body = {
            "structure": "headers",
            "header_type": "day",
            "entries": [
                {"number": e["number"], "answer": e["answer"], "line": e["line"]}
                for e in p["entries"]
            ],
            "headers": p["headers"],
        }
        r = client.post(f"/api/workbooks/{wid}/sections/import", json=body)
        assert r.status_code == 201
        secs = r.json()["sections"]
        assert [s["problem_count"] for s in secs] == [2, 2]

        # Each section grades against ITS OWN key — no cross-chapter override.
        s1, s2 = secs[0]["id"], secs[1]["id"]
        a1 = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "10", "2": "20"}}
        ).json()
        a2 = client.post(
            "/api/attempts", json={"section_id": s2, "answers": {"1": "30", "2": "40"}}
        ).json()
        assert a1["score"] == 2
        assert a2["score"] == 2

    def test_import_rejects_non_mc_numeric_answer(self, client, wb):
        preview = client.post(
            "/api/extract-text",
            json={"raw_text": "1. 3\n2. 미적분"},
        ).json()
        entries = [
            {"number": e["number"], "answer": e["answer"], "line": e.get("line", 0)}
            for e in preview["entries"]
        ]
        assert len(entries) == 1


class TestImportAndGrading:
    def test_full_lifecycle(self, client, wb):
        r = _import_headers(client, wb)
        assert r.status_code == 201
        sections = r.json()["sections"]
        assert len(sections) == 2
        assert all(s["problem_count"] == 5 for s in sections)

        detail = client.get(f"/api/workbooks/{wb}").json()
        assert len(detail["sections"]) == 2

        sid = sections[0]["id"]
        sec = client.get(f"/api/sections/{sid}").json()
        assert sec["numbers"] == [1, 2, 3, 4, 5]
        body = repr(sec).lower()
        assert "answer" not in body.replace("workbook_title", "")

        att = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"1": "3", "2": "9", "4": "③"}},
        ).json()
        assert att["score"] == 1
        assert set(att["wrong_numbers"]) == {2, 4}
        assert set(att["unanswered_numbers"]) == {3, 5}

        misses = client.post("/api/attempts/from-misses", json={"attempt_id": att["id"]})
        assert misses.status_code == 201
        mdata = misses.json()
        assert sorted(mdata["numbers"]) == [2, 3, 4, 5]

        retry = client.post(
            "/api/attempts",
            json={
                "section_id": sid,
                "answers": {"2": "4", "3": "1", "4": "5", "5": "2"},
            },
        )
        assert retry.status_code == 201

        stats = client.get(f"/api/workbooks/{wb}/stats").json()
        assert len(stats["sections"]) == 2
        top = stats["top_missed"]
        assert top and any(t["number"] == 2 for t in top)
        entry = next(t for t in top if t["number"] == 2)
        assert entry["section_id"] == sid
        assert entry["workbook_id"] == wb
        assert entry["workbook_title"] == "테스트 문제집"

        history = client.get(f"/api/sections/{sid}/attempts").json()
        assert len(history) == 2
        assert history[0]["id"] >= history[-1]["id"]

        full = client.get(f"/api/attempts/{att['id']}").json()
        assert len(full["results"]) == 5

    def test_ordinary_attempts_are_full_and_update_stats(self, client, wb):
        """Regression for the partial-retry flag: an ordinary attempt (no
        merge_attempt_id) must be is_full_attempt: true and must keep
        updating section/workbook history and best/recent stats exactly as
        before this feature existed."""
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]

        a1 = client.post(
            "/api/attempts", json={"section_id": sid, "answers": {"1": "3"}}
        ).json()
        assert a1["is_full_attempt"] is True

        a2 = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"1": "3", "2": "4"}},
        ).json()
        assert a2["is_full_attempt"] is True
        assert a2["percent"] > a1["percent"]

        history = client.get(f"/api/sections/{sid}/attempts").json()
        assert len(history) == 2  # both ordinary attempts counted

        stats = client.get(f"/api/workbooks/{wb}/stats").json()
        sec = next(s for s in stats["sections"] if s["section_id"] == sid)
        assert sec["attempt_count"] == 2
        assert sec["latest_percent"] == a2["percent"]
        assert sec["best_percent"] == a2["percent"]

        fetched = client.get(f"/api/attempts/{a1['id']}").json()
        assert fetched["is_full_attempt"] is True

    def test_import_chunks_structure(self, client, wb):
        preview = client.post("/api/extract-text", json={"raw_text": FLAT_SAMPLE}).json()
        entries = [
            {"number": e["number"], "answer": e["answer"], "line": e.get("line", 0)}
            for e in preview["entries"]
        ]
        r = client.post(
            f"/api/workbooks/{wb}/sections/import",
            json={"structure": "chunks", "chunk_size": 5, "entries": entries},
        )
        assert r.status_code == 201
        secs = r.json()["sections"]
        assert len(secs) == 3
        labels = [s["label"] for s in secs]
        assert labels[0] == "1~5" and labels[-1] == "11~12"

    def test_import_unknown_workbook(self, client):
        r = client.post(
            "/api/workbooks/999/sections/import",
            json={
                "structure": "chunks",
                "chunk_size": 5,
                "entries": [{"number": 1, "answer": "1"}],
            },
        )
        assert r.status_code == 404

    def test_attempt_unknown_section_404(self, client):
        r = client.post("/api/attempts", json={"section_id": 999, "answers": {}})
        assert r.status_code == 404

    def test_empty_answers_allowed(self, client, wb):
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        att = client.post("/api/attempts", json={"section_id": sid, "answers": {}}).json()
        assert att["score"] == 0
        assert len(att["unanswered_numbers"]) == 5

    def test_extra_answer_note(self, client, wb):
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        att = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"1": "3", "50": "1"}},
        ).json()
        assert att["score"] >= 1
        assert "note" in att

    def test_answered_only_excludes_skipped_from_total(self, client, wb):
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        att = client.post(
            "/api/attempts",
            json={
                "section_id": sid,
                "answers": {"1": "3", "2": "9"},
                "answered_only": True,
            },
        ).json()
        assert att["score"] == 1
        assert att["total"] == 2
        assert att["percent"] == 50.0
        assert att["wrong_numbers"] == [2]
        assert set(att["unanswered_numbers"]) == {3, 4, 5}
        assert att["note"] == "3문항은 미응답으로 채점에서 제외했습니다."

        full = client.get(f"/api/attempts/{att['id']}").json()
        assert full["total"] == 2
        assert full["percent"] == 50.0
        assert set(full["unanswered_numbers"]) == {3, 4, 5}

    def test_answered_only_omitted_keeps_full_total(self, client, wb):
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        att = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"1": "3", "2": "9"}},
        ).json()
        assert att["total"] == 5
        assert att["percent"] == 20.0
        assert "note" not in att

    def test_from_misses_perfect_422(self, client, wb):
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        perfect = {"1": "3", "2": "4", "3": "1", "4": "5", "5": "2"}
        att = client.post("/api/attempts", json={"section_id": sid, "answers": perfect}).json()
        assert att["score"] == 5
        r = client.post("/api/attempts/from-misses", json={"attempt_id": att["id"]})
        assert r.status_code == 422

    def test_delete_attempt(self, client, wb):
        r = _import_headers(client, wb)
        sid = r.json()["sections"][0]["id"]
        att = client.post("/api/attempts", json={"section_id": sid, "answers": {}}).json()
        assert client.delete(f"/api/attempts/{att['id']}").status_code == 204
        assert client.get(f"/api/attempts/{att['id']}").status_code == 404


class TestQaRegressions:
    def test_huge_id_404_not_500(self, client):
        assert client.get("/api/workbooks/999999999999999999999").status_code == 422
        assert client.delete("/api/workbooks/999999999999999999999").status_code == 422
        r = client.post(
            "/api/attempts",
            json={"section_id": 999999999999999999999, "answers": {}},
        )
        assert r.status_code == 422
        r = client.post(
            "/api/attempts/from-misses", json={"attempt_id": 999999999999999999999}
        )
        assert r.status_code == 422

    def test_workbook_detail_has_aggregates(self, client, wb):
        d = client.get(f"/api/workbooks/{wb}").json()
        for key in ("section_count", "problem_count", "latest_percent"):
            assert key in d

    def test_attempt_answer_size_capped(self, client, wb):
        r0 = _import_headers(client, wb)
        sid = r0.json()["sections"][0]["id"]
        r = client.post(
            "/api/attempts", json={"section_id": sid, "answers": {"1": "x" * 500}}
        )
        assert r.status_code == 422

    def test_too_many_answers_rejected(self, client, wb):
        r0 = _import_headers(client, wb)
        sid = r0.json()["sections"][0]["id"]
        answers = {str(i): "1" for i in range(501)}
        r = client.post("/api/attempts", json={"section_id": sid, "answers": answers})
        assert r.status_code == 422

    def test_sqlish_answer_safe(self, client, wb):
        r0 = _import_headers(client, wb)
        sid = r0.json()["sections"][0]["id"]
        r = client.post(
            "/api/attempts",
            json={"section_id": sid, "answers": {"1": "'; DROP TABLE workbooks;--"}},
        )
        assert r.status_code == 201
        assert client.get("/api/workbooks").status_code == 200
