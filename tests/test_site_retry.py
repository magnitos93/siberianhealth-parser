"""Smoke tests for the Playwright goto retry/timeout helper."""
from __future__ import annotations

from typing import Any

import pytest

from sibparser.site import PAGE_GOTO_TIMEOUT_MS, _goto_with_retry


class _FakePage:
    """Minimal duck-typed stand-in for ``playwright.sync_api.Page``.

    ``goto_results`` is a list of "outcomes" applied in order:
      * ``None`` — success
      * ``Exception`` instance — raised
    """

    def __init__(self, goto_results: list[Any]) -> None:
        self._results = list(goto_results)
        self.goto_calls: list[dict[str, Any]] = []
        self.waits: list[int] = []

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        outcome = self._results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


def test_goto_succeeds_on_first_try() -> None:
    from playwright.sync_api import TimeoutError as PWTimeout  # noqa: F401  - import for parity

    page = _FakePage([None])
    _goto_with_retry(page, "https://example.com")  # type: ignore[arg-type]
    assert len(page.goto_calls) == 1
    assert page.goto_calls[0]["timeout"] == PAGE_GOTO_TIMEOUT_MS
    assert page.goto_calls[0]["timeout"] == 90_000


def test_goto_retries_once_on_timeout() -> None:
    from playwright.sync_api import TimeoutError as PWTimeout

    page = _FakePage([PWTimeout("first try"), None])
    _goto_with_retry(page, "https://example.com")  # type: ignore[arg-type]
    # Two goto attempts, one wait between them.
    assert len(page.goto_calls) == 2
    assert page.waits == [2000]


def test_goto_gives_up_after_retries_exhausted() -> None:
    from playwright.sync_api import TimeoutError as PWTimeout

    page = _FakePage([PWTimeout("first"), PWTimeout("second")])
    with pytest.raises(PWTimeout):
        _goto_with_retry(page, "https://example.com")  # type: ignore[arg-type]
    assert len(page.goto_calls) == 2  # initial attempt + 1 retry
