import pytest

from assistant.voice.speechtext import clean_for_speech


@pytest.mark.parametrize("text,spoken", [
    ("It's ax² + bx + c = 0.", "It's ax squared plus bx plus c equals 0."),
    ("90 x 90 is 8,100.", "90 times 90 is 8,100."),
    ("**Bold** and `code`", "Bold and code"),
    ("See [the docs](https://example.com) or https://x.io/a", "See the docs or the link"),
    ("It's 21°C, 60% humidity — nice.", "It's 21 degrees Celsius, 60 percent humidity, nice."),
])
def test_clean_for_speech(text, spoken):
    assert clean_for_speech(text) == spoken
