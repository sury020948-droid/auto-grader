from app.services.normalizer import (
    answer_matches_type,
    answers_equal,
    canonical_type,
    classify_answer,
    normalize_answer,
    normalize_mc,
    normalize_numeric,
)


class TestNormalizeAnswer:
    def test_plain_digit(self):
        assert normalize_answer("3") == "3"

    def test_circled_multi(self):
        assert normalize_answer("①③") == "1,3"

    def test_circled_sorted_numeric(self):
        assert normalize_answer("④①") == "1,4"

    def test_fullwidth(self):
        assert normalize_answer("３") == "3"

    def test_paren_wrapped(self):
        assert normalize_answer("(2)") == "2"
        assert normalize_answer("[ㄱ]") == "ㄱ"

    def test_prefix_and_bun(self):
        assert normalize_answer("정답: 4번") == "4"
        assert normalize_answer("답 12") == "12"

    def test_case_insensitive_mc_letters(self):
        assert normalize_answer(" b ") == "B"
        assert normalize_answer("e") == "E"

    def test_out_of_range_letters_rejected(self):
        assert normalize_answer("O") == ""
        assert normalize_answer("x") == ""

    def test_multi_separators(self):
        assert normalize_answer("3,5") == "3,5"
        assert normalize_answer("3 또는 5") == "3,5"
        assert normalize_answer("ㄱ, ㄴ") == "ㄱ,ㄴ"

    def test_slash_rejected_as_fraction_risk(self):
        assert normalize_answer("3 / 5") == ""

    def test_fraction_rejected(self):
        assert normalize_answer("3/4") == ""
        assert classify_answer("3/4") is None

    def test_range_rejected(self):
        assert normalize_answer("3~5") == ""
        assert classify_answer("3-4") is None

    def test_word_answers_rejected(self):
        assert normalize_answer("가,나") == ""
        assert normalize_answer("사과") == ""

    def test_numeric_with_thousands_comma(self):
        assert normalize_answer("1,234") == "1234"
        assert classify_answer("1,234") == "numeric"

    def test_negative_and_decimal(self):
        assert normalize_answer("-1.5") == "-1.5"
        assert normalize_answer("(0.5)") == "0.5"
        assert normalize_answer("+7") == "7"
        assert normalize_answer("-0.0") == "0"

    def test_trailing_zeros_collapsed(self):
        assert normalize_numeric("3.10") == "3.1"
        assert normalize_numeric("3.0") == "3"
        assert normalize_numeric("100") == "100"


class TestClassifyAndMatch:
    def test_classify_numeric(self):
        assert classify_answer("-4.5") == "numeric"
        assert classify_answer("42") == "numeric"
        assert classify_answer("2,000") == "numeric"

    def test_classify_mc(self):
        assert classify_answer("③") == "multiple_choice"
        assert classify_answer("B") == "multiple_choice"
        assert classify_answer("ㄱ") == "multiple_choice"
        assert classify_answer("①③") == "multiple_choice"

    def test_classify_unsupported(self):
        assert classify_answer("정답: x^2") is None
        assert classify_answer("") is None
        assert classify_answer(None) is None

    def test_matches_type(self):
        assert answer_matches_type("3", "numeric")
        assert answer_matches_type("3", "multiple_choice")
        assert not answer_matches_type("1,3", "numeric")
        assert answer_matches_type("1,3", "multiple_choice")
        assert not answer_matches_type("사과", "multiple_choice")

    def test_canonical_type(self):
        assert canonical_type("-1.5") == "numeric"
        assert canonical_type("B") == "multiple_choice"
        assert canonical_type("1,3") == "multiple_choice"

    def test_normalize_mc_direct(self):
        assert normalize_mc("a,c") == "A,C"
        assert normalize_mc("⑤②") == "2,5"
        assert normalize_mc("ㅁ") == "ㅁ"

    def test_empty(self):
        assert normalize_answer("") == ""
        assert normalize_answer(None) == ""
        assert normalize_answer("   ") == ""

    def test_trailing_period(self):
        assert normalize_answer("5.") == "5"


class TestAnswersEqual:
    def test_exact(self):
        assert answers_equal("3", "3")

    def test_normalized_match(self):
        assert answers_equal("3", "③ ")
        assert answers_equal("1,4", "④,①")

    def test_numeric_equivalence(self):
        assert answers_equal("3.0", "3")
        assert answers_equal("1234", "1,234")
        assert answers_equal("-1.5", "-1.50")
        assert not answers_equal("3", "3.0001")

    def test_set_order_free(self):
        assert answers_equal("1,4", "4 1".replace(" ", ","))

    def test_wrong_value(self):
        assert not answers_equal("3", "4")

    def test_single_vs_set_mismatch(self):
        assert not answers_equal("1,4", "1")

    def test_blank_given(self):
        assert not answers_equal("3", "")
        assert not answers_equal("3", "  ")

    def test_korean_jamo(self):
        assert answers_equal("ㄱ", "ㄱ")
        assert not answers_equal("ㄱ", "ㄴ")

    def test_word_answer_never_correct(self):
        assert not answers_equal("3", "사과")
        assert not answers_equal("ㄱ", "정답 아님")
