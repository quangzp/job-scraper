import hashlib
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urljoin, urlsplit, urlunsplit

from asgiref.sync import sync_to_async
from crawlee.crawlers import PlaywrightCrawlingContext
from django.db import transaction

from app_dashboard.models import JobDetail, JobLink, Keyword
from scrapers.config.selector_loader import load_domain_selectors
from .base import BaseHarvester, _env_int

logger = logging.getLogger(__name__)


class LinkedInCheckpointError(RuntimeError):
    pass


class LinkedInSoftBlockError(RuntimeError):
    pass


SOFT_BLOCK_TEXT_PATTERNS = (
    'unusual activity',
    'temporarily restricted',
    'security verification',
    'verify your identity',
    'authwall',
    'checkpoint',
    'captcha',
    'account has been restricted',
    'your account is restricted',
    'we need to verify',
    # 'xác minh',
    # 'bảo mật',
    # 'hạn chế',
)


class LinkedInHarvester(BaseHarvester):
    login_url = 'https://www.linkedin.com/login/vi'
    feed_url = 'https://www.linkedin.com/feed/'
    jobs_search_url = 'https://www.linkedin.com/jobs/search/?origin=JOBS_HOME_JYMBII'

    def __init__(self, max_requests_per_crawl: int = 100, domain_config=None):
        self.email = os.getenv('LINKEDIN_EMAIL', '').strip()
        self.password = os.getenv('LINKEDIN_PASSWORD', '').strip()
        self.session_path = self._build_session_path()
        self.selectors, self.extractor_selectors = self._load_selector_config()
        self._saved_count_by_keyword: dict[str, int] = {}
        self._blocked_reason: str | None = None
        super().__init__(domain='linkedin', max_requests_per_crawl=max_requests_per_crawl, domain_config=domain_config)
        self.max_jobs_per_keyword = self._get_domain_int('max_jobs_per_keyword', 100)

    def _build_session_path(self) -> Path:
        session_dir = Path(os.getenv('LINKEDIN_SESSION_DIR', 'storage/sessions/linkedin'))
        account_key = self.email or 'default'
        account_hash = hashlib.sha256(account_key.encode('utf-8')).hexdigest()[:16]
        return session_dir / f'{account_hash}.json'

    def _get_browser_new_context_options(self):
        if self.session_path.exists():
            logger.info('Reusing LinkedIn session state: %s', self.session_path)
            return {'storage_state': str(self.session_path)}
        return None

    def _use_single_persistent_session(self) -> bool:
        return True

    def _get_request_handler_timeout_seconds(self) -> int:
        max_jobs = self._get_domain_int('max_jobs_per_keyword', 100)
        default_timeout = max(30 * 60, max_jobs * 90)
        return _env_int('HARVEST_LINKEDIN_REQUEST_HANDLER_TIMEOUT_SECONDS', default_timeout)

    def _get_max_request_retries(self) -> int:
        return _env_int('HARVEST_LINKEDIN_MAX_REQUEST_RETRIES', 0)

    def _load_selector_config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        linkedin_config = load_domain_selectors('linkedin')
        return linkedin_config.get('harvester', {}), linkedin_config.get('extractor', {})

    def _selector(self, name: str):
        value = self.selectors.get(name)
        if not value:
            raise RuntimeError(f'Missing LinkedIn selector config: {name}')
        return value

    def _selector_list(self, name: str) -> list[str]:
        value = self._selector(name)
        if isinstance(value, list):
            return [item for item in value if item]
        return [value]

    def _extractor_selector(self, name: str):
        value = self.extractor_selectors.get(name)
        if not value:
            raise RuntimeError(f'Missing LinkedIn extractor selector config: {name}')
        return value

    def _extractor_selector_list(self, name: str) -> list[str]:
        value = self._extractor_selector(name)
        if isinstance(value, list):
            return [item for item in value if item]
        return [value]

    def _search_locations(self) -> list[str]:
        if self.domain_config and self.domain_config.search_locations:
            return [str(location).strip() for location in self.domain_config.search_locations if str(location).strip()]
        return ['Vietnam']

    def _normalize_job_url(self, raw_url: str, base_url: str) -> str | None:
        if not raw_url:
            return None
        absolute_url = urljoin(base_url, raw_url)
        parts = urlsplit(absolute_url)
        if '/jobs/view/' not in parts.path:
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))

    def _job_id_from_url(self, url: str) -> str | None:
        parts = urlsplit(url)
        segments = [segment for segment in parts.path.split('/') if segment]
        try:
            view_index = segments.index('view')
            return segments[view_index + 1]
        except (ValueError, IndexError):
            return None

    def _template_selector_list(self, name: str, **values) -> list[str]:
        return [selector.format(**values) for selector in self._selector_list(name)]

    async def _has_selector(self, page, selector: str) -> bool:
        try:
            return await page.locator(selector).count() > 0
        except Exception:
            return False

    async def _has_visible_selector(self, page, selector: str) -> bool:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 5)):
                if await locator.nth(index).is_visible():
                    return True
        except Exception:
            return False
        return False

    async def _first_visible_locator(self, page, selector_name: str, timeout: int = 10000):
        last_error = None
        for selector in self._selector_list(selector_name):
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state='visible', timeout=timeout)
                return locator
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f'No visible LinkedIn selector found: {selector_name}') from last_error

    async def _first_visible_extractor_locator(self, page, selector_name: str, timeout: int = 10000):
        last_error = None
        for selector in self._extractor_selector_list(selector_name):
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state='visible', timeout=timeout)
                return locator
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f'No visible LinkedIn extractor selector found: {selector_name}') from last_error

    async def _first_existing_locator_from_selectors(self, page, selectors: list[str]):
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0:
                    return locator
            except Exception:
                continue
        return None

    async def _is_logged_in(self, page) -> bool:
        return await self._has_selector(page, self._selector('logged_in_marker'))

    async def _has_checkpoint(self, page) -> bool:
        return await self._has_selector(page, self._selector('checkpoint'))

    async def _has_selector_group(self, page, selector_name: str) -> bool:
        for selector in self._selector_list(selector_name):
            if await self._has_visible_selector(page, selector):
                return True
        return False

    async def _detect_soft_block(self, page, stage: str) -> str | None:
        selector_checks = (
            ('checkpoint', 'checkpoint/captcha/2FA selector'),
            ('soft_block', 'soft-block selector'),
            ('security_verification', 'security verification selector'),
            ('restricted_notice', 'restricted notice selector'),
        )
        if stage != 'before login':
            selector_checks = (*selector_checks, ('authwall', 'authwall selector'))
        for selector_name, reason in selector_checks:
            if await self._has_selector_group(page, selector_name):
                return f'{reason} at {stage}'

        page_bits = [page.url.lower()]
        try:
            page_bits.append((await page.title()).lower())
        except Exception:
            pass
        try:
            body_text = await page.locator('body').inner_text(timeout=3000)
            page_bits.append(body_text[:5000].lower())
        except Exception:
            pass

        joined_text = '\n'.join(page_bits)
        for pattern in SOFT_BLOCK_TEXT_PATTERNS:
            if stage == 'before login' and pattern == 'authwall':
                continue
            if pattern in joined_text:
                return f'text pattern {pattern!r} at {stage}'
        return None

    async def _raise_if_soft_blocked(self, page, stage: str) -> None:
        reason = await self._detect_soft_block(page, stage)
        if reason:
            raise LinkedInSoftBlockError(reason)

    async def _log_login_marker_diagnostics(self, page, error: Exception) -> None:
        marker_selector = self._selector('logged_in_marker')
        title = ''
        body_text = ''
        marker_count: int | str = 'unknown'
        visible_marker_count: int | str = 'unknown'
        soft_block_reason = None

        try:
            title = await page.title()
        except Exception as exc:
            title = f'<title unavailable: {exc}>'

        try:
            marker_locator = page.locator(marker_selector)
            marker_count = await marker_locator.count()
            visible_count = 0
            for index in range(min(int(marker_count), 10)):
                try:
                    if await marker_locator.nth(index).is_visible():
                        visible_count += 1
                except Exception:
                    continue
            visible_marker_count = visible_count
        except Exception as exc:
            marker_count = f'count failed: {exc}'
            visible_marker_count = 'unknown'

        try:
            soft_block_reason = await self._detect_soft_block(page, 'login marker wait failed')
        except Exception as exc:
            soft_block_reason = f'soft-block check failed: {exc}'

        try:
            body_text = await page.locator('body').inner_text(timeout=5000)
            body_text = ' '.join(body_text.split())[:2000]
        except Exception as exc:
            body_text = f'<body text unavailable: {exc}>'

        logger.error(
            'LinkedIn login marker diagnostic: url=%s title=%r marker_selector=%r '
            'marker_count=%s visible_marker_count=%s soft_block_reason=%r body_text=%r error=%s',
            page.url,
            title,
            marker_selector,
            marker_count,
            visible_marker_count,
            soft_block_reason,
            body_text,
            error,
        )

    def _retire_context_session(self, context: PlaywrightCrawlingContext, reason: str) -> None:
        session = getattr(context, 'session', None)
        if not session:
            logger.warning('LinkedIn soft block detected without Crawlee session: %s', reason)
            return

        try:
            session.mark_bad()
            session.retire()
            logger.warning('Retired LinkedIn Crawlee session after soft block: %s', reason)
        except Exception as exc:
            logger.warning('Could not retire LinkedIn Crawlee session after soft block %s: %s', reason, exc)

    async def _move_mouse_like_user(self, page, steps: int | None = None) -> None:
        viewport = page.viewport_size or {'width': 1366, 'height': 768}
        width = max(320, int(viewport.get('width') or 1366))
        height = max(240, int(viewport.get('height') or 768))
        total_steps = steps or random.randint(2, 5)

        for _ in range(total_steps):
            await page.mouse.move(
                random.randint(80, max(90, width - 120)),
                random.randint(90, max(100, height - 160)),
                steps=random.randint(6, 18),
            )
            await page.wait_for_timeout(random.randint(250, 900))

    async def _move_mouse_to_locator(self, locator) -> None:
        try:
            box = await locator.bounding_box(timeout=3000)
        except Exception:
            box = None
        if not box:
            return

        x = box['x'] + min(max(12, box['width'] * random.uniform(0.25, 0.75)), max(12, box['width'] - 12))
        y = box['y'] + min(max(12, box['height'] * random.uniform(0.25, 0.75)), max(12, box['height'] - 12))
        await locator.page.mouse.move(x, y, steps=random.randint(8, 20))
        await locator.page.wait_for_timeout(random.randint(150, 450))

    async def _scroll_locator_like_user(self, locator, *, direction: int = 1, rounds: int | None = None) -> None:
        await self._move_mouse_to_locator(locator)
        total_rounds = rounds or random.randint(2, 4)
        page = locator.page

        for _ in range(total_rounds):
            delta = random.randint(220, 620) * (1 if direction >= 0 else -1)
            await page.mouse.wheel(0, delta)
            await page.wait_for_timeout(random.randint(250, 700))

        if random.random() < 0.35:
            await page.mouse.wheel(0, -random.randint(80, 220) * (1 if direction >= 0 else -1))
            await page.wait_for_timeout(random.randint(200, 500))

    async def _scroll_results_to_job_card(self, page, card_locator, results_container, card_index: int) -> None:
        scroll_target = results_container or page.locator('body').first
        if card_index > 0:
            await self._scroll_locator_like_user(scroll_target, direction=1, rounds=1)

        viewport = page.viewport_size or {'height': 768}
        viewport_height = max(360, int(viewport.get('height') or 768))
        preferred_top = 150
        preferred_bottom = viewport_height - 160

        for _ in range(10):
            try:
                box = await card_locator.bounding_box(timeout=1500)
            except Exception:
                box = None

            if box and preferred_top <= box['y'] <= preferred_bottom:
                await self._move_mouse_to_locator(card_locator)
                return

            direction = 1
            if box and box['y'] < preferred_top:
                direction = -1
            await self._scroll_locator_like_user(scroll_target, direction=direction, rounds=1)

        await card_locator.scroll_into_view_if_needed(timeout=5000)
        await self._move_mouse_to_locator(card_locator)

    async def _scroll_results_to_top(self, page, results_container) -> None:
        scroll_target = results_container or page.locator('body').first
        await self._move_mouse_to_locator(scroll_target)
        for _ in range(4):
            await page.mouse.wheel(0, -900)
            await page.wait_for_timeout(random.randint(350, 800))

    async def _find_loaded_job_locators(self, page, url: str):
        job_id = self._job_id_from_url(url)
        if not job_id:
            return None, None

        link = await self._first_existing_locator_from_selectors(
            page,
            self._template_selector_list('job_link_by_id_template', job_id=job_id),
        )
        if not link:
            return None, None

        card = await self._first_existing_locator_from_selectors(
            page,
            self._template_selector_list('job_card_by_id_template', job_id=job_id),
        )
        return link, card or link

    async def _scroll_results_to_job_url(self, page, url: str, results_container, card_index: int):
        scroll_target = results_container or page.locator('body').first
        if card_index == 0:
            await self._scroll_results_to_top(page, scroll_target)
        elif card_index > 0:
            await self._scroll_locator_like_user(scroll_target, direction=1, rounds=1)

        viewport = page.viewport_size or {'height': 768}
        viewport_height = max(360, int(viewport.get('height') or 768))
        preferred_top = 150
        preferred_bottom = viewport_height - 160

        for _ in range(18):
            link, card = await self._find_loaded_job_locators(page, url)
            target = card or link
            box = None
            if target:
                try:
                    box = await target.bounding_box(timeout=1200)
                except Exception:
                    box = None
                if box and preferred_top <= box['y'] <= preferred_bottom:
                    await self._move_mouse_to_locator(target)
                    return link, target
                if box and box['y'] < preferred_top:
                    await self._scroll_locator_like_user(scroll_target, direction=-1, rounds=1)
                    continue

            await self._scroll_locator_like_user(scroll_target, direction=1, rounds=1)

        link, card = await self._find_loaded_job_locators(page, url)
        target = card or link
        if target:
            await target.scroll_into_view_if_needed(timeout=5000)
            await self._move_mouse_to_locator(target)
        return link, target

    async def _save_session_state(self, page) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        await page.context.storage_state(path=str(self.session_path))
        logger.info('Saved LinkedIn session state: %s', self.session_path)

    async def _ensure_logged_in(self, page) -> None:
        await self._raise_if_soft_blocked(page, 'before login')

        if await self._is_logged_in(page):
            return

        if not self.email or not self.password:
            raise RuntimeError('LINKEDIN_EMAIL and LINKEDIN_PASSWORD must be configured in .env.')

        logger.info('LinkedIn session is missing or expired. Logging in...')
        logger.info(
            'LinkedIn credentials loaded email=%s password_len=%s',
            self.email,
            self.password,
        )
        await page.goto(self.login_url, wait_until='domcontentloaded')
        await page.type(self._selector('login_email'), self.email, delay=100)
        await page.type(self._selector('login_password'), self.password, delay=100)
        await page.wait_for_timeout(1800)
        await page.click(self._selector('login_submit'), delay=100)
        await page.wait_for_load_state('domcontentloaded', timeout=30000)

        await self._raise_if_soft_blocked(page, 'after login submit')

        try:
            await page.wait_for_selector(self._selector('logged_in_marker'), timeout=30000)
        except Exception as exc:
            await self._log_login_marker_diagnostics(page, exc)
            raise RuntimeError('LinkedIn login did not reach an authenticated page.') from exc

        await self._save_session_state(page)

    async def get_initial_requests(self, keyword: Keyword) -> List[str]:
        return [self.feed_url]

    async def _browse_feed_passively(self, page) -> None:
        try:
            await page.wait_for_selector(self._selector('feed_marker'), timeout=15000)
        except Exception:
            logger.info('LinkedIn feed marker was not visible; continuing with Jobs navigation.')
        await self._raise_if_soft_blocked(page, 'feed loaded')

        rounds = random.randint(2, 4)
        for _ in range(rounds):
            await self._move_mouse_like_user(page, steps=random.randint(1, 3))
            await page.mouse.wheel(0, random.randint(280, 950))
            await page.wait_for_timeout(random.randint(900, 2400))
            if random.random() < 0.35:
                await page.mouse.wheel(0, -random.randint(80, 240))
                await page.wait_for_timeout(random.randint(500, 1300))
            await self._raise_if_soft_blocked(page, 'passive feed browse')

    async def _open_jobs_search(self, page, keyword_name: str, location: str) -> None:
        jobs_nav = await self._first_visible_locator(page, 'jobs_nav')
        await self._move_mouse_like_user(page, steps=random.randint(1, 2))
        await jobs_nav.click(delay=random.randint(80, 180))
        await page.wait_for_load_state('domcontentloaded', timeout=30000)
        await page.wait_for_timeout(random.randint(1500, 3000))
        await self._raise_if_soft_blocked(page, 'after jobs navigation')

        try:
            keyword_input = await self._first_visible_locator(page, 'jobs_keyword_input', timeout=5000)
        except Exception:
            logger.info('LinkedIn jobs home did not show search inputs. Opening jobs search entry.')
            search_entry = await self._first_visible_locator(page, 'jobs_search_entry', timeout=10000)
            await search_entry.scroll_into_view_if_needed(timeout=5000)
            await self._move_mouse_like_user(page, steps=random.randint(1, 2))
            await search_entry.click(delay=random.randint(80, 180))
            await page.wait_for_load_state('domcontentloaded', timeout=30000)
            await page.wait_for_timeout(random.randint(1500, 3000))
            await self._raise_if_soft_blocked(page, 'after jobs search entry')
            try:
                keyword_input = await self._first_visible_locator(page, 'jobs_keyword_input', timeout=8000)
            except Exception:
                logger.info('LinkedIn jobs search SPA route did not expose inputs. Reloading jobs search URL.')
                await page.goto(self.jobs_search_url, wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(random.randint(2500, 4500))
                await self._raise_if_soft_blocked(page, 'after jobs search reload')
                keyword_input = await self._first_visible_locator(page, 'jobs_keyword_input', timeout=10000)

        await self._move_mouse_like_user(page, steps=random.randint(1, 2))
        await keyword_input.click(delay=random.randint(80, 180))
        await keyword_input.press('Control+A')
        await keyword_input.type(keyword_name, delay=random.randint(70, 140))

        location_input = await self._first_visible_locator(page, 'jobs_location_input', timeout=10000)
        await self._move_mouse_like_user(page, steps=random.randint(1, 2))
        await location_input.click(delay=random.randint(80, 180))
        await location_input.press('Control+A')
        await location_input.type(location, delay=random.randint(70, 140))

        try:
            search_submit = await self._first_visible_locator(page, 'jobs_search_submit', timeout=3000)
            await self._move_mouse_like_user(page, steps=random.randint(1, 2))
            await search_submit.click(delay=random.randint(80, 180))
        except Exception:
            await location_input.press('Enter')

        await page.wait_for_load_state('domcontentloaded', timeout=30000)
        await page.wait_for_timeout(random.randint(2500, 4500))
        await self._raise_if_soft_blocked(page, 'after jobs search')

        if not await self._has_selector_group(page, 'date_posted_filter_button'):
            logger.info('LinkedIn search results route did not expose filters. Reloading current search URL.')
            await page.goto(page.url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(random.randint(3000, 5000))
            await self._raise_if_soft_blocked(page, 'after jobs search reload')

        try:
            await page.wait_for_selector(self._selector('jobs_results_marker'), timeout=20000)
        except Exception:
            logger.warning('LinkedIn jobs results marker was not visible after UI search.')

        await self._apply_recent_jobs_filter(page)

    async def _apply_recent_jobs_filter(self, page) -> None:
        logger.info('Applying LinkedIn recent jobs filter through the UI.')

        try:
            filter_button = await self._first_visible_locator(page, 'date_posted_filter_button', timeout=10000)
            await filter_button.scroll_into_view_if_needed(timeout=5000)
            await self._move_mouse_like_user(page, steps=random.randint(1, 2))
            await filter_button.click(delay=random.randint(80, 180))

            option = await self._first_visible_locator(page, 'date_posted_24h_option', timeout=10000)
            await option.scroll_into_view_if_needed(timeout=5000)
            await self._move_mouse_like_user(page, steps=random.randint(1, 2))
            await option.click(delay=random.randint(80, 180))
            await page.wait_for_timeout(random.randint(300, 800))

            apply_button = await self._first_visible_locator(page, 'date_posted_apply_button', timeout=10000)
            await self._move_mouse_like_user(page, steps=random.randint(1, 2))
            await apply_button.click(delay=random.randint(80, 180))
        except Exception as exc:
            raise RuntimeError('Could not apply LinkedIn 24-hour Date posted filter through the UI.') from exc

        try:
            await page.wait_for_load_state('domcontentloaded', timeout=15000)
        except Exception:
            logger.debug('LinkedIn did not trigger a full navigation after applying the Date posted filter.')

        await page.wait_for_timeout(random.randint(1500, 3000))
        await self._raise_if_soft_blocked(page, 'after recent jobs filter')

        try:
            await page.wait_for_selector(self._selector('jobs_results_marker'), timeout=20000)
        except Exception:
            logger.warning('LinkedIn jobs results marker was not visible after applying recent jobs filter.')

    async def _collect_current_job_cards(self, page) -> list[dict[str, Any]]:
        ordered_urls: list[str] = []
        seen_urls: set[str] = set()
        link_selector = ', '.join(self._selector_list('job_link'))
        results_container = await self._first_existing_locator_from_selectors(
            page,
            self._selector_list('jobs_results_container'),
        )
        scroll_target = results_container or page.locator('body').first

        async def collect_loaded_urls() -> int:
            added_count = 0

            for card_selector in self._selector_list('job_card'):
                cards = page.locator(card_selector)
                try:
                    count = await cards.count()
                except Exception as exc:
                    logger.debug('LinkedIn job-card selector failed: %s - %s', card_selector, exc)
                    continue

                for index in range(count):
                    card = cards.nth(index)
                    link = card.locator(link_selector).first
                    try:
                        raw_url = await link.get_attribute('href', timeout=2000)
                    except Exception:
                        continue

                    normalized_url = self._normalize_job_url(raw_url or '', page.url)
                    if not normalized_url or normalized_url in seen_urls:
                        continue

                    seen_urls.add(normalized_url)
                    ordered_urls.append(normalized_url)
                    added_count += 1

            for selector in self._selector_list('job_link'):
                links = page.locator(selector)
                try:
                    count = await links.count()
                except Exception as exc:
                    logger.debug('LinkedIn job-link selector failed: %s - %s', selector, exc)
                    continue

                for index in range(count):
                    link = links.nth(index)
                    try:
                        raw_url = await link.get_attribute('href', timeout=2000)
                    except Exception:
                        continue
                    normalized_url = self._normalize_job_url(raw_url or '', page.url)
                    if not normalized_url or normalized_url in seen_urls:
                        continue
                    seen_urls.add(normalized_url)
                    ordered_urls.append(normalized_url)
                    added_count += 1

            return added_count

        no_new_rounds = 0
        for round_index in range(12):
            added_count = await collect_loaded_urls()
            if added_count == 0:
                no_new_rounds += 1
            else:
                no_new_rounds = 0

            if no_new_rounds >= 3:
                break

            await self._scroll_locator_like_user(scroll_target, direction=1, rounds=1)
            await page.wait_for_timeout(random.randint(400, 900))

        await self._scroll_results_to_top(page, scroll_target)
        logger.info('Collected %s LinkedIn job URLs on current results page.', len(ordered_urls))

        return [{'url': url} for url in ordered_urls]

    async def _extract_first_text(self, page, selector_name: str) -> str:
        try:
            selectors = self._extractor_selector_list(selector_name)
        except RuntimeError:
            return ''

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                text = await locator.inner_text(timeout=3000)
                text = ' '.join(text.split())
                if text:
                    return text
            except Exception as exc:
                logger.debug('LinkedIn extractor text selector failed: %s - %s', selector, exc)
        return ''

    async def _extract_sector_text(self, page) -> str:
        try:
            selectors = self._extractor_selector_list('sector')
        except RuntimeError:
            return ''

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                text = await locator.evaluate(
                    """element => Array.from(element.childNodes)
                        .filter(node => node.nodeType === Node.TEXT_NODE)
                        .map(node => node.textContent || '')
                        .join(' ')""",
                    timeout=3000,
                )
                text = ' '.join(str(text or '').split())
                if text:
                    return text
            except Exception as exc:
                logger.debug('LinkedIn sector selector failed: %s - %s', selector, exc)
        return ''

    async def _extract_first_href(self, page, selector_name: str) -> str:
        try:
            selectors = self._extractor_selector_list(selector_name)
        except RuntimeError:
            return ''

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                href = await locator.get_attribute('href', timeout=3000)
                if href:
                    return urljoin(page.url, href)
            except Exception as exc:
                logger.debug('LinkedIn extractor href selector failed: %s - %s', selector, exc)
        return ''

    async def _wait_for_linkedin_detail_panel(self, page) -> None:
        await self._first_visible_extractor_locator(page, 'detail_ready', timeout=15000)

    async def _browse_linkedin_detail_panel(self, page, *, rounds: int = 1, allow_reverse: bool = False) -> None:
        try:
            selectors = self._extractor_selector_list('detail_scroll_container')
        except RuntimeError:
            selectors = []

        container = await self._first_existing_locator_from_selectors(page, selectors)
        if not container:
            container = page.locator('body').first

        await self._scroll_locator_like_user(container, direction=1, rounds=rounds)
        if allow_reverse and random.random() < 0.35:
            await self._scroll_locator_like_user(container, direction=-1, rounds=random.randint(1, 2))

    async def _select_some_detail_text(self, page) -> bool:
        try:
            description = await self._first_visible_extractor_locator(page, 'description', timeout=3000)
        except Exception:
            return False

        try:
            await description.scroll_into_view_if_needed(timeout=5000)
            await page.wait_for_timeout(random.randint(250, 700))
        except Exception:
            pass

        try:
            text_nodes = description.locator('p, li, span').filter(has_text='')
            count = await text_nodes.count()
        except Exception:
            count = 0

        candidate_indexes = list(range(min(count, 20)))
        random.shuffle(candidate_indexes)
        for index in candidate_indexes:
            node = text_nodes.nth(index)
            try:
                text = ' '.join((await node.inner_text(timeout=1000)).split())
                if len(text) < 20:
                    continue
                box = await node.bounding_box(timeout=1000)
                if not box or box['width'] < 80 or box['height'] < 12:
                    continue

                start_x = box['x'] + random.uniform(8, min(40, max(8, box['width'] * 0.25)))
                y = box['y'] + min(max(8, box['height'] * 0.55), box['height'] - 4)
                drag_width = min(box['width'] - 12, random.uniform(80, 220))
                end_x = start_x + max(40, drag_width)

                await page.mouse.move(start_x, y, steps=random.randint(8, 16))
                await page.wait_for_timeout(random.randint(150, 400))
                await page.mouse.down()
                await page.mouse.move(end_x, y + random.uniform(-3, 3), steps=random.randint(8, 18))
                await page.mouse.up()
                await page.wait_for_timeout(random.randint(300, 800))
                return True
            except Exception:
                continue

        try:
            box = await description.bounding_box(timeout=1000)
            if not box or box['width'] < 120 or box['height'] < 24:
                return False
            start_x = box['x'] + random.uniform(20, 60)
            y = box['y'] + min(max(18, box['height'] * 0.25), box['height'] - 8)
            end_x = min(box['x'] + box['width'] - 20, start_x + random.uniform(120, 260))
            await page.mouse.move(start_x, y, steps=random.randint(8, 16))
            await page.wait_for_timeout(random.randint(150, 400))
            await page.mouse.down()
            await page.mouse.move(end_x, y + random.uniform(-3, 3), steps=random.randint(8, 18))
            await page.mouse.up()
            await page.wait_for_timeout(random.randint(300, 800))
            return True
        except Exception:
            return False

    async def _expand_linkedin_description(self, page) -> bool:
        try:
            selectors = self._extractor_selector_list('description_show_more')
        except RuntimeError:
            return False

        for selector in selectors:
            try:
                button = page.locator(selector).first
                if await button.count() == 0 or not await button.is_visible():
                    continue
                await button.scroll_into_view_if_needed(timeout=3000)
                await self._move_mouse_like_user(page, steps=random.randint(1, 2))
                await button.click(delay=random.randint(80, 180))
                await page.wait_for_timeout(random.randint(300, 800))
                return True
            except Exception as exc:
                logger.debug('LinkedIn description expand selector failed: %s - %s', selector, exc)
        return False

    async def _extract_linkedin_job_detail(self, page) -> Dict[str, Any]:
        await self._wait_for_linkedin_detail_panel(page)
        expanded = await self._expand_linkedin_description(page)
        await self._browse_linkedin_detail_panel(page, rounds=2 if expanded else 1, allow_reverse=expanded)
        if random.random() < 0.5 and not await self._select_some_detail_text(page):
            logger.debug('LinkedIn detail description text selection was not possible on this page.')

        data: Dict[str, Any] = {
            'title': await self._extract_first_text(page, 'title'),
            'company_name': await self._extract_first_text(page, 'company_name'),
            'company_url': await self._extract_first_href(page, 'company_url'),
            'contract_type': await self._extract_first_text(page, 'contract_type'),
            'deadline': await self._extract_first_text(page, 'deadline'),
            'description': await self._extract_first_text(page, 'description'),
            'experience_level': await self._extract_first_text(page, 'experience_level'),
            'location': await self._extract_first_text(page, 'location'),
            'posted_time': await self._extract_first_text(page, 'posted_time'),
            'salary': await self._extract_first_text(page, 'salary'),
            'sector': await self._extract_sector_text(page),
        }

        if not data['title']:
            raise RuntimeError('LinkedIn detail panel did not expose a job title.')
        return data

    @sync_to_async
    def _get_completed_linkedin_urls(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()

        successful_urls = set(
            JobLink.objects.filter(
                url__in=urls,
                domain=self.domain,
                status='SUCCESS',
            ).values_list('url', flat=True)
        )
        if not successful_urls:
            return set()

        detailed_urls = set(
            JobDetail.objects.filter(job_url__in=successful_urls).values_list('job_url', flat=True)
        )
        return successful_urls & detailed_urls

    @sync_to_async
    def _save_linkedin_job_result(
        self,
        url: str,
        keyword_name: str,
        data: Dict[str, Any] | None,
        is_success: bool,
    ) -> None:
        with transaction.atomic():
            link, _ = JobLink.objects.get_or_create(
                url=url,
                defaults={
                    'keyword': keyword_name,
                    'domain': self.domain,
                    'status': 'PENDING',
                },
            )

            update_fields = []
            if link.keyword != keyword_name:
                link.keyword = keyword_name
                update_fields.append('keyword')
            if link.domain != self.domain:
                link.domain = self.domain
                update_fields.append('domain')

            if is_success and data:
                JobDetail.objects.update_or_create(
                    job_url=url,
                    defaults={
                        'title': data.get('title', ''),
                        'company_name': data.get('company_name', ''),
                        'company_url': data.get('company_url', ''),
                        'contract_type': data.get('contract_type', ''),
                        'deadline': data.get('deadline', ''),
                        'description': data.get('description', ''),
                        'experience_level': data.get('experience_level', ''),
                        'location': data.get('location', ''),
                        'posted_time': data.get('posted_time', ''),
                        'salary': data.get('salary', ''),
                        'sector': data.get('sector', ''),
                    },
                )
                if link.status != 'SUCCESS':
                    link.status = 'SUCCESS'
                    update_fields.append('status')
            elif link.status != 'SUCCESS' and link.status != 'PENDING':
                link.status = 'PENDING'
                update_fields.append('status')

            if update_fields:
                link.save(update_fields=[*set(update_fields), 'updated_at'])

    async def _process_current_job_cards(self, page, keyword_name: str) -> None:
        if await self._has_selector_group(page, 'no_results'):
            logger.info('LinkedIn no-results selector matched for keyword=%s at %s.', keyword_name, page.url)
            return

        job_cards = await self._collect_current_job_cards(page)
        if not job_cards:
            logger.info('No LinkedIn job cards found for keyword=%s at %s.', keyword_name, page.url)
            return

        completed_urls = await self._get_completed_linkedin_urls([job_card['url'] for job_card in job_cards])
        pending_job_cards = [job_card for job_card in job_cards if job_card['url'] not in completed_urls]
        logger.info(
            'LinkedIn current page jobs: collected=%s skipped_completed=%s pending_to_crawl=%s.',
            len(job_cards),
            len(completed_urls),
            len(pending_job_cards),
        )

        if not pending_job_cards:
            logger.info('All LinkedIn jobs on current page are already completed. Moving to next page if available.')
            return

        results_container = await self._first_existing_locator_from_selectors(
            page,
            self._selector_list('jobs_results_container'),
        )

        for card_index, job_card in enumerate(pending_job_cards):
            saved_count = self._saved_count_by_keyword.get(keyword_name, 0)
            if saved_count >= self.max_jobs_per_keyword:
                logger.info(
                    'Stopping current LinkedIn page early because max_jobs_per_keyword=%s was reached. '
                    'remaining_on_page=%s',
                    self.max_jobs_per_keyword,
                    len(pending_job_cards) - card_index,
                )
                break

            url = job_card['url']
            try:
                locator, card_locator = await self._scroll_results_to_job_url(page, url, results_container, card_index)
                if not locator:
                    raise RuntimeError('LinkedIn job card was not loaded while scrolling the results list.')
                if not await locator.is_visible():
                    await locator.scroll_into_view_if_needed(timeout=5000)
                await self._move_mouse_to_locator(locator)
                await locator.click(delay=random.randint(80, 180))
                await page.wait_for_timeout(random.randint(500, 1000))
                await self._raise_if_soft_blocked(page, 'after LinkedIn job-card click')
                data = await self._extract_linkedin_job_detail(page)
                await self._save_linkedin_job_result(url, keyword_name, data, is_success=True)
                logger.info('Saved LinkedIn JobDetail and marked SUCCESS: %s', url)
            except LinkedInSoftBlockError:
                raise
            except Exception as exc:
                logger.warning('Could not extract LinkedIn detail panel for %s: %s', url, exc)
                await self._save_linkedin_job_result(url, keyword_name, None, is_success=False)

            self._saved_count_by_keyword[keyword_name] = saved_count + 1
            await page.wait_for_timeout(random.randint(250, 800))

    async def _click_next_jobs_page(self, page) -> bool:
        for selector in self._selector_list('next_page'):
            locator = page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                if not await locator.is_enabled():
                    continue
                await locator.scroll_into_view_if_needed(timeout=5000)
                await self._move_mouse_like_user(page, steps=random.randint(1, 2))
                await locator.click(delay=random.randint(80, 180))
                await page.wait_for_load_state('domcontentloaded', timeout=30000)
                await page.wait_for_timeout(random.randint(1500, 3000))
                await self._raise_if_soft_blocked(page, 'after jobs pagination')
                return True
            except Exception as exc:
                logger.debug('LinkedIn next-page selector failed: %s - %s', selector, exc)
        return False

    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        page = context.page
        request = context.request
        keyword_name = request.user_data.get('keyword_name')
        location = request.user_data.get('location')

        try:
            await self._ensure_logged_in(page)
            await self._raise_if_soft_blocked(page, 'after login check')

            if not keyword_name:
                logger.error('Missing keyword_name in LinkedIn request user_data.')
                return

            await self._browse_feed_passively(page)
            await self._open_jobs_search(page, keyword_name, location or 'Vietnam')

            for page_number in range(1, self.max_pages_per_keyword + 1):
                if self._saved_count_by_keyword.get(keyword_name, 0) >= self.max_jobs_per_keyword:
                    logger.info('Reached max_jobs_per_keyword=%s for keyword=%s.', self.max_jobs_per_keyword, keyword_name)
                    break

                await self._raise_if_soft_blocked(page, 'before processing job cards')
                await self._process_current_job_cards(page, keyword_name)

                if self._saved_count_by_keyword.get(keyword_name, 0) >= self.max_jobs_per_keyword:
                    logger.info(
                        'Reached max_jobs_per_keyword=%s after processing current LinkedIn page. Stop pagination.',
                        self.max_jobs_per_keyword,
                    )
                    break

                if page_number >= self.max_pages_per_keyword:
                    logger.info('Reached max_pages_per_keyword=%s for %s. Stop pagination.', self.max_pages_per_keyword, page.url)
                    break
                if not await self._click_next_jobs_page(page):
                    logger.info('No LinkedIn next page button available at %s.', page.url)
                    break
        except LinkedInSoftBlockError as exc:
            reason = str(exc)
            logger.error('Stopping LinkedIn harvest because a soft block was detected: %s url=%s', reason, page.url)
            self._blocked_reason = reason
            self._retire_context_session(context, reason)
            return

    async def harvest(self, keyword_name: str) -> None:
        logger.info('Starting LinkedIn harvest for keyword=%s', keyword_name)

        if self._blocked_reason:
            logger.warning('Skipping LinkedIn harvest for keyword=%s because session is blocked: %s', keyword_name, self._blocked_reason)
            return

        if (not self.email or not self.password) and not self.session_path.exists():
            logger.error(
                'LinkedIn credentials are missing and no reusable session exists. '
                'Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD in .env.'
            )
            return

        keyword, _ = await sync_to_async(Keyword.objects.get_or_create)(
            name=keyword_name,
            defaults={'is_active': True},
        )
        if not keyword.is_active:
            logger.warning('Keyword %r is inactive.', keyword_name)
            return

        self._saved_count_by_keyword[keyword_name] = 0
        locations = self._search_locations()
        for location in locations:
            if self._blocked_reason:
                logger.warning('Stopping remaining LinkedIn locations for keyword=%s: %s', keyword_name, self._blocked_reason)
                break
            if self._saved_count_by_keyword.get(keyword_name, 0) >= self.max_jobs_per_keyword:
                break

            request = self.build_harvest_request(
                url=self.feed_url,
                label='FEED_TO_JOBS',
                keyword_name=keyword_name,
                user_data={
                    'location': location,
                    'page_number': 1,
                },
                unique_context=location,
            )
            await self.crawler.run([request])

        logger.info(
            'Finished LinkedIn harvest for keyword=%s. Processed %s job cards.',
            keyword_name,
            self._saved_count_by_keyword.get(keyword_name, 0),
        )
