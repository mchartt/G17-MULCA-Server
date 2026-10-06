from muca_server.cognition.query_extraction import extract_query


def test_the_exact_phrasing_from_the_spec():
    assert extract_query("please tell me something about coffee") == "coffee"


def test_tell_me_about_without_something():
    assert extract_query("tell me about the budget plan") == "the budget plan"


def test_what_do_you_know_about():
    assert extract_query("what do you know about Ada's proposal") == "Ada's proposal"


def test_do_you_remember_anything_about():
    assert (
        extract_query("do you remember anything about the deadline")
        == "the deadline"
    )


def test_what_did_we_say_about():
    assert extract_query("what did we say about the timeline") == "the timeline"


def test_remind_me_about():
    assert extract_query("remind me about the vendor contract") == "the vendor contract"


def test_do_you_know_about_without_anything():
    assert extract_query("do you know about the espresso machine") == (
        "the espresso machine"
    )


def test_can_you_tell_me_about_matches_via_the_tell_me_about_pattern():
    # search(), not match(): a wrapper pattern matches anywhere in the
    # message, so a leading "can you" needs no pattern of its own.
    assert extract_query("can you tell me about the espresso machine") == (
        "the espresso machine"
    )


def test_a_direct_address_prefix_is_stripped_first():
    assert (
        extract_query("MULCA, tell me something about coffee") == "coffee"
    )
    assert extract_query("@MULCA tell me about coffee") == "coffee"


def test_trailing_punctuation_is_stripped():
    assert extract_query("tell me about coffee?") == "coffee"
    assert extract_query("tell me about coffee.") == "coffee"


def test_matching_is_case_insensitive():
    assert extract_query("TELL ME ABOUT Coffee") == "Coffee"


def test_an_unmatched_phrasing_falls_back_to_the_whole_message():
    assert extract_query("so what's the deal with coffee anyway") == (
        "so what's the deal with coffee anyway"
    )


def test_the_fallback_still_strips_a_leading_address():
    assert extract_query("MULCA, what's the deal with coffee") == (
        "what's the deal with coffee"
    )
