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


def test_a_line_saying_it_isnt_done_is_never_a_repeat() -> None:
    # Security review: a negation reads almost like the claim it retracts,
    # and it is the line the claim check's extra round exists for.
    pairs = [
        ("I've noted the hold for her.", "I couldn't note the hold for her."),
        ("I've opened the report beside the panel.", "I couldn't open the report beside the panel."),
        ("I've sent the summary to Rachel.", "I haven't sent the summary to Rachel."),
        ("I've updated the credit limit to $10,000.", "I was unable to update the credit limit to $10,000."),
        ("The order is now released for shipping.", "The order is not released for shipping."),
    ]
    for claim, retraction in pairs:
        assert new_line("Harbor owes $4,656. " + claim, retraction) == retraction


def test_a_failure_line_after_a_long_rewrite_is_kept() -> None:
    earlier = "Harbor owes $4,656, of which $1,900 is late. I've noted the hold for her."
    rewrite = " ".join(f"Point number {i} restates part of the answer in other words." for i in range(8))
    text = rewrite + " I couldn't note the hold for her: there's no tool for that here."
    line = new_line(earlier, text)
    assert "I couldn't note the hold for her: there's no tool for that here." in line
    assert len(line) <= 300


def test_the_comparison_stays_cheap_on_long_text() -> None:
    import time

    long_sentence = "word " * 6000
    many = " ".join(f"Sentence {i} is about item {i * 7} in the list." for i in range(800))
    start = time.monotonic()
    new_line(long_sentence, long_sentence[::-1])
    new_line(many, " ".join(reversed(many.split(". "))))
    repeated_sentences(many + " " + long_sentence)
    assert time.monotonic() - start < 5


def test_a_retraction_of_a_claim_that_says_not_is_kept() -> None:
    # Security review, round 2: both say "not", so the not-guard alone let
    # the retraction be matched with the claim.
    earlier = "Done. I've removed it so it won't show in the brief."
    retraction = "I couldn't remove it, so it will still show in the brief."
    assert new_line(earlier, retraction) == retraction
    # Only word for word does a failure line count as said.
    assert new_line(retraction, retraction) == ""


def test_long_sentences_stay_cheap() -> None:
    import time

    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu"]

    def prose(seed: int) -> str:
        return " ".join(words[(seed * 7 + i * 5) % len(words)] for i in range(45)) + "."

    earlier = " ".join(prose(i) for i in range(200))
    text = " ".join(prose(i + 1000) for i in range(200))
    start = time.monotonic()
    new_line(earlier, text)
    repeated_sentences(earlier + " " + text)
    assert time.monotonic() - start < 10


def test_an_uncontracted_retraction_is_kept() -> None:
    # Security review, round 3: "could not" / "did not" / "have not".
    earlier = "Done. I've removed it so it won't show in the brief."
    for retraction in (
        "I could not remove it, so it will still show in the brief.",
        "I did not remove it, so it will still show in the brief.",
        "I have not removed it, so it will still show in the brief.",
    ):
        assert new_line(earlier, retraction) == retraction


def test_rows_and_items_with_different_figures_are_not_repeats() -> None:
    # Eval erpnext_094 (Nov 18, 2026): a 13-week outlook's table rows and its
    # list of late invoices were flagged in 12 of 15 runs; none was said twice.
    reply = (
        "When Metro's invoices land:\n"
        "- **Invoice for $8,650.46 (due Oct 1):** lands Nov 21.\n"
        "- **Invoice for $332.50 (due Oct 2):** lands Nov 22.\n"
        "- **Invoice for $567.00 (due Oct 21):** lands Dec 11.\n\n"
        "| Week | Baseline | Metro late |\n"
        "|---|---|---|\n"
        "| Jan 13 | 203,526.52 | 203,526.52 |\n"
        "| Jan 20 | 203,526.52 | 203,526.52 |\n"
        "| 10 Jan 20–26 | 203,526.52 | 198,816.52 | 4,710.00 |\n"
        "| 12 Feb 3–9 | 195,644.02 | 190,934.02 | 4,710.00 |\n"
    )
    assert repeated_sentences(reply) == []


def test_a_row_or_figure_sentence_said_twice_is_still_found() -> None:
    row = "| Jan 20 | 203,526.52 | 198,816.52 | -4,710.00 |"
    assert repeated_sentences(f"{row}\n| Jan 27 | 1.00 | 2.00 |\n{row}\n") == [row]
    reply = (
        "Payroll of $5,382.50 falls at the end of each month, with rent on the 2nd. "
        "Payroll of $5382.50 lands at the end of every month, with rent on the 2nd."
    )
    assert repeated_sentences(reply) == [
        "Payroll of $5382.50 lands at the end of every month, with rent on the 2nd."
    ]


def test_prose_restated_with_its_figures_written_another_way_is_still_found() -> None:
    # Only table rows and list items are told apart by their figures.
    reply = (
        "Payroll of $5,382.50 falls at the end of each month, with rent on the 2nd. "
        "Payroll of about $5,400 falls at the end of each month, with rent on the 2nd."
    )
    assert repeated_sentences(reply) == [
        "Payroll of about $5,400 falls at the end of each month, with rent on the 2nd."
    ]


def test_new_line_still_compares_words_only() -> None:
    # The claim check's extra round: a re-asserted claim with a changed figure
    # is still a repeat there, as before (security review of the change).
    earlier = "I've removed the $4,656 Harbor item from your brief for today."
    again = "I've removed the $4,650 Harbor item from your brief for today."
    assert new_line(earlier, again) == ""

