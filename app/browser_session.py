import asyncio
import base64
import logging
import os

logger = logging.getLogger("flow.browser")

_WIDTH = 1280
_HEIGHT = 720


class BrowserSession:
    """Singleton headless Chromium session for server-side YouTube login."""

    WIDTH = _WIDTH
    HEIGHT = _HEIGHT

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @property
    def is_active(self) -> bool:
        return self._page is not None

    async def start(self, url: str = "https://accounts.google.com/ServiceLogin?service=youtube") -> str:
        if self.is_active:
            return await self.screenshot()

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("playwright is not installed — run: pip install playwright && playwright install chromium --with-deps")

        logger.info("Starting headless browser session")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": self.WIDTH, "height": self.HEIGHT},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        logger.info("Browser session started")
        return await self.screenshot()

    async def screenshot(self) -> str:
        if not self._page:
            raise RuntimeError("Browser not started")
        png = await self._page.screenshot(full_page=False)
        return base64.b64encode(png).decode()

    async def tap(self, x_frac: float, y_frac: float) -> str:
        if not self._page:
            raise RuntimeError("Browser not started")
        x = int(x_frac * self.WIDTH)
        y = int(y_frac * self.HEIGHT)
        await self._page.mouse.click(x, y)
        await asyncio.sleep(0.4)
        await self._wait_idle()
        return await self.screenshot()

    async def type_text(self, text: str) -> str:
        if not self._page:
            raise RuntimeError("Browser not started")
        await self._page.keyboard.type(text)
        await asyncio.sleep(0.2)
        return await self.screenshot()

    async def key_press(self, key: str) -> str:
        if not self._page:
            raise RuntimeError("Browser not started")
        await self._page.keyboard.press(key)
        await asyncio.sleep(0.4)
        await self._wait_idle()
        return await self.screenshot()

    async def save_cookies(self, cookie_path: str) -> int:
        if not self._context:
            raise RuntimeError("Browser not started")
        cookies = await self._context.cookies()

        dir_path = os.path.dirname(cookie_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        count = 0
        lines = ["# Netscape HTTP Cookie File\n\n"]
        for c in cookies:
            domain = c.get("domain", "")
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            expires = int(c.get("expires", 0)) if (c.get("expires") or -1) > 0 else 0
            name = c.get("name", "").replace("\n", "").replace("\r", "").replace("\t", "")
            value = c.get("value", "").replace("\n", "").replace("\r", "").replace("\t", "")
            if not name:
                continue
            lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
            count += 1

        with open(cookie_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        logger.info(f"Saved {count} browser cookies to {cookie_path}")
        await self.stop()
        return count

    async def stop(self):
        logger.info("Stopping browser session")
        for obj, method in [(self._browser, "close"), (self._playwright, "stop")]:
            if obj:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    async def _wait_idle(self):
        try:
            await self._page.wait_for_load_state("networkidle", timeout=3_000)
        except Exception:
            pass


browser_session = BrowserSession()
