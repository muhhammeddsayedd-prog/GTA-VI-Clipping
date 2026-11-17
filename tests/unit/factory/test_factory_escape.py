"""Escaping for ASS text and ffmpeg filter values, with hostile input."""

import pytest

from sixstars.factory.escape import (
    WORD_JOINER,
    ass_color,
    ass_field,
    ass_override_color,
    ass_text,
    ass_time,
    drawtext_text,
    ffmpeg_color,
    filter_path,
    filter_value,
    normalize_filter_path,
    parse_hex_color,
    render_filter,
    single_line,
)

J = WORD_JOINER


def unescape_filter_value(escaped: str) -> str:
    """What ffmpeg recovers from a value: the graph parser unescapes once, the option parser once more.

    Mirrors `av_get_token`: a backslash keeps the next character, quotes group text. No quotes are produced
    by `filter_value`, so only the backslash rule is needed, applied twice.
    """

    def once(text: str) -> str:
        out, i = [], 0
        while i < len(text):
            if text[i] == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
            else:
                assert text[i] != "'", "unescaped quote"
                out.append(text[i])
                i += 1
        return "".join(out)

    return once(once(escaped))


HOSTILE = [
    "plain",
    "comma, separated",
    "colon: here",
    "back\\slash",
    "single ' quote",
    'double " quote',
    "100% legit %{pts}",
    "{braces} and [brackets]",
    "semi;colon",
    "emoji 😭🎥 ok",
    "trailing backslash\\",
    "\\N \\n \\h {\\an8}",
    "  padded  ",
    "all at once: it's 100%, [ok]; {yes} \\ 'no' =x",
    "'; drawbox=c=red [x]",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_filter_value_round_trips_through_both_ffmpeg_parsers(text: str) -> None:
    escaped = filter_value(text)
    assert unescape_filter_value(escaped) == text


@pytest.mark.parametrize("text", HOSTILE)
def test_filter_value_leaves_no_bare_graph_separators(text: str) -> None:
    escaped = filter_value(text)
    for i, ch in enumerate(escaped):
        if ch in ",;[]'":
            assert escaped[i - 1] == "\\", f"bare {ch!r} in {escaped!r}"
    assert not escaped.startswith(" ")
    assert not escaped.endswith(" ") or escaped.endswith("\ ")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a,b", "a\\,b"),
        ("a:b", "a\\\\:b"),
        ("a'b", "a\\\\\\'b"),
        ("a\\b", "a\\\\\\\\b"),
        ("a;b[c]", "a\\;b\\[c\\]"),
        ("between(t,1,2)", "between(t\\,1\\,2)"),
        ("50%", "50%"),
        ("", ""),
    ],
)
def test_filter_value_exact(value: str, expected: str) -> None:
    assert filter_value(value) == expected


@pytest.mark.parametrize("value", ["line\nbreak", "carriage\rreturn", "sep\u2028arator"])
def test_filter_value_rejects_line_breaks(value: str) -> None:
    with pytest.raises(ValueError, match="single-line"):
        filter_value(value)


@pytest.mark.parametrize(
    ("path", "normal"),
    [
        ("C:\\Users\\me\\My Clips\\a b.ass", "C:/Users/me/My Clips/a b.ass"),
        ("d:\\Personal Projects\\6Starts - GTA VI\\x.ass", "d:/Personal Projects/6Starts - GTA VI/x.ass"),
        ("C:/already/forward.ass", "C:/already/forward.ass"),
        ("\\\\server\\share\\clip.ass", "//server/share/clip.ass"),
        ("/data/render/abc/clip.ass", "/data/render/abc/clip.ass"),
        ("/data/it's, a [dir]; 100%/clip.ass", "/data/it's, a [dir]; 100%/clip.ass"),
        ("/data/odd\\name:x.ass", "/data/odd\\name:x.ass"),
        ("relative dir/clip.ass", "relative dir/clip.ass"),
    ],
)
def test_filter_path_windows_and_linux(path: str, normal: str) -> None:
    assert normalize_filter_path(path) == normal
    assert unescape_filter_value(filter_path(path)) == normal


def test_filter_path_escapes_the_drive_colon() -> None:
    assert filter_path("C:\\a b\\c.ass") == "C\\\\:/a b/c.ass"


def test_render_filter_escapes_every_value() -> None:
    rendered = render_filter("drawbox", [("x", "0"), ("color", "black@0.3"), ("enable", "between(t,1,2)")])
    assert rendered == "drawbox=x=0:color=black@0.3:enable=between(t\\,1\\,2)"
    assert render_filter("anull", []) == "anull"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("two\nlines", "two lines"),
        ("win\r\nbreak", "win break"),
        ("tab\there", "tab here"),
        ("bell\x07 and null\x00", "bell and null"),
        ("  many   spaces  ", "many spaces"),
    ],
)
def test_single_line(text: str, expected: str) -> None:
    assert single_line(text) == expected


def test_drawtext_text_flattens_then_escapes() -> None:
    assert unescape_filter_value(drawtext_text("50% off:\nit's [here], ok; \\o/ 😭")) == (
        "50% off: it's [here], ok; \\o/ 😭"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain words", "plain words"),
        ("{\\an8}top", f"\\{{\\{J}an8\\}}top"),
        ("a\\Nb", f"a\\{J}Nb"),
        ("a\\nb \\h", f"a\\{J}nb \\{J}h"),
        ("end\\", f"end\\{J}"),
        ("100%, ok: yes; 'q' \"d\"", "100%, ok: yes; 'q' \"d\""),
        ("cry 😭🎥", "cry 😭🎥"),
        ("two\nlines", "two lines"),
        ("win\r\nbreak\rhere", "win break here"),
        ("tab\tand\x00null", "tab andnull"),
        (f"joiner{J}inside", "joinerinside"),
        ("  spaced  ", "spaced"),
    ],
)
def test_ass_text(text: str, expected: str) -> None:
    assert ass_text(text) == expected


def test_ass_text_can_keep_line_breaks_as_hard_breaks() -> None:
    assert ass_text("one\n\n{two}\r\nthree", keep_newlines=True) == "one\\N\\{two\\}\\Nthree"


@pytest.mark.parametrize("text", HOSTILE)
def test_ass_text_never_opens_an_override_block_or_breaks_the_line(text: str) -> None:
    escaped = ass_text(text + "\n" + text)
    assert "\n" not in escaped and "\r" not in escaped
    for i, ch in enumerate(escaped):
        if ch in "{}":
            assert escaped[i - 1] == "\\"
        if ch == "\\":
            assert escaped[i + 1] in ("{", "}", J)


def test_ass_field_removes_commas_and_breaks() -> None:
    assert ass_field("Font, With\nComma") == "Font With Comma"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0:00:00.00"),
        (1.234, "0:00:01.23"),
        (59.999, "0:01:00.00"),
        (3725.5, "1:02:05.50"),
        (-1, "0:00:00.00"),
    ],
)
def test_ass_time(seconds: float, expected: str) -> None:
    assert ass_time(seconds) == expected


def test_colors() -> None:
    assert parse_hex_color("#FFD400") == (255, 212, 0)
    assert parse_hex_color("ffd400") == (255, 212, 0)
    assert ass_color("#FFD400") == "&H0000D4FF"
    assert ass_color("#FFD400", alpha=128) == "&H8000D4FF"
    assert ass_override_color("#FFD400") == "&H00D4FF&"
    assert ffmpeg_color("#FFD400") == "0xFFD400"
    assert ffmpeg_color("#FFD400", opacity=0.6) == "0xFFD400@0.6"


@pytest.mark.parametrize("value", ["", "#FFF", "yellow", "#GGGGGG", "#FFD40000", "0xFFD400"])
def test_bad_colors_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="colour"):
        parse_hex_color(value)
