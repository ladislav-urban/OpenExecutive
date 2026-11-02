"""Sentences a reply says twice (utils.repeats)."""
from __future__ import annotations

from openexecutive.utils.repeats import new_line, repeated_sentences


def test_a_reworded_repeat_is_found() -> None:
    reply = (
        "Whether to release it is Rachel Dunmore's call; she holds customer credit. "
        "Harbor owes $4,656.\n\n"
        "Whether to release it is Rachel Dunmore's decision; she holds customer credit."
    )
    assert repeated_sentences(reply) == [
        "Whether to release it is Rachel Dunmore's decision; she holds customer credit."
    ]


def test_a_repeated_closing_line_is_found() -> None:
    reply = (
        "Denver's margin is 18%. I've opened the report beside the panel.\n\n"
        "I have opened the report beside the panel."
    )
    assert repeated_sentences(reply) == ["I have opened the report beside the panel."]


def test_different_sentences_and_short_ones_pass() -> None:
    reply = (
        "Metro owes $4,700 at 70 days. Riverbend owes $2,900 at 40 days. "
        "Brightline owes $600 at 5 days. Yes. Yes."
    )
    assert repeated_sentences(reply) == []


def test_new_line_keeps_only_what_is_new_and_short() -> None:
    earlier = "Whether to release it is Rachel's call; she holds customer credit."
    text = (
        "Whether to release it is Rachel's decision; she holds customer credit. "
        "I couldn't leave her a note from here. " + "More detail follows here. " * 20
    )
    line = new_line(earlier, text)
    assert line.startswith("I couldn't leave her a note from here.")
    assert len(line) <= 300
    assert new_line(earlier, earlier) == ""
