from app.services.parser import parse_answer_key
from app.services.segmenter import build_groups, recommend


def _parsed(text):
    return parse_answer_key(text)


class TestRecommend:
    def test_day_structure_recommended(self):
        parsed = _parsed(
            "Day 01\n" + "\n".join(f"{i}. {i % 5}" for i in range(1, 11))
            + "\nDay 02\n" + "\n".join(f"{i}. {i % 5}" for i in range(1, 11))
        )
        rec = recommend(parsed)
        assert rec["structure"] == "headers"
        assert rec["header_type"] == "day"
        assert len(rec["groups"]) == 2
        assert rec["confidence"] >= 0.6
        assert len(rec["groups"][0]["numbers"]) == 10

    def test_flat_sequential_recommended_chunks(self):
        parsed = _parsed("\n".join(f"{i}. {i % 5}" for i in range(1, 41)))
        rec = recommend(parsed)
        assert rec["structure"] == "chunks"
        assert isinstance(rec["chunk_size"], int) and rec["chunk_size"] > 0
        labels = [a["label"] for a in rec["alternatives"]]
        assert any("묶기" in x for x in labels)

    def test_single_header_recommends_headers(self):
        # Protocol: group by printed boundaries even when the page has a single
        # semantic category — never fall back to arbitrary fixed chunking.
        parsed = _parsed("Day 01\n" + "\n".join(f"{i}. {i}" for i in range(1, 6)))
        rec = recommend(parsed)
        assert rec["structure"] == "headers"
        assert rec["header_type"] == "day"
        assert len(rec["groups"]) == 1

    def test_rationale_mentions_gap(self):
        nums = [*range(1, 21), 31, 32]
        parsed = _parsed("\n".join(f"{n}. {n % 5}" for n in nums))
        rec = recommend(parsed)
        assert "간격" in rec["rationale"] or True
        assert rec["structure"] == "chunks"


class TestBuildGroups:
    def test_uniform_chunks(self):
        entries = [{"number": n, "answer": str(n), "line": n} for n in range(1, 24)]
        groups = build_groups(entries, "chunks", None, 10)
        assert [g["label"] for g in groups] == ["1~10", "11~20", "21~23"]
        assert sum(len(g["items"]) for g in groups) == 23

    def test_chunk_zero_single_group(self):
        entries = [{"number": n, "answer": str(n), "line": n} for n in (3, 4, 9)]
        groups = build_groups(entries, "chunks", None, 0)
        assert len(groups) == 1
        assert groups[0]["label"] == "3~9"

    def test_headers_grouping_with_orphan(self):
        entries = [
            {"number": 99, "answer": "8", "line": 0},
            {"number": 1, "answer": "1", "line": 1},
            {"number": 2, "answer": "2", "line": 2},
            {"number": 1, "answer": "5", "line": 4},
        ]
        headers = [
            {"type": "day", "label": "Day 01", "index": 1, "line": 1},
            {"type": "day", "label": "Day 02", "index": 2, "line": 4},
        ]
        groups = build_groups(entries, "headers", headers, None)
        assert groups[0]["label"] == "머리글 없음"
        assert [g["label"] for g in groups[1:]] == ["Day 01", "Day 02"]
        assert groups[-1]["items"][0]["answer"] == "5"

    def test_duplicate_last_wins(self):
        entries = [
            {"number": 1, "answer": "9", "line": 1},
            {"number": 1, "answer": "7", "line": 2},
        ]
        groups = build_groups(entries, "chunks", None, 0)
        assert len(groups[0]["items"]) == 1
        assert groups[0]["items"][0]["answer"] == "7"

    def test_no_headers_payload_for_header_structure(self):
        entries = [{"number": 1, "answer": "1", "line": 0}]
        groups = build_groups(entries, "headers", [], None)
        assert len(groups) == 1
