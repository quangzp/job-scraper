import abc
import asyncio
import hashlib
import logging
import os
import random
from datetime import timedelta
from typing import Any, List

from asgiref.sync import sync_to_async
from crawlee import Request
from crawlee.configuration import Configuration
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.events import LocalEventManager
from crawlee.fingerprint_suite import DefaultFingerprintGenerator, HeaderGeneratorOptions
from django.utils import timezone

# Classes in scrapers/ are called through run_worker.py after django.setup().
from app_dashboard.models import JobLink, Keyword, TargetDomain
from scrapers.utils.browser_pool import build_browser_pool, get_browser_backend
from scrapers.utils.proxy import load_proxy_configuration, mask_proxy_url

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning('Invalid integer value for %s=%r. Using default=%s.', name, value, default)
        return default


class BaseHarvester(abc.ABC):
    """Base class for harvesters that collect job detail URLs."""

    def __init__(self, domain: str, max_requests_per_crawl: int = 100, domain_config: TargetDomain | None = None):
        self.domain = domain
        self.domain_config = domain_config or self._load_domain_config_safely()
        self.max_requests_per_crawl = max_requests_per_crawl
        self.max_pages_per_keyword = self._get_domain_int('max_pages_per_keyword', 5)
        self.max_jobs_per_keyword = self._get_domain_int('max_jobs_per_keyword', 100)
        self.request_delay_min_seconds = self._get_domain_int('request_delay_min_seconds', 1)
        self.request_delay_max_seconds = max(
            self.request_delay_min_seconds,
            self._get_domain_int('request_delay_max_seconds', 3),
        )
        self.proxy_configuration = load_proxy_configuration(domain=self.domain, component='Harvester')
        self.crawlee_storage_dir = os.getenv('CRAWLEE_STORAGE_DIR') or './storage/harvest'
        self.crawlee_configuration = Configuration(storage_dir=self.crawlee_storage_dir)
        self.crawlee_event_manager = LocalEventManager().from_config(config=self.crawlee_configuration)
        self.request_handler_timeout_seconds = self._get_request_handler_timeout_seconds()
        self.max_request_retries = self._get_max_request_retries()
        logger.info(
            'Harvester crawler config domain=%s request_handler_timeout=%ss max_request_retries=%s',
            self.domain,
            self.request_handler_timeout_seconds,
            self.max_request_retries,
        )
        self.crawler = self._setup_crawler()

    def _load_domain_config_safely(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return TargetDomain.objects.filter(name=self.domain).first()
        logger.warning('Domain config was not provided for %s inside an async loop. Using defaults.', self.domain)
        return None

    def _get_domain_int(self, field_name: str, default: int) -> int:
        if not self.domain_config:
            return default
        value = getattr(self.domain_config, field_name, default)
        return default if value is None else int(value)

    def _get_browser_new_context_options(self):
        return None

    def _use_single_persistent_session(self) -> bool:
        return False

    def _get_request_handler_timeout_seconds(self) -> int:
        domain_env_name = f'HARVEST_{self.domain.upper()}_REQUEST_HANDLER_TIMEOUT_SECONDS'
        return _env_int(domain_env_name, _env_int('HARVEST_REQUEST_HANDLER_TIMEOUT_SECONDS', 5 * 60))

    def _get_max_request_retries(self) -> int:
        domain_env_name = f'HARVEST_{self.domain.upper()}_MAX_REQUEST_RETRIES'
        return _env_int(domain_env_name, _env_int('HARVEST_MAX_REQUEST_RETRIES', 3))

    def _get_single_session_crawler_options(self) -> dict[str, Any]:
        if not self._use_single_persistent_session():
            return {}

        from crawlee import ConcurrencySettings
        from crawlee.sessions import SessionPool

        max_tasks_per_minute = _env_int('CRAWLEE_HARVEST_MAX_TASKS_PER_MINUTE', 30)
        return {
            'max_session_rotations': 0,
            'concurrency_settings': ConcurrencySettings(max_tasks_per_minute=max_tasks_per_minute),
            'session_pool': SessionPool(
                max_pool_size=1,
                create_session_settings={
                    'max_usage_count': 999_999,
                    'max_age': timedelta(hours=999_999),
                    'max_error_score': 100,
                },
            ),
        }

    def _setup_crawler(self) -> PlaywrightCrawler:
        browser_new_context_options = self._get_browser_new_context_options()
        session_crawler_options = self._get_single_session_crawler_options()
        browser_pool = build_browser_pool(
            headless=True,
            browser_new_context_options=browser_new_context_options,
        )
        logger.info('Using harvester Crawlee storage domain=%s dir=%s', self.domain, self.crawlee_storage_dir)
        if browser_pool:
            logger.info('Using browser backend: %s', get_browser_backend())
            crawler_options = {
                'max_requests_per_crawl': self.max_requests_per_crawl,
                'max_request_retries': self.max_request_retries,
                'request_handler_timeout': timedelta(seconds=self.request_handler_timeout_seconds),
                'browser_pool': browser_pool,
                'proxy_configuration': self.proxy_configuration,
                'configuration': self.crawlee_configuration,
                'event_manager': self.crawlee_event_manager,
                **session_crawler_options,
            }
            crawler = PlaywrightCrawler(**crawler_options)
        else:
            logger.info('Using browser backend: playwright')
            fingerprint_generator = DefaultFingerprintGenerator(
                header_options=HeaderGeneratorOptions(
                    browsers=['chrome', 'edge', 'safari'],
                    operating_systems=['windows', 'macos', 'linux'],
                    devices=['desktop'],
                )
            )

            crawler_options = {
                'max_requests_per_crawl': self.max_requests_per_crawl,
                'max_request_retries': self.max_request_retries,
                'request_handler_timeout': timedelta(seconds=self.request_handler_timeout_seconds),
                'headless': True,
                'fingerprint_generator': fingerprint_generator,
                'proxy_configuration': self.proxy_configuration,
                'configuration': self.crawlee_configuration,
                'event_manager': self.crawlee_event_manager,
                **session_crawler_options,
            }
            if browser_new_context_options:
                crawler_options['browser_new_context_options'] = browser_new_context_options
            crawler = PlaywrightCrawler(**crawler_options)

        @crawler.router.default_handler
        async def request_handler(context: PlaywrightCrawlingContext) -> None:
            proxy_url = mask_proxy_url(getattr(getattr(context, 'proxy_info', None), 'url', None))
            logger.info(
                'Harvester request proxy domain=%s request_url=%s proxy=%s',
                self.domain,
                context.request.url,
                proxy_url,
            )

            try:
                await context.page.wait_for_load_state('domcontentloaded', timeout=15000)
            except Exception:
                logger.warning(f"Timeout waiting for page load at {context.request.url}; continuing.")

            await asyncio.sleep(random.uniform(self.request_delay_min_seconds, self.request_delay_max_seconds))
            try:
                await self._simulate_human_behavior(context.page)
            except Exception as exc:
                logger.warning(
                    'Human-like scroll failed for %s; continuing to process page: %s',
                    context.request.url,
                    exc,
                )
            await self.process_page(context)

        @crawler.error_handler
        async def error_handler(context: PlaywrightCrawlingContext, error: Exception) -> None:
            request = context.request
            retry_count = getattr(request, 'retry_count', None)
            logger.error(
                'Harvester request will be retried after %s at %s retry_count=%s: %s',
                type(error).__name__,
                request.url,
                retry_count,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

        @crawler.failed_request_handler
        async def failed_request_handler(context: PlaywrightCrawlingContext, error: Exception) -> None:
            logger.error(
                'Harvester request failed permanently after retries: %s at %s: %s',
                type(error).__name__,
                context.request.url,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

        return crawler

    async def _simulate_human_behavior(self, page) -> None:
        """Simulate a short human-like scroll."""
        scroll_height = await page.evaluate("document.body.scrollHeight")
        logger.info(f"Starting scroll simulation. Page height: {scroll_height}px")

        if scroll_height < 500:
            logger.warning("Page is too short or content has not loaded; skipping scroll.")
            return

        await page.evaluate("""
            async () => {
                const scrollHeight = document.body.scrollHeight;
                const target = scrollHeight / 2;
                let currentPos = 0;

                while (currentPos < target) {
                    const step = Math.floor(Math.random() * 40) + 10;
                    currentPos += step;
                    window.scrollTo(0, currentPos);
                    await new Promise(resolve => setTimeout(resolve, Math.random() * 50 + 20));
                }
            }
        """)
        await asyncio.sleep(random.uniform(1, 2))

    def selector_values(self, selector) -> list[str]:
        if not selector:
            return []
        if isinstance(selector, list):
            return [item for item in selector if item]
        return [selector]

    async def has_no_results(self, page, selectors=None) -> bool:
        selectors = selectors if selectors is not None else getattr(self, 'selectors', {}).get('no_results', '')
        for selector in self.selector_values(selectors):
            try:
                if await page.locator(selector).count() > 0:
                    logger.info('No-results selector matched for domain=%s selector=%s', self.domain, selector)
                    return True
            except Exception as exc:
                logger.debug('No-results selector failed for domain=%s selector=%s: %s', self.domain, selector, exc)
        return False

    @abc.abstractmethod
    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        """Extract job URLs from a list page."""
        pass

    @abc.abstractmethod
    async def get_initial_requests(self, keyword: Keyword) -> List[str]:
        """Build initial list page URLs for a keyword."""
        pass

    def should_enqueue_next_page(self, request) -> bool:
        return not self.is_job_limit_reached(request)

    def saved_count_from_request(self, request) -> int:
        try:
            return int(request.user_data.get('saved_count', 0) or 0)
        except (TypeError, ValueError):
            return 0

    def is_job_limit_reached(self, request, saved_count: int | None = None) -> bool:
        saved_count = self.saved_count_from_request(request) if saved_count is None else saved_count
        if saved_count >= self.max_jobs_per_keyword:
            logger.info(
                'Reached max_jobs_per_keyword=%s for %s. Stop pagination.',
                self.max_jobs_per_keyword,
                request.url,
            )
            return True
        return False

    def next_page_user_data(self, request, **extra):
        user_data = dict(request.user_data or {})
        current_page = int(user_data.get('page_number', 1) or 1)
        user_data['page_number'] = current_page + 1
        user_data.update(extra)
        return user_data

    def _harvest_request_unique_key(
        self,
        url: str,
        keyword_name: str,
        page_number: int,
        unique_context: str = '',
    ) -> str:
        raw_key = f'{self.domain}|{keyword_name}|{page_number}|{unique_context}|{url}'
        digest = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:24]
        return f'harvest:{self.domain}:{digest}'

    def build_harvest_request(
        self,
        *,
        url: str,
        keyword_name: str,
        label: str = 'LIST_PAGE',
        user_data: dict[str, Any] | None = None,
        unique_context: str = '',
    ) -> Request:
        request_user_data = dict(user_data or {})
        request_user_data.setdefault('keyword_name', keyword_name)
        request_user_data.setdefault('page_number', 1)
        page_number = int(request_user_data.get('page_number', 1) or 1)

        return Request.from_url(
            url=url,
            label=label,
            user_data=request_user_data,
            unique_key=self._harvest_request_unique_key(
                url,
                keyword_name,
                page_number,
                unique_context,
            ),
        )

    @sync_to_async
    def filter_existing_links(self, urls: List[str]) -> List[str]:
        """Return URLs that are not already present in JobLink."""
        existing_urls = set(JobLink.objects.filter(url__in=urls).values_list('url', flat=True))
        return [url for url in urls if url not in existing_urls]

    @sync_to_async
    def save_job_link(self, url: str, keyword: Keyword) -> bool:
        """Persist a job URL as PENDING, protected by the unique URL constraint."""
        try:
            obj, created = JobLink.objects.get_or_create(
                url=url,
                defaults={
                    'keyword': keyword.name,
                    'domain': self.domain,
                    'status': 'PENDING',
                },
            )
            if created:
                logger.info(f"Saved new job link: {url}")
            else:
                logger.debug(f"Job link already exists: {url}")
            return created
        except Exception as e:
            logger.error(f"Error saving job link {url}: {e}")
            return False

    async def save_job_links_for_page(self, urls: List[str], keyword: Keyword) -> int:
        """Save all new job URLs discovered on the current page."""
        saved_count = 0
        for url in urls:
            if await self.save_job_link(url, keyword):
                saved_count += 1
        return saved_count

    async def harvest(self, keyword_name: str) -> None:
        """Run the harvester for one keyword."""
        started_at = timezone.localtime(timezone.now()).isoformat()
        logger.info(
            "Starting harvester domain=%s keyword=%s started_at=%s",
            self.domain,
            keyword_name,
            started_at,
        )

        keyword, _ = await sync_to_async(Keyword.objects.get_or_create)(
            name=keyword_name,
            defaults={'is_active': True},
        )

        if not keyword.is_active:
            logger.warning(f"Keyword {keyword_name!r} is inactive.")
            return

        start_urls = await self.get_initial_requests(keyword)
        if not start_urls:
            logger.warning(f"No start URLs generated for keyword: {keyword_name}")
            return

        requests = [
            self.build_harvest_request(url=url, keyword_name=keyword_name)
            for url in start_urls
        ]

        await self.crawler.run(requests)
        logger.info(f"Finished harvest for keyword={keyword_name} domain={self.domain}")
