"""Independent coverage for multi-image answer-key upload.

Complements tests/test_gemini.py::TestExtractAnswerKeyBatch/TestMergeResults
(unit-level offset arithmetic) and tests/test_api.py's multi-image cases
(preview-only HTTP assertions) with scenarios those don't exercise:
the exact MAX_EXTRACT_IMAGES boundary, fail-fast on a *first*-image and a
zero-entries failure, a full extract -> import -> grade round trip across
three separate photos, filtering of an empty file slot, and a
characterization test for the documented header-collision trade-off.
"""

import json

import pytest

from app import config
from app.errors import GeminiResponseError
from app.services import gemini as gemini_service

PNG = b"\x89PNG\r\n\x1a\nfake"


def _payload(title, main_category, items, notes=None):
    return {
        "workbook_title": title,
        "groups": [{"main_category": main_category, "sub_category": None, "items": items}],
        "notes": notes or [],
    }


class FakeResponse:
    def __init__(self, payload):
        self.text = json.dumps(payload)


class SequencedModels:
    """One canned payload per Gemini call, in call order. An Exception
    instance in the sequence is raised instead of returned, simulating a
    bad/unreadable image at that position of the batch."""

    def __init__(self, items):
        self.items = list(items)
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        item = self.items[len(self.calls) - 1]
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


class FakeClient:
    def __init__(self, models):
        self.models = models


def _patch_sequenced(monkeypatch, items):
    models = SequencedModels(items)
    monkeypatch.setattr(gemini_service.config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini_service.config, "GEMINI_MODEL", "gemini-test")
    monkeypatch.setattr(gemini_service, "_client", lambda key: FakeClient(models))
    return models


def _files(n):
    return [("file", (f"p{i}.png", PNG, "image/png")) for i in range(n)]


@pytest.fixture()
def wb(client):
    r = client.post("/api/workbooks", json={"title": "다중 이미지 테스트"})
    assert r.status_code == 201
    return r.json()["id"]


class TestMaxImagesBoundary:
    def test_exactly_max_images_succeeds(self, client, monkeypatch):
        """The count check is `> MAX_EXTRACT_IMAGES`, so the boundary value
        itself must succeed -- only exceeding it should 400. (The existing
        suite only checks MAX+1; this checks the inclusive edge.)"""
        n = config.MAX_EXTRACT_IMAGES
        payloads = [
            _payload(
                f"제목{i}", f"Day {i:02d}", [{"number": 1, "type": "numeric", "answer": str(i)}]
            )
            for i in range(n)
        ]
        models = _patch_sequenced(monkeypatch, payloads)
        r = client.post("/api/extract", files=_files(n))
        assert r.status_code == 200
        p = r.json()
        assert len(models.calls) == n
        assert len(p["headers"]) == n
        assert len(p["entries"]) == n
        assert [h["index"] for h in p["headers"]] == list(range(n))
        assert [h["line"] for h in p["headers"]] == list(range(n))
        assert [e["line"] for e in p["entries"]] == list(range(n))


class TestMultiImageFailFast:
    def test_first_image_failure_stops_before_second_call(self, client, monkeypatch):
        """A bad *first* image must fail the whole request immediately --
        the second (perfectly good) image must never reach Gemini. The
        existing suite only exercises a failure on the *second* image."""
        models = _patch_sequenced(
            monkeypatch,
            [
                GeminiResponseError("첫 이미지 인식 실패"),
                _payload("t", "Day 01", [{"number": 1, "type": "numeric", "answer": "1"}]),
            ],
        )
        r = client.post("/api/extract", files=_files(2))
        assert r.status_code == 502
        assert "첫 이미지 인식 실패" in r.json()["detail"]
        assert len(models.calls) == 1

    def test_second_image_zero_entries_fails_whole_batch(self, client, monkeypatch):
        """A blurry/handwritten second photo -- Gemini responds but yields
        no usable multiple-choice/numeric entries -- must fail the whole
        request (matching today's single-image 502), not silently drop
        that image and return a partial result built from image 1 alone."""
        good = _payload("워크북", "Day 01", [{"number": 1, "type": "numeric", "answer": "1"}])
        empty = {"workbook_title": "", "groups": [], "notes": ["전부 손글씨"]}
        models = _patch_sequenced(monkeypatch, [good, empty])
        r = client.post("/api/extract", files=_files(2))
        assert r.status_code == 502
        assert "손글씨" in r.json()["detail"]
        assert len(models.calls) == 2


class TestMultiImageFullLifecycle:
    def test_three_images_import_and_grade_independently(self, client, monkeypatch, wb):
        """The real end-to-end path a user takes: three separate photos
        merge into one preview, get imported as three sections, and each
        section grades against only its own photo's answer key -- the
        multi-image analogue of test_api.py's
        test_multi_chapter_repeated_numbers_no_collision (which only
        covers multiple groups *within one* image)."""
        payloads = [
            _payload(
                "워크북",
                "Day 01",
                [
                    {"number": 1, "type": "numeric", "answer": "10"},
                    {"number": 2, "type": "numeric", "answer": "20"},
                ],
            ),
            _payload("", "Day 02", [{"number": 1, "type": "numeric", "answer": "30"}]),
            _payload(
                "",
                "Day 03",
                [
                    {"number": 1, "type": "numeric", "answer": "40"},
                    {"number": 2, "type": "numeric", "answer": "50"},
                    {"number": 3, "type": "numeric", "answer": "60"},
                ],
            ),
        ]
        models = _patch_sequenced(monkeypatch, payloads)
        r = client.post("/api/extract", files=_files(3))
        assert r.status_code == 200
        p = r.json()
        assert len(models.calls) == 3
        assert p["workbook_title"] == "워크북"
        assert p["recommendation"]["structure"] == "headers"
        assert len(p["headers"]) == 3
        # no spurious duplicate warning: three distinct headers, no collision
        assert [i for i in p["issues"] if i["kind"] == "duplicate"] == []

        body = {
            "structure": "headers",
            "header_type": "day",
            "entries": [
                {"number": e["number"], "answer": e["answer"], "line": e["line"]}
                for e in p["entries"]
            ],
            "headers": p["headers"],
        }
        imp = client.post(f"/api/workbooks/{wb}/sections/import", json=body)
        assert imp.status_code == 201
        secs = imp.json()["sections"]
        assert [s["problem_count"] for s in secs] == [2, 1, 3]

        s1, s2, s3 = (s["id"] for s in secs)
        a1 = client.post(
            "/api/attempts", json={"section_id": s1, "answers": {"1": "10", "2": "20"}}
        ).json()
        a2 = client.post("/api/attempts", json={"section_id": s2, "answers": {"1": "30"}}).json()
        a3 = client.post(
            "/api/attempts",
            json={"section_id": s3, "answers": {"1": "40", "2": "1", "3": "60"}},
        ).json()
        assert a1["score"] == 2
        assert a2["score"] == 1
        assert a3["score"] == 2
        assert set(a3["wrong_numbers"]) == {2}

    def test_blank_filename_slot_among_real_files_is_ignored(self, client, monkeypatch):
        """An empty file part (`filename=""` on the wire -- e.g. an unused
        native `<input type=file>` slot) alongside a real one must be
        filtered out -- not counted toward MAX_EXTRACT_IMAGES and never
        sent to Gemini -- matching the router's `if f.filename` filter.

        Built as a raw multipart body rather than via httpx's `files=`
        helper: httpx silently *omits* the filename parameter for a
        falsy filename when encoding `files=[("file", ("", ...))]`, which
        turns that part into a plain form field rather than a genuine
        empty-filename file part -- not the shape being tested here.
        """
        one_image = [_payload("t", "Day 01", [{"number": 1, "type": "numeric", "answer": "1"}])]
        models = _patch_sequenced(monkeypatch, one_image)
        boundary = "TestBoundaryMultiImage"
        body = (
            f'--{boundary}\r\n'
            'Content-Disposition: form-data; name="file"; filename=""\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            "\r\n"
            f'--{boundary}\r\n'
            'Content-Disposition: form-data; name="file"; filename="p1.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
        r = client.post(
            "/api/extract",
            content=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert r.status_code == 200
        assert len(models.calls) == 1


class TestMultiImageKnownLimitation:
    def test_two_headerless_images_collide_into_one_duplicate_warning(self, client, monkeypatch):
        """Documented trade-off (flagged, not fixed, by the implementation
        plan): when Gemini finds no printed header on either photo, both
        fall back to the generic '전체' label, and label-based scoping
        merges them into one scope -- so identical printed numbers across
        two logically unrelated photos surface as a 'duplicate' warning.
        This locks in that the failure mode is a *warning*, not silent
        data loss or a crash: both entries still come back in the result.
        """
        payload1 = {
            "workbook_title": "",
            "entries": [{"number": 1, "type": "numeric", "answer": "5"}],
            "notes": [],
        }
        payload2 = {
            "workbook_title": "",
            "entries": [{"number": 1, "type": "numeric", "answer": "9"}],
            "notes": [],
        }
        models = _patch_sequenced(monkeypatch, [payload1, payload2])
        r = client.post("/api/extract", files=_files(2))
        assert r.status_code == 200
        p = r.json()
        assert len(models.calls) == 2
        assert [e["number"] for e in p["entries"]] == [1, 1]
        assert [e["answer"] for e in p["entries"]] == ["5", "9"]
        dupes = [i for i in p["issues"] if i["kind"] == "duplicate"]
        assert dupes, "expected the known label-collision duplicate warning"
