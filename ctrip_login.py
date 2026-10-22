"""Official Ctrip QR login in an isolated, short-lived server browser."""

from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .ctrip import _BROWSER_LOCK, browser_options, write_state
from .fetcher import run_blocking

LOGIN_URL = "https://passport.ctrip.com/user/login?BackUrl=https%3A%2F%2Fwww.ctrip.com%2F"
LOGIN_TIMEOUT_MS = 120_000


def _commit_state(state, path):
    # A query finishing concurrently must not overwrite the newly confirmed account.
    with _BROWSER_LOCK:
        write_state(state, path)


class CtripQRLogin:
    def __init__(self, context):
        self.context = context

    async def start(self) -> bytes:
        self.page = await self.context.new_page()
        self.page.set_default_timeout(45_000)
        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await self.page.get_by_role("link", name="扫码登录", exact=True).click()
        qr = self.page.get_by_test_id("qrCodeBox").locator("canvas")
        await qr.wait_for(state="visible")
        return await qr.screenshot(type="png", scale="css")

    async def wait_and_save(self, path: Path):
        # Ctrip's official login page redirects to BackUrl after confirmation.
        # Merely displaying/scanning the QR code never exports a session.
        await self.page.wait_for_url("https://www.ctrip.com/", timeout=LOGIN_TIMEOUT_MS)
        state = await self.context.storage_state(indexed_db=False)
        await run_blocking(_commit_state, state, path)


@asynccontextmanager
async def qr_login():
    from playwright.async_api import async_playwright

    with TemporaryDirectory(prefix="jipiao-ctrip-login-") as profile:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=profile, **browser_options(mobile=False)
            )
            try:
                await context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
                yield CtripQRLogin(context)
            finally:
                await context.close()
