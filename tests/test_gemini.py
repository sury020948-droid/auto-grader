import json

import pytest

from app import config
from app.errors import GeminiResponseError, GeminiUnavailableError
from app.services import gemini


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FlakyModels:
    """Fails N times, then succeeds."""

    def __init__(self, fail_times, exc=None, response=None):
        self.fail_times = fail_times
        self.exc = exc or ConnectionError("network down")
        self.response = response
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return self.response


class FakeClient:
    def __init__(self, models):
        self.models = models


def _payload(entries=None, notes=None, groups=None, title=""):
    if groups is None:
        groups = [
            {"main_category": "Day 01", "sub_category": None, "items": entries or []}
        ]
    return {"workbook_title": title, "groups": groups, "notes": notes or []}


def _patch(monkeypatch, models, model="gemini-test", key="k"):
    monkeypatch.setattr(config, "GEMINI_API_KEY", key)
    monkeypatch.setattr(config, "GEMINI_MODEL", model)
    monkeypatch.setattr(config, "GEMINI_MAX_RETRIES", 2)
    monkeypatch.setattr(gemini, "_client", lambda key: FakeClient(models))
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)


TABLE_LAYOUT = [
    {"number": 1, "type": "multiple_choice", "answer": "3"},
    {"number": 2, "type": "numeric", "answer": "-4.5"},
    {"number": 10, "type": "multiple_choice", "answer": "ㄱ"},
    {"number": 12, "type": "numeric", "answer": "0.75"},
]


class TestParseModelJson:
    def test_plain_groups(self):
        p = gemini.parse_model_json('{"workbook_title": "", "groups": []}')
        assert p["groups"] == []

    def test_legacy_entries_wrapped_as_group(self):
        p = gemini.parse_model_json('{"entries": [], "notes": []}')
        assert len(p["groups"]) == 1
        assert p["groups"][0]["main_category"] == "전체"
        assert p["notes"] == []

    def test_fenced(self):
        text = (
            '```json\n{"workbook_title": "", "groups": [{"main_category": "Day 01",'
            ' "items": [{"number": 1, "type": "numeric", "answer": "5"}]}]}\n```'
        )
        p = gemini.parse_model_json(text)
        assert p["groups"][0]["items"][0]["answer"] == "5"

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "not json", '[{"a": 1}]', '{"notes": []}'],
    )
    def test_invalid_raises(self, text):
        with pytest.raises(GeminiResponseError):
            gemini.parse_model_json(text)


class TestCategoryType:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Day 01", "day"),
            ("2일차", "day"),
            ("01 힘과 운동", "chapter"),
            ("Chapter 3", "chapter"),
            ("Unit 4", "unit"),
            ("3단원", "unit"),
            ("Lesson 7", "lesson"),
            ("수능 2점 테스트", "step"),
            ("기타 묶음", "chapter"),
        ],
    )
    def test_classification(self, label, expected):
        assert gemini._category_type(label) == expected

    def test_sub_label_merged(self):
        assert gemini.group_label("01 힘과 운동", "수능 2점 테스트") == (
            "01 힘과 운동 - 수능 2점 테스트"
        )
        assert gemini.group_label("Day 02", None) == "Day 02"
        assert gemini.group_label("", None) == "전체"


class TestValidateGroups:
    def test_table_layout_sorted_and_canonical(self):
        shuffled = [TABLE_LAYOUT[2], TABLE_LAYOUT[0], TABLE_LAYOUT[3], TABLE_LAYOUT[1]]
        groups, notes = gemini.validate_groups(_payload(groups=[
            {"main_category": "Day 01", "sub_category": None, "items": shuffled}
        ]))
        entries = groups[0]["entries"]
        assert [e["number"] for e in entries] == [1, 2, 10, 12]
        assert [e["answer"] for e in entries] == ["3", "-4.5", "ㄱ", "0.75"]
        assert {e["qtype"] for e in entries} == {"multiple_choice", "numeric"}
        assert notes == []

    def test_multiple_groups_kept_separate(self):
        payload = _payload(
            title="쎈 미적분",
            groups=[
                {
                    "main_category": "Day 01",
                    "sub_category": None,
                    "items": [{"number": 1, "type": "numeric", "answer": "151"}],
                },
                {
                    "main_category": "01 힘과 운동",
                    "sub_category": "수능 2점 테스트",
                    "items": [
                        {"number": 1, "type": "multiple_choice", "answer": "④①"},
                        {"number": 2, "type": "numeric", "answer": "256"},
                    ],
                },
                {
                    "main_category": "빈 그룹",
                    "sub_category": None,
                    "items": [],
                },
            ],
        )
        groups, _ = gemini.validate_groups(payload)
        assert [(g["main_category"], g["sub_category"]) for g in groups] == [
            ("Day 01", None),
            ("01 힘과 운동", "수능 2점 테스트"),
        ]
        assert groups[0]["entries"][0]["answer"] == "151"
        assert groups[1]["entries"][0]["answer"] == "1,4"

    def test_same_number_allowed_in_different_groups(self):
        payload = _payload(groups=[
            {
                "main_category": "A",
                "items": [{"number": 1, "type": "numeric", "answer": "1"}],
            },
            {
                "main_category": "B",
                "items": [{"number": 1, "type": "numeric", "answer": "9"}],
            },
        ])
        groups, _ = gemini.validate_groups(payload)
        assert groups[0]["entries"][0]["answer"] == "1"
        assert groups[1]["entries"][0]["answer"] == "9"

    def test_multi_select_mc(self):
        groups, _ = gemini.validate_groups(
            _payload(groups=[{
                "main_category": "Day 01",
                "sub_category": None,
                "items": [
                    {"number": 7, "type": "multiple_choice", "answer": "④①"},
                    {"number": 8, "type": "multiple_choice", "answer": "B,D"},
                ],
            }])
        )
        by_num = {e["number"]: e["answer"] for e in groups[0]["entries"]}
        assert by_num == {7: "1,4", 8: "B,D"}

    def test_thousands_separator_numeric(self):
        groups, _ = gemini.validate_groups(
            _payload(entries=[{"number": 3, "type": "numeric", "answer": "1,234"}])
        )
        entry = groups[0]["entries"][0]
        assert entry["answer"] == "1234"
        assert entry["answer_display"] == "1,234"

    def test_unsupported_type_skipped_with_note(self):
        payload = _payload(entries=[
            {"number": 1, "type": "word_problem", "answer": "x=3"},
            {"number": 2, "type": "numeric", "answer": "5"},
        ])
        groups, notes = gemini.validate_groups(payload)
        assert [e["number"] for e in groups[0]["entries"]] == [2]
        assert any("1" in n and "지원하지 않는" in n for n in notes)

    def test_word_answer_for_numeric_slot_skipped(self):
        payload = _payload(entries=[
            {"number": 1, "type": "numeric", "answer": "무한대"},
            {"number": 2, "type": "numeric", "answer": "x^2"},
        ])
        groups, notes = gemini.validate_groups(payload)
        assert groups == []
        assert len(notes) == 2

    @pytest.mark.parametrize("num", [0, -1, 1000])
    def test_out_of_range_numbers_skipped(self, num):
        groups, notes = gemini.validate_groups(
            _payload(entries=[{"number": num, "type": "numeric", "answer": "1"}])
        )
        assert groups == []
        assert any("잘못된 문항 번호" in n for n in notes)

    def test_bool_number_rejected(self):
        groups, notes = gemini.validate_groups(
            _payload(entries=[{"number": True, "type": "numeric", "answer": "1"}])
        )
        assert groups == []
        assert notes

    def test_duplicate_number_last_wins_within_group(self):
        payload = _payload(entries=[
            {"number": 5, "type": "numeric", "answer": "1"},
            {"number": 5, "type": "numeric", "answer": "2"},
        ])
        groups, _ = gemini.validate_groups(payload)
        assert len(groups[0]["entries"]) == 1
        assert groups[0]["entries"][0]["answer"] == "2"

    def test_non_dict_and_missing_fields_ignored(self):
        payload = _payload(entries=["oops", {"type": "numeric", "answer": "1"}, None, 42])
        groups, _ = gemini.validate_groups(payload)
        assert groups == []


class TestValidateEntriesCompat:
    def test_flat_payload_still_works(self):
        merged, notes = gemini.validate_entries({"entries": TABLE_LAYOUT})
        assert [e["number"] for e in merged] == [1, 2, 10, 12]
        assert notes == []


class TestExtractAnswerKey:
    def test_success_returns_structured(self, monkeypatch):
        resp = FakeResponse(json.dumps(_payload(TABLE_LAYOUT, title="쎈 미적분")))
        _patch(monkeypatch, FlakyModels(0, response=resp))
        out = gemini.extract_answer_key(b"img-bytes", "image/jpeg")
        assert out["model"] == "gemini-test"
        assert out["workbook_title"] == "쎈 미적분"
        assert len(out["entries"]) == 4
        assert out["headers"] == [
            {"type": "day", "label": "Day 01", "index": 0, "line": 0}
        ]
        assert [e["line"] for e in out["entries"]] == [0, 1, 2, 3]
        assert out["raw_text"].startswith("1. 3")
        assert out["raw_text"].split("\n")[1] == "2. -4.5"

    def test_multi_group_lines_and_headers(self, monkeypatch):
        resp = FakeResponse(json.dumps(_payload(title="T", groups=[
            {
                "main_category": "Day 01",
                "sub_category": None,
                "items": TABLE_LAYOUT[:2],
            },
            {
                "main_category": "01 힘과 운동",
                "sub_category": "수능 2점 테스트",
                "items": TABLE_LAYOUT[2:],
            },
        ])))
        _patch(monkeypatch, FlakyModels(0, response=resp))
        out = gemini.extract_answer_key(b"img", "image/png")
        assert [(h["label"], h["line"]) for h in out["headers"]] == [
            ("Day 01", 0),
            ("01 힘과 운동 - 수능 2점 테스트", 2),
        ]
        assert [h["type"] for h in out["headers"]] == ["day", "chapter"]
        nums_by_line = {e["line"]: e["number"] for e in out["entries"]}
        assert nums_by_line == {0: 1, 1: 2, 2: 10, 3: 12}

    def test_no_api_key_raises_unavailable(self, monkeypatch):
        monkeypatch.setattr(config, "GEMINI_API_KEY", "")
        with pytest.raises(GeminiUnavailableError):
            gemini.extract_answer_key(b"x", "image/png")

    @pytest.mark.parametrize(
        "data",
        [b"", b"x" * (config.MAX_UPLOAD_BYTES + 1)],
    )
    def test_size_guards(self, monkeypatch, data):
        _patch(monkeypatch, FlakyModels(0))
        with pytest.raises(GeminiResponseError):
            gemini.extract_answer_key(data, "image/png")

    def test_retry_then_success(self, monkeypatch):
        resp = FakeResponse(
            json.dumps(_payload([{"number": 1, "type": "numeric", "answer": "9"}]))
        )
        models = FlakyModels(2, response=resp)
        _patch(monkeypatch, models)
        out = gemini.extract_answer_key(b"img", "image/png")
        assert out["entries"][0]["answer"] == "9"
        assert models.calls == 3

    def test_all_retries_exhausted(self, monkeypatch):
        models = FlakyModels(99)
        _patch(monkeypatch, models)
        with pytest.raises(GeminiResponseError):
            gemini.extract_answer_key(b"img", "image/png")

    def test_empty_text_response(self, monkeypatch):
        _patch(monkeypatch, FlakyModels(0, response=FakeResponse(None)))
        with pytest.raises(GeminiResponseError):
            gemini.extract_answer_key(b"img", "image/png")


class TestPromptContract:
    def test_prompt_mentions_only_two_types(self):
        low = gemini.SYSTEM_PROMPT.lower()
        assert '"multiple_choice"' in low.replace("'", '"') or \
            "multiple_choice" in low
        assert "numeric" in low

    def test_prompt_forbids_arbitrary_chunking(self):
        low = gemini.SYSTEM_PROMPT.lower()
        assert "no arbitrary chunking" in low
        assert "spanning" in low
        assert "main_category" in low
        assert "sub_category" in low

    def test_schema_enum_locked(self):
        enum = gemini._RESPONSE_SCHEMA["properties"]["groups"]["items"]["properties"][
            "items"
        ]["items"]["properties"]["type"]["enum"]
        assert sorted(enum) == ["multiple_choice", "numeric"]

    def test_schema_requires_semantic_fields(self):
        group_props = gemini._RESPONSE_SCHEMA["properties"]["groups"]["items"]
        assert set(group_props["required"]) == {"main_category", "items"}
        top_required = gemini._RESPONSE_SCHEMA["required"]
        assert "workbook_title" in top_required and "groups" in top_required
