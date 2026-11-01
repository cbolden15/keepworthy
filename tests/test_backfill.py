import pytest

from backfill import extract_id


@pytest.mark.parametrize("token,expected", [
    ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("  dQw4w9WgXcQ  ", "dQw4w9WgXcQ"),
    ("p5e_b9GXHbg", "p5e_b9GXHbg"),
])
def test_extract_id(token, expected):
    assert extract_id(token) == expected


def test_extract_id_rejects_garbage():
    with pytest.raises(ValueError):
        extract_id("not-a-url")
