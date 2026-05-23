import logging
import os
from datetime import timedelta
from typing import Any

from crawlee._utils.context import ensure_context
from crawlee.browsers import (
    BrowserPool,
    PlaywrightBrowserController,
    PlaywrightBrowserPlugin,
)
from typing_extensions import override

logger = logging.getLogger(__name__)


DEFAULT_BROWSER_BACKEND = 'playwright'
CLOAKBROWSER_BACKENDS = {'cloakbrowser', 'cloak'}


def get_browser_backend() -> str:
    return os.getenv('SCRAPER_BROWSER', DEFAULT_BROWSER_BACKEND).strip().lower()


def is_cloakbrowser_enabled() -> bool:
    return get_browser_backend() in CLOAKBROWSER_BACKENDS


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning('Invalid integer value for %s=%r. Using default=%s.', name, value, default)
        return default


class CloakBrowserPlugin(PlaywrightBrowserPlugin):
    """Crawlee browser plugin backed by CloakBrowser's patched Chromium binary."""

    @override
    @ensure_context
    async def new_browser(self) -> PlaywrightBrowserController:
        if not self._playwright:
            raise RuntimeError('Playwright browser plugin is not initialized.')

        try:
            from cloakbrowser.config import IGNORE_DEFAULT_ARGS, get_default_stealth_args
            from cloakbrowser.download import ensure_binary
        except ImportError as exc:
            raise RuntimeError(
                'SCRAPER_BROWSER=cloakbrowser requires the cloakbrowser package. '
                'Install project dependencies first.'
            ) from exc

        binary_path = ensure_binary()
        stealth_args = get_default_stealth_args()

        launch_options: dict[str, Any] = dict(self._browser_launch_options or {})
        launch_options.pop('executable_path', None)
        launch_options.pop('chromium_sandbox', None)
        existing_args = list(launch_options.pop('args', []) or [])
        launch_options['args'] = [*existing_args, *stealth_args]

        logger.info('Launching CloakBrowser patched Chromium from %s', binary_path)
        browser = await self._playwright.chromium.launch(
            executable_path=binary_path,
            ignore_default_args=IGNORE_DEFAULT_ARGS,
            **launch_options,
        )

        return PlaywrightBrowserController(
            browser=browser,
            use_incognito_pages=self._use_incognito_pages,
            max_open_pages_per_browser=_env_int('CLOAKBROWSER_MAX_OPEN_PAGES', 1),
            header_generator=None,
        )


def build_browser_pool(
    *,
    headless: bool = True,
    browser_new_context_options: dict[str, Any] | None = None,
) -> BrowserPool | None:
    """Return a custom BrowserPool when a non-default browser backend is enabled."""
    backend = get_browser_backend()
    if backend == DEFAULT_BROWSER_BACKEND:
        return None

    if backend not in CLOAKBROWSER_BACKENDS:
        raise RuntimeError(
            f"Unsupported SCRAPER_BROWSER={backend!r}. "
            "Supported values: 'playwright', 'cloakbrowser'."
        )

    use_incognito_pages = bool(browser_new_context_options)

    return BrowserPool(
        plugins=[
            CloakBrowserPlugin(
                browser_type='chromium',
                browser_launch_options={'headless': headless},
                browser_new_context_options=browser_new_context_options,
                use_incognito_pages=use_incognito_pages,
            )
        ],
        operation_timeout=timedelta(seconds=_env_int('CLOAKBROWSER_OPERATION_TIMEOUT_SECONDS', 90)),
    )
