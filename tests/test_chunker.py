from assistant.voice.chunker import SentenceChunker


def run(deltas, **kw):
    c = SentenceChunker(**kw)
    out = []
    for d in deltas:
        out += c.feed(d)
    return out + c.flush()


def test_first_chunk_breaks_at_clause():
    out = run(["Certainly sir, ", "the time is ", "half past three. ", "Anything else?"])
    assert out == ["Certainly sir,", "the time is half past three.", "Anything else?"]


def test_later_chunks_wait_for_sentence_end():
    out = run(["Done. ", "Volume is now thirty, ", "and muted", "."])
    assert out == ["Done.", "Volume is now thirty, and muted."]


def test_abbreviations_and_decimals_do_not_split():
    # A complete sentence already in the buffer beats an earlier clause break.
    out = run(["Mr. Smith paid 3.5 dollars, e.g. a coffee. Then left."])
    assert out == ["Mr. Smith paid 3.5 dollars, e.g. a coffee.", "Then left."]


def test_no_split_before_honorific():
    assert run(["One moment,", " sir", ". Checking now."]) == ["One moment, sir.", "Checking now."]
    assert run(["Good afternoon, sir, ", "how can I help?"]) == ["Good afternoon, sir,", "how can I help?"]


def test_clause_waits_for_next_word():
    c = SentenceChunker()
    assert c.feed("Right away, ") == []
    assert c.feed("opening") == []
    assert c.feed(" it") == ["Right away,"]


def test_token_by_token_stream():
    text = "It is sunny today. High of twenty. Low of ten."
    out = run(list(text))
    assert out == ["It is sunny today.", "High of twenty.", "Low of ten."]


def test_run_on_text_is_capped():
    out = run(["word " * 100], max_chars=60)
    assert all(len(c) <= 60 for c in out) and len(out) > 1


def test_flush_empty():
    assert SentenceChunker().flush() == []
