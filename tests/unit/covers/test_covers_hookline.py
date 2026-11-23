"""Cover engine C4 (docs/29a stage 3, section 6.3): the cover line checks. No network."""

from sixstars.covers import hookline
from sixstars.covers.hookline import LineContext, validate

TRANSCRIPT = [
    "oh",
    "my",
    "god",
    "i",
    "did",
    "it",
    "six",
    "stars",
    "bro",
    "we",
    "got",
    "six",
    "stars",
    "they",
    "are",
    "everywhere",
]
CONTEXT = LineContext(
    title="Kai Cenat Gets A Six Star Wanted Level In GTA 6",
    transcript_words=TRANSCRIPT,
    recent_lines=["THEY ARE EVERYWHERE"],
    profanity=frozenset({"fuck", "shit"}),
)


def check(text: str, source: str = "oh my god i did it", style: str = "quote") -> hookline.Candidate:
    return validate({"text": text, "style": style, "key_word": "", "source": source}, CONTEXT)


def test_a_good_quote_passes() -> None:
    line = check("OH MY GOD I DID", "oh my god i did")
    assert line.valid and line.lines == ("OH MY GOD I", "DID") and line.key_word == "GOD"


def test_wrap() -> None:
    assert hookline.wrap("FIRST CAR STOLEN") == ("FIRST CAR", "STOLEN")
    assert hookline.wrap("ABSOLUTELYUNREAL") is None  # one word over 14 characters
    assert hookline.wrap("ONE TWO THREE FOUR FIVE SIX SEVEN") is None


def test_rules() -> None:
    assert "words=6" in check("OH MY GOD I DID IT", "oh my god i did it").problems
    assert "not_capitals" in check("Oh my god").problems
    assert "emoji" in check("SIX STARS 😭", "six stars bro").problems
    assert "punctuation" in check("SIX STARS?!?!", "six stars bro").problems
    assert check("SIX STARS?!", "six stars bro").valid
    assert "banned:insane" in check("INSANE SIX STARS", "six stars bro").problems
    assert "banned:you won't believe" in check("YOU WON'T BELIEVE", "six stars").problems
    assert "spoils_outcome" in check("HE DIES", "six stars bro").problems
    assert "source_not_in_transcript" in check("FIRST CAR STOLEN", "i stole my first car").problems
    assert "repeats_recent" in check("THEY ARE EVERYWHERE!", "they are everywhere").problems


def test_title_overlap() -> None:
    # Title content words: kai, cenat, gets, six, star, wanted, level.
    assert "repeats_title" in check("SIX STAR WANTED", "six stars bro").problems
    assert check("WE GOT SIX STARS", "we got six stars").valid  # "six" only (stars != star)


def test_profanity_is_masked_f_star_ck() -> None:
    ctx = LineContext(
        title="", transcript_words=["what", "the", "fuck", "is", "that"], profanity=frozenset({"fuck"})
    )
    line = validate({"text": "WHAT THE FUCK", "style": "quote", "source": "what the fuck"}, ctx)
    assert line.text == "WHAT THE F*CK" and line.valid


def test_source_fuzzy_match() -> None:
    assert hookline.source_found("we got six stars", TRANSCRIPT)
    assert not hookline.source_found("the police are here", TRANSCRIPT)


def test_transcript_window_and_values() -> None:
    words = [(w, i * 1.0, i * 1.0 + 0.5) for i, w in enumerate(TRANSCRIPT)]
    window = hookline.transcript_window(words, 3.0, pad_s=1.0)
    assert window == ["god", "i", "did"]
    item = hookline.LineInput("c1", "Title", "kai", ["police chase"], words, 3.0, ["A", "B"])
    values = hookline.prompt_values(item)
    assert values["tags"] == "police chase" and values["recent_lines"] == '["A", "B"]'
    assert "did it" in str(values["transcript"])
