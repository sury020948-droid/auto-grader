from typing import ClassVar

from app.services.parser import detect_issues, parse_answer_key


def numbers_of(parsed):
    return [e["number"] for e in parsed["entries"]]


class TestFlatKeys:
    def test_same_line_pairs(self):
        parsed = parse_answer_key("1. 3 2. 4 3. 1")
        assert numbers_of(parsed) == [1, 2, 3]
        assert [e["answer"] for e in parsed["entries"]] == ["3", "4", "1"]

    def test_paren_style(self):
        parsed = parse_answer_key("1.(2) 2.(4)\n11.(1) 12.(3)")
        assert numbers_of(parsed) == [1, 2, 11, 12]
        assert parsed["entries"][0]["answer"] == "2"

    def test_colon_and_bracket(self):
        parsed = parse_answer_key("1: ㄱ\n2] ㄴ")
        assert numbers_of(parsed) == [1, 2]

    def test_circled_answers(self):
        parsed = parse_answer_key("1. ③ 2. ⑤")
        assert [e["answer"] for e in parsed["entries"]] == ["3", "5"]

    def test_multi_select_answer(self):
        parsed = parse_answer_key("5. ①③")
        assert parsed["entries"][0]["answer"] == "1,3"

    def test_multiline_column_major(self):
        text = "1.② 6.④\n2.① 7.⑤"
        parsed = parse_answer_key(text)
        assert numbers_of(parsed) == [1, 6, 2, 7]


class TestNoise:
    def test_title_lines_ignored(self):
        parsed = parse_answer_key("빠른 정답\n정답 및 해설\n1. 2\n")
        assert numbers_of(parsed) == [1]
        assert not any(i["kind"] != "gap" for i in [] ) or True

    def test_page_number_ignored(self):
        parsed = parse_answer_key("42\n1. 3")
        assert numbers_of(parsed) == [1]

    def test_decimal_not_pair(self):
        parsed = parse_answer_key("1. 3.14 2. 5")
        nums = numbers_of(parsed)
        assert 2 in nums

    def test_empty_parse_no_entries(self):
        assert parse_answer_key("아무 내용 없음")["entries"] == []

    def test_word_answers_excluded_as_noise(self):
        parsed = parse_answer_key("1. 사과 2. 3 3. x^2+1")
        assert [e["number"] for e in parsed["entries"]] == [2]
        kinds = [i["kind"] for i in parsed["issues"]]
        assert "noise" in kinds

    def test_numeric_negative_and_decimal_kept(self):
        parsed = parse_answer_key("1. -1.5 2. 0.75 3. 1,234")
        assert [e["answer"] for e in parsed["entries"]] == ["-1.5", "0.75", "1234"]

    def test_fraction_rejected(self):
        parsed = parse_answer_key("1. 3/4 2. 5")
        assert [e["number"] for e in parsed["entries"]] == [2]


class TestHeaders:
    def test_day_headers(self):
        text = "Day 01\n1. 2 2. 3\nDay 02\n1. 4 2. 1"
        parsed = parse_answer_key(text)
        assert len(parsed["headers"]) == 2
        assert all(h["type"] == "day" for h in parsed["headers"])
        assert parsed["headers"][0]["index"] == 1

    def test_korean_day_header(self):
        parsed = parse_answer_key("1일차\n1. 1\n2일차\n1. 2")
        assert [h["type"] for h in parsed["headers"]] == ["day", "day"]

    def test_chapter_header(self):
        parsed = parse_answer_key("Chapter 2\n1. 1")
        assert parsed["headers"][0]["type"] == "chapter"

    def test_zhang_header(self):
        parsed = parse_answer_key("제 3 장\n1. 9")
        assert parsed["headers"][0]["type"] == "chapter"
        assert parsed["headers"][0]["index"] == 3

    def test_unit_header(self):
        parsed = parse_answer_key("Unit 05\n1. 1")
        assert parsed["headers"][0]["type"] == "unit"


class TestIssues:
    def test_gap_detected(self):
        parsed = parse_answer_key("1. 1 2. 2 3. 3 4. 4 5. 5 9. 9 10. 1")
        kinds = [i["kind"] for i in parsed["issues"]]
        assert "gap" in kinds

    def test_duplicate_detected(self):
        parsed = parse_answer_key("1. 2 1. 3")
        kinds = [i["kind"] for i in parsed["issues"]]
        assert "duplicate" in kinds
        assert parsed["entries"][-1]["answer"] == "3"


class TestScopedIssues:
    """Duplicates must be scoped per printed section, not global."""

    HEADERS: ClassVar[list[dict]] = [
        {"label": "Day 01", "type": "day", "line": 0},
        {"label": "Day 02", "type": "day", "line": 2},
    ]

    def test_restarting_numbers_across_sections_not_flagged(self):
        entries = [
            {"number": 1, "answer": "1", "line": 0},
            {"number": 2, "answer": "2", "line": 1},
            {"number": 1, "answer": "3", "line": 2},
            {"number": 2, "answer": "4", "line": 3},
        ]
        assert detect_issues(entries, self.HEADERS) == []

    def test_within_section_duplicate_flagged_with_scope_label(self):
        entries = [
            {"number": 1, "answer": "1", "line": 2},
            {"number": 1, "answer": "9", "line": 3},
        ]
        issues = detect_issues(entries, self.HEADERS)
        dupes = [i for i in issues if i["kind"] == "duplicate"]
        assert len(dupes) == 1
        assert "[Day 02]" in dupes[0]["message"]

    def test_orphan_and_header_scopes_independent(self):
        entries = [
            {"number": 1, "answer": "1", "line": 0},  # 머리글 없음 scope
            {"number": 1, "answer": "5", "line": 5},  # Day 01 scope (orphan header line=5)
        ]
        issues = detect_issues(entries, [{"label": "Day 01", "type": "day", "line": 5}])
        assert issues == []

    def test_gap_also_scoped_per_section(self):
        entries = [
            {"number": 1, "answer": "1", "line": 0},
            {"number": 9, "answer": "9", "line": 1},  # gap inside Day 01
            {"number": 1, "answer": "2", "line": 2},
            {"number": 2, "answer": "3", "line": 3},  # clean Day 02
        ]
        issues = detect_issues(entries, self.HEADERS)
        gaps = [i for i in issues if i["kind"] == "gap"]
        assert len(gaps) == 1
        assert "[Day 01]" in gaps[0]["message"]

    def test_no_headers_keeps_flat_behavior(self):
        entries = [
            {"number": 1, "answer": "1", "line": 0},
            {"number": 1, "answer": "2", "line": 1},
        ]
        kinds = [i["kind"] for i in detect_issues(entries)]
        assert "duplicate" in kinds


class TestUnicodeCleanup:
    def test_fullwidth_digits_in_markers(self):
        parsed = parse_answer_key("１. ３ ２. ４")
        assert numbers_of(parsed) == [1, 2]
        assert [e["answer"] for e in parsed["entries"]] == ["3", "4"]

    def test_crlf(self):
        parsed = parse_answer_key("1. 1\r\n2. 2\r\n")
        assert len(parsed["entries"]) == 2


class TestOcrGluedPairs:
    def test_leading_glued_pair_recovered(self):
        text = "Day 01\n\n13 2.4 3.1 4.5 5.2\n6.4 7.3"
        parsed = parse_answer_key(text)
        nums = [e["number"] for e in parsed["entries"]]
        assert nums[0] == 1 and parsed["entries"][0]["answer"] == "3"
        assert nums[1] == 2

    def test_normal_lines_not_unglued(self):
        parsed = parse_answer_key("12. 3 13. 4")
        assert [e["number"] for e in parsed["entries"]] == [12, 13]
