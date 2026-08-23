from app.services.grader import grade


def _keys(spec):
    canonical = {}
    display = {}
    for num, (c, d) in spec.items():
        canonical[num] = c
        display[num] = d
    return canonical, display


class TestGrade:
    def test_mixed_statuses(self):
        kc, kd = _keys({1: ("3", "3"), 2: ("1", "1"), 3: ("4", "4")})
        out = grade(kc, kd, {"1": "3", "2": "2"})
        assert out["score"] == 1
        assert out["total"] == 3
        assert out["percent"] == 33.3
        statuses = {r["number"]: r["status"] for r in out["results"]}
        assert statuses == {1: "correct", 2: "incorrect", 3: "unanswered"}
        assert out["wrong_numbers"] == [2]
        assert out["unanswered_numbers"] == [3]

    def test_set_answers(self):
        kc, kd = _keys({5: ("1,4", "①④")})
        out = grade(kc, kd, {"5": "④,①"})
        assert out["score"] == 1

    def test_normalized_given(self):
        kc, kd = _keys({1: ("3", "(3)")})
        out = grade(kc, kd, {"1": "③ "})
        assert out["results"][0]["status"] == "correct"

    def test_expected_display_passthrough(self):
        kc, kd = _keys({7: ("ㄱ", "(ㄱ)")})
        out = grade(kc, kd, {"7": "ㄴ"})
        r = out["results"][0]
        assert r["expected"] == "(ㄱ)"
        assert r["given"] == "ㄴ"
        assert r["status"] == "incorrect"

    def test_extra_inputs_flagged(self):
        kc, kd = _keys({1: ("1", "1")})
        out = grade(kc, kd, {"1": "1", "99": "5", "100": ""})
        assert out["extra_ignored"] == [99]

    def test_empty_answers_all_unanswered(self):
        kc, kd = _keys({1: ("1", "1"), 2: ("2", "2")})
        out = grade(kc, kd, {})
        assert out["score"] == 0
        assert out["percent"] == 0.0
        assert len(out["unanswered_numbers"]) == 2

    def test_string_keys_with_whitespace(self):
        kc, kd = _keys({10: ("B", "B")})
        out = grade(kc, kd, {"10": " b "})
        assert out["score"] == 1

    def test_results_sorted_by_number(self):
        kc, kd = _keys({9: ("1", "1"), 2: ("1", "1"), 15: ("1", "1")})
        out = grade(kc, kd, {})
        assert [r["number"] for r in out["results"]] == [2, 9, 15]

    def test_numeric_equivalence_grading(self):
        kc, kd = _keys({1: ("3.0", "3.0"), 2: ("-1.5", "-1.5"), 3: ("1234", "1,234")})
        out = grade(kc, kd, {"1": "3", "2": "-1.50", "3": "1,234"})
        assert out["score"] == 3

    def test_numeric_wrong_value(self):
        kc, kd = _keys({1: ("0.75", "0.75")})
        out = grade(kc, kd, {"1": ".76"})
        assert out["score"] == 0
        assert out["wrong_numbers"] == [1]

    def test_qtype_metadata_per_result(self):
        kc, kd = _keys({1: ("3", "3"), 2: ("B", "B"), 3: ("1,4", "①④")})
        out = grade(kc, kd, {"1": "3", "2": "A", "3": "④,①"})
        qtypes = {r["number"]: r["qtype"] for r in out["results"]}
        assert qtypes[1] == "numeric"
        assert qtypes[2] == "multiple_choice"
        assert qtypes[3] == "multiple_choice"

    def test_free_text_given_is_incorrect_not_crash(self):
        kc, kd = _keys({1: ("3", "3")})
        out = grade(kc, kd, {"1": "'; DROP TABLE workbooks;--"})
        assert out["results"][0]["status"] == "incorrect"
