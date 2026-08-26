import pytest

from app import config
from app.errors import GeminiResponseError
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

    def test_rename_updates_title(self, client, wb):
        r = client.patch(f"/api/workbooks/{wb}", json={"title": "새 이름"})
        assert r.status_code == 200
        assert r.json()["title"] == "새 이름"
        assert client.get(f"/api/workbooks/{wb}").json()["title"] == "새 이름"
        items = client.get("/api/workbooks").json()
        assert next(b for b in items if b["id"] == wb)["title"] == "새 이름"

    def test_rename_blank_title_rejected(self, client, wb):
        assert client.patch(f"/api/workbooks/{wb}", json={"title": "   "}).status_code == 400

    def test_rename_missing_workbook_404(self, client):
        assert client.patch("/api/workbooks/999", json={"title": "새 이름"}).status_code == 404

    def test_cross_device_rename_blocked(self, client, other_device_client, wb):
        r = other_device_client.patch(f"/api/workbooks/{wb}", json={"title": "가로채기"})
        assert r.status_code == 404
        assert client.get(f"/api/workbooks/{wb}").json()["title"] == "테스트 문제집"

    def test_rename_trims_surrounding_whitespace(self, client, wb):
        r = client.patch(f"/api/workbooks/{wb}", json={"title": "  트림 테스트  "})
        assert r.status_code == 200
        assert r.json()["title"] == "트림 테스트"
        assert client.get(f"/api/workbooks/{wb}").json()["title"] == "트림 테스트"

    def test_rename_empty_string_title_422(self, client, wb):
        """A literal "" fails pydantic's min_length=1 (422) -- distinct from
        the whitespace-only case, which passes schema validation and is only
        caught by the route's own post-strip blank check (400)."""
        r = client.patch(f"/api/workbooks/{wb}", json={"title": ""})
        assert r.status_code == 422
        assert client.get(f"/api/workbooks/{wb}").json()["title"] == "테스트 문제집"

    def test_rename_missing_title_field_422(self, client, wb):
        assert client.patch(f"/api/workbooks/{wb}", json={}).status_code == 422

    def test_rename_title_too_long_422(self, client, wb):
        r = client.patch(f"/api/workbooks/{wb}", json={"title": "x" * 121})
        assert r.status_code == 422
        assert client.get(f"/api/workbooks/{wb}").json()["title"] == "테스트 문제집"

    def test_rename_title_at_max_length_accepted(self, client, wb):
        title = "x" * 120
        r = client.patch(f"/api/workbooks/{wb}", json={"title": title})
        assert r.status_code == 200
        assert r.json()["title"] == title

    def test_rename_invalid_wid_path_422(self, client):
        """`wid` reuses the shared `ID` path type (ge=1) on PATCH too."""
        assert client.patch("/api/workbooks/0", json={"title": "x"}).status_code == 422

    def test_rename_response_matches_list_item_shape(self, client, wb):
        list_item = next(b for b in client.get("/api/workbooks").json() if b["id"] == wb)
        patch_body = client.patch(
            f"/api/workbooks/{wb}", json={"title": "모양 테스트"}
        ).json()
        assert set(patch_body) == set(list_item)

    def test_rename_does_not_change_id_or_created_at(self, client, wb):
        before = client.get(f"/api/workbooks/{wb}").json()
        after = client.patch(f"/api/workbooks/{wb}", json={"title": "정체성 유지"}).json()
        assert after["id"] == before["id"] == wb
        assert after["created_at"] == before["created_at"]

    def test_rename_to_current_title_is_a_no_op_success(self, client, wb):
        r = client.patch(f"/api/workbooks/{wb}", json={"title": "테스트 문제집"})
        assert r.status_code == 200
        assert r.json()["title"] == "테스트 문제집"

    def test_successive_renames_persist_latest_title(self, client, wb):
        client.patch(f"/api/workbooks/{wb}", json={"title": "첫번째"})
        client.patch(f"/api/workbooks/{wb}", json={"title": "두번째"})
        r = client.patch(f"/api/workbooks/{wb}", json={"title": "세번째"})
        assert r.status_code == 200
        assert client.get(f"/api/workbooks/{wb}").json()["title"] == "세번째"

    def test_rename_does_not_affect_other_workbooks(self, client, wb):
        other_id = client.post("/api/workbooks", json={"title": "다른 워크북"}).json()["id"]
        client.patch(f"/api/workbooks/{wb}", json={"title": "이것만 변경"})
        assert client.get(f"/api/workbooks/{other_id}").json()["title"] == "다른 워크북"

    def test_rename_preserves_sections_and_counts(self, client, wb):
        _import_headers(client, wb)
        before = client.get(f"/api/workbooks/{wb}").json()
        assert before["section_count"] > 0
        assert before["problem_count"] > 0
        before_section_ids = [s["id"] for s in before["sections"]]

        r = client.patch(f"/api/workbooks/{wb}", json={"title": "이름 변경됨"})
        assert r.status_code == 200
        assert r.json()["section_count"] == before["section_count"]
        assert r.json()["problem_count"] == before["problem_count"]

        after = client.get(f"/api/workbooks/{wb}").json()
        assert after["title"] == "이름 변경됨"
        assert after["section_count"] == before["section_count"]
        assert after["problem_count"] == before["problem_count"]
        assert [s["id"] for s in after["sections"]] == before_section_ids


class TestUpdateWorkbookTitleDal:
    """`rename_workbook` never inspects `update_workbook_title`'s return
    value -- pin its True/False contract directly at the dal layer so a
    silent regression there (e.g. dropping the `user_id` scope from the
    WHERE clause) is still caught even though the route itself can't."""

    def test_false_for_nonexistent_workbook(self, client, device_id):
        from app import db as dal

        conn = dal.connect()
        try:
            user = dal.get_or_create_device_user(conn, device_id)
            ok = dal.update_workbook_title(conn, 999999, int(user["id"]), "x")
        finally:
            conn.close()
        assert ok is False

    def test_false_for_foreign_owner_and_leaves_title_untouched(
        self, client, wb, device_id
    ):
        import uuid

        from app import db as dal

        conn = dal.connect()
        try:
            other = dal.get_or_create_device_user(conn, str(uuid.uuid4()))
            ok = dal.update_workbook_title(conn, wb, int(other["id"]), "가로채기")
            conn.commit()
        finally:
            conn.close()
        assert ok is False
        assert client.get(f"/api/workbooks/{wb}").json()["title"] == "테스트 문제집"

    def test_true_and_persists_for_real_owner(self, client, wb, device_id):
        from app import db as dal

        conn = dal.connect()
        try:
            user = dal.get_or_create_device_user(conn, device_id)
            ok = dal.update_workbook_title(conn, wb, int(user["id"]), "DAL 직접 변경")
            conn.commit()
        finally:
            conn.close()
        assert ok is True
        assert client.get(f"/api/workbooks/{wb}").json()["title"] == "DAL 직접 변경"


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

    def _patch_gemini_sequenced(self, monkeypatch, items, model="gemini-test"):
        """Like `_patch_gemini_client` but returns one item per Gemini call,
        in order — a dict payload becomes a JSON response, an Exception
        instance is raised instead (simulating one bad image in a batch)."""
        import json

        class FakeResponse:
            def __init__(self, payload):
                self.text = json.dumps(payload)

        class FakeModels:
            def __init__(self):
                self.calls = []

            def generate_content(self, **kwargs):
                self.calls.append(kwargs)
                item = items[len(self.calls) - 1]
                if isinstance(item, Exception):
                    raise item
                return FakeResponse(item)

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

    def test_multi_image_merges_continuous_ranges(self, client, monkeypatch):
        """Two images posted under repeated 'file' fields merge into one
        continuous answer key — header index/line and entry line span both
        images without overlap, and notes from both images appear."""
        payload1 = {
            "workbook_title": "쎈 미적분",
            "groups": [{
                "main_category": "Day 01",
                "sub_category": None,
                "items": [
                    {"number": 1, "type": "numeric", "answer": "1"},
                    {"number": 2, "type": "numeric", "answer": "2"},
                ],
            }],
            "notes": ["첫 사진 노트"],
        }
        payload2 = {
            "workbook_title": "",
            "groups": [{
                "main_category": "Day 02",
                "sub_category": None,
                "items": [{"number": 1, "type": "numeric", "answer": "9"}],
            }],
            "notes": ["둘째 사진 노트"],
        }
        holder = self._patch_gemini_sequenced(monkeypatch, [payload1, payload2])
        png = b"\x89PNG\r\n\x1a\nfake"
        r = client.post(
            "/api/extract",
            files=[
                ("file", ("p1.png", png, "image/png")),
                ("file", ("p2.png", png, "image/png")),
            ],
        )
        assert r.status_code == 200
        p = r.json()
        assert len(holder.models.calls) == 2
        assert p["workbook_title"] == "쎈 미적분"
        assert [(h["label"], h["index"], h["line"]) for h in p["headers"]] == [
            ("Day 01", 0, 0),
            ("Day 02", 1, 2),
        ]
        assert [e["number"] for e in p["entries"]] == [1, 2, 1]
        assert [e["line"] for e in p["entries"]] == [0, 1, 2]
        notes = [i["message"] for i in p["issues"] if i["kind"] == "noise"]
        assert any("첫 사진 노트" in n for n in notes)
        assert any("둘째 사진 노트" in n for n in notes)

    def test_single_image_still_works_as_before(self, client, monkeypatch):
        """A lone file under the repeated-field-capable 'file' param must
        behave exactly as the pre-existing single-image path did."""
        self._patch_gemini_client(monkeypatch, TABLE_PAYLOAD)
        png = b"\x89PNG\r\n\x1a\nfake"
        r = client.post(
            "/api/extract",
            files={"file": ("key.png", png, "image/png")},
        )
        assert r.status_code == 200
        p = r.json()
        assert p["headers"] == [
            {"type": "day", "label": "Day 01", "index": 0, "line": 0}
        ]
        assert [e["number"] for e in p["entries"]] == [1, 2, 3, 4]

    def test_max_images_exceeded_400_before_gemini_call(self, client, monkeypatch):
        holder = self._patch_gemini_sequenced(
            monkeypatch, [TABLE_PAYLOAD] * (config.MAX_EXTRACT_IMAGES + 1)
        )
        png = b"\x89PNG\r\n\x1a\nfake"
        files = [
            ("file", (f"p{i}.png", png, "image/png"))
            for i in range(config.MAX_EXTRACT_IMAGES + 1)
        ]
        r = client.post("/api/extract", files=files)
        assert r.status_code == 400
        assert holder.models.calls == []  # rejected before any Gemini call

    def test_multi_image_bad_type_415_before_gemini_call(self, client, monkeypatch):
        holder = self._patch_gemini_sequenced(monkeypatch, [TABLE_PAYLOAD, TABLE_PAYLOAD])
        png = b"\x89PNG\r\n\x1a\nfake"
        r = client.post(
            "/api/extract",
            files=[
                ("file", ("good.png", png, "image/png")),
                ("file", ("bad.txt", b"hello", "text/plain")),
            ],
        )
        assert r.status_code == 415
        assert holder.models.calls == []  # rejected before any Gemini call

    def test_multi_image_second_failure_fails_whole_request(self, client, monkeypatch):
        """Fail-fast: an error on image 2 fails the whole request (same
        502 a single bad image gives today) — no silent partial result."""
        holder = self._patch_gemini_sequenced(
            monkeypatch, [TABLE_PAYLOAD, GeminiResponseError("이미지 2 인식 실패")]
        )
        png = b"\x89PNG\r\n\x1a\nfake"
        r = client.post(
            "/api/extract",
            files=[
                ("file", ("p1.png", png, "image/png")),
                ("file", ("p2.png", png, "image/png")),
            ],
        )
        assert r.status_code == 502
        assert "이미지 2 인식 실패" in r.json()["detail"]
        assert len(holder.models.calls) == 2  # stopped right after the failure

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
