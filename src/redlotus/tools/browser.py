"""Tools browser responsibilities."""

from __future__ import annotations

import asyncio
import os
import sys
from functools import wraps

from redlotus.runtime.config import get_env
from redlotus.tools.registry import resolve_readable_path


def page_action(operation):
    """Serialize page actions and report browser failures to the Agent."""

    @wraps(operation)
    async def run(self, *args, **kwargs):
        async with self._lock:
            try:
                await self._start()
                return await operation(self, *args, **kwargs)
            except (ImportError, RuntimeError) as exc:
                return f"Error: Browser unavailable: {exc}"
            except self._browser_error as exc:
                return f"Error: {operation.__name__}: {exc}"

    return run


class PlaywrightBrowserSession:
    """A lazy browser owned and closed by the Agent's event loop."""

    def __init__(self, workspace):
        self.workspace = workspace
        self._lock = asyncio.Lock()
        self._playwright = self._browser = self._page = None
        self._browser_error = ()

    async def _start(self):
        if self._page is not None:
            return
        from playwright.async_api import Error, async_playwright

        if getattr(sys, "frozen", False):
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "")
        self._browser_error = Error
        self._playwright = await async_playwright().start()
        try:
            headless = (get_env("BROWSER_HEADLESS", warn=False) or "").lower() not in (
                "0",
                "false",
                "no",
            )
            self._browser = await self._playwright.chromium.launch(headless=headless)
            self._page = await self._browser.new_page(
                viewport={"width": 1280, "height": 720}, locale="zh-CN"
            )
            self._page.set_default_timeout(30_000)
        except BaseException:
            await self._close()
            raise

    async def _close(self):
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._playwright is not None:
                await self._playwright.stop()
            self._playwright = self._browser = self._page = None

    async def close(self):
        async with self._lock:
            await self._close()

    @page_action
    async def browser_navigate(
        self, url: str, wait_until: str = "domcontentloaded"
    ) -> str:
        """Open a URL in this Agent's browser page.

        Args:
            url: The full URL to open.
            wait_until: The Playwright navigation event to wait for.

        Returns:
            The resulting page URL and title, or a browser error."""
        await self._page.goto(url, wait_until=wait_until, timeout=60_000)
        return f"OK\nURL: {self._page.url}\nTitle: {await self._page.title()}"

    @page_action
    async def browser_get_content(self) -> str:
        """Read the current page's URL and full visible body text."""
        text = await self._page.locator("body").inner_text()
        return f"URL: {self._page.url}\n{text}"

    @page_action
    async def browser_screenshot(self, name: str, full_page: bool = False) -> str:
        """Save a screenshot of this Agent's current browser page.

        Args:
            name: Destination path within the allowed project paths.
            full_page: Capture the entire page when true, otherwise the visible viewport.

        Returns:
            The saved screenshot path, or a browser error."""
        path = resolve_readable_path(name, work_base=self.workspace.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=str(path), full_page=full_page)
        return f"Screenshot saved: {path}"

    @page_action
    async def browser_click(self, selector: str) -> str:
        """Click a matching element in the current page.

        Args:
            selector: A Playwright selector for the intended element.

        Returns:
            The clicked selector, or a browser error."""
        await self._page.click(selector)
        return f"Clicked: {selector}"

    @page_action
    async def browser_fill(self, selector: str, text: str) -> str:
        """Replace the value of a matching input in the current page.

        Args:
            selector: A Playwright selector for the input element.
            text: The value to fill.

        Returns:
            The filled selector, or a browser error."""
        await self._page.fill(selector, text)
        return f"Filled: {selector}"

    @page_action
    async def browser_press_key(self, key: str) -> str:
        """Press a key or shortcut in the current browser page.

        Args:
            key: The Playwright key name or shortcut, such as Enter or Control+A.

        Returns:
            The pressed key, or a browser error."""
        await self._page.keyboard.press(key)
        return f"Pressed: {key}"

    @page_action
    async def browser_wait_for_selector(
        self, selector: str, timeout_ms: int = 30_000
    ) -> str:
        """Wait for a matching element to become visible in the current page.

        Args:
            selector: A Playwright selector for the intended element.
            timeout_ms: Maximum wait time in milliseconds.

        Returns:
            The visible selector, or a browser error."""
        await self._page.wait_for_selector(selector, timeout=timeout_ms)
        return f"Visible: {selector}"

    @page_action
    async def browser_evaluate(self, javascript_expression: str) -> str:
        """Evaluate JavaScript within this Agent's browser page.

        Args:
            javascript_expression: JavaScript to evaluate in the current page context.

        Returns:
            The evaluation result, or a browser error."""
        return repr(await self._page.evaluate(javascript_expression))

    async def browser_close(self) -> str:
        """Close this Agent's browser page and release its browser resources."""
        await self.close()
        return "Browser closed"
