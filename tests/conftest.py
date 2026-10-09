"""Presentation-only helpers for assertions against human CLI output."""

import pytest
from rich.text import Text


@pytest.fixture
def rendered_text():
    def normalize(output: str) -> str:
        # Decode terminal styling before removing Rich's wrapped panel edges.
        # Preserve words, punctuation and case; only layout whitespace changes.
        plain = Text.from_ansi(output).plain
        return " ".join(plain.replace("│", " ").split())

    return normalize
