"""Change history is visible on the API guide and in the revision note."""

from pathlib import Path

from openfactors.api.api_guide import load_guide, render_guide

NOTE = Path("docs/change-history.md")
REQUIRED = (
    "Schema `1.0.0` is the only public schema.",
    "No public field is deprecated.",
    "A breaking change is a new `dataset_version`.",
    "A change to values is a new `release_id`.",
    "That release is not available.",
    "A corrected source value is a new release.",
    "X-Release-Id",
    "The active release can move. The pin does not.",
    "This note does not list releases.",
)


def test_change_history_is_visible_and_matches_the_note() -> None:
    guide = load_guide()
    note = NOTE.read_text(encoding="utf-8")
    page = render_guide(guide)
    for sentence in REQUIRED:
        assert sentence in guide
        assert sentence in note
        assert sentence in page
    assert "<h2>Change history</h2>" in page
    assert "PH-PILOT" not in guide
    assert "PH-PILOT" not in note
