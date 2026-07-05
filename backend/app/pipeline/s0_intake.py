"""S0 Intake (LLD §5-S0). v1 text-only: trim + collapse whitespace.
image/audio/url intake is deferred (do not build yet — LLD §5-S0)."""
import re

_WS = re.compile(r"\s+")


def intake(raw_text: str) -> str:
    return _WS.sub(" ", raw_text).strip()
