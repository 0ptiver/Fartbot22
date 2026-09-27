import pytest

from assistant.voice.textnorm import compact, normalize_words


@pytest.mark.parametrize("a,b", [
    ("90 x 90", "90 times 90"),
    ("ninety times ninety", "90 times 90"),
    ("8,100", "8100"),
    ("eight thousand one hundred", "8100"),
    ("two hundred and five", "205"),
    ("ax² + bx", "ax squared plus bx"),
])
def test_equivalent(a, b):
    assert normalize_words(a) == normalize_words(b)


def test_compact():
    assert compact("90 x 90 is 8,100.") == "90times90is8100"
