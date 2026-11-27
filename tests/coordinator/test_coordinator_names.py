"""One name per member in a linked group (coordination/names.py)."""

from muca_server.cognition.coordination.names import distinct_names


def test_a_name_used_once_stays_as_it_is():
    assert distinct_names([(1, "Ada", "web"), (2, "Bruno", "discord")]) == {1: "Ada", 2: "Bruno"}


def test_the_same_name_in_two_channels_gets_the_channel():
    assert distinct_names([(1, "Ada", "web"), (9001, "Ada", "discord")]) == {1: "Ada (Web)", 9001: "Ada (Discord)"}


def test_the_same_name_twice_in_one_channel_gets_a_number_by_id():
    assert distinct_names([(9002, "Luca", "discord"), (9001, "luca", "discord")]) == {9002: "Luca 2", 9001: "luca"}


def test_one_person_with_one_id_is_one_name_whatever_the_channels():
    # After /coordinator me the Discord member IS the web member: same id.
    assert distinct_names([(1, "Ada", "web"), (1, "Ada R.", "web"), (2, "Chiara", "slack")]) == {1: "Ada", 2: "Chiara"}


def test_mixed_twins_and_channels_never_collide():
    names = distinct_names([(1, "Ada", "web"), (9001, "Ada", "discord"), (9002, "Ada", "discord")])
    assert len(set(names.values())) == 3
    assert names[1] == "Ada (Web)"
