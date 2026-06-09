import abc
import asyncio
import logging
import os
import random
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

from asgiref.sync import sync_to_async
from crawlee import ConcurrencySettings, Request
from crawlee.configuration import Configuration
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.events import LocalEventManager
from crawlee.fingerprint_suite import DefaultFingerprintGenerator, HeaderGeneratorOptions
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from app_dashboard.models import JobDetail, JobLink, TargetDomain
from scrapers.utils.browser_pool import build_browser_pool, get_browser_backend
from scrapers.utils.proxy import load_proxy_configuration

logger = logging.getLogger(__name__)
logging.getLogger('crawlee.storage_clients').setLevel(logging.ERROR)
logging.getLogger('django.db.backends').setLevel(logging.WARNING)


class BaseExtractor(abc.ABC):
    """
    Base extractor for Phase 2.

    It uses Crawlee for crawling and Django ORM row locks for safe multi-worker
    state management.
    """

    def __init__(self, domain: str, max_requests_per_crawl: int = 50, domain_config: TargetDomain | None = None):
        self.domain = domain
        self.domain_config = domain_config or self._load_domain_config_safely()
        self.max_requests_per_crawl = max_requests_per_crawl
        self.job_read_time_seconds = self._get_domain_int('job_read_time_seconds', 3)
        self.request_delay_min_seconds = self._get_domain_int('request_delay_min_seconds', 1)
        self.request_delay_max_seconds = max(
            self.request_delay_min_seconds,
            self._get_domain_int('request_delay_max_seconds', 3),
        )
        self.failed_retry_cooldown_unit_seconds = int(os.getenv('FAILED_RETRY_COOLDOWN_UNIT_SECONDS', str(30 * 60)))
        self.max_session_rotations = int(os.getenv('CRAWLEE_MAX_SESSION_ROTATIONS', '0'))
        self.retry_on_blocked = os.getenv('CRAWLEE_RETRY_ON_BLOCKED', 'false').lower() == 'true'
        self.proxy_configuration = load_proxy_configuration(domain=self.domain)
        self.crawlee_storage_dir = self._get_domain_storage_dir()
        self.crawlee_configuration = Configuration(storage_dir=self.crawlee_storage_dir)
        self.crawlee_event_manager = LocalEventManager().from_config(config=self.crawlee_configuration)
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

    def _get_domain_storage_dir(self) -> str:
        base_storage_dir = os.getenv('CRAWLEE_STORAGE_DIR') or './storage/extract'
        return str(Path(base_storage_dir) / self.domain)

    def _setup_crawler(self) -> PlaywrightCrawler:
        concurrency_settings = ConcurrencySettings(
            desired_concurrency=3,
            min_concurrency=1,
            max_concurrency=5,
        )

        browser_pool = build_browser_pool(headless=True)
        crawler_options: dict[str, Any] = {
            'max_requests_per_crawl': self.max_requests_per_crawl,
            'concurrency_settings': concurrency_settings,
            'request_handler_timeout': timedelta(seconds=60),
            'max_session_rotations': self.max_session_rotations,
            'retry_on_blocked': self.retry_on_blocked,
            'proxy_configuration': self.proxy_configuration,
            'configuration': self.crawlee_configuration,
            'event_manager': self.crawlee_event_manager,
        }
        logger.info('Using extractor Crawlee storage domain=%s dir=%s', self.domain, self.crawlee_storage_dir)

        if browser_pool:
            logger.info('Using browser backend: %s', get_browser_backend())
            crawler_options['browser_pool'] = browser_pool
        else:
            logger.info('Using browser backend: playwright')
            fingerprint_generator = DefaultFingerprintGenerator(
                header_options=HeaderGeneratorOptions(
                    browsers=['chrome', 'edge'],
                    operating_systems=['windows', 'macos'],
                    devices=['desktop'],
                )
            )
            crawler_options.update(
                {
                    'headless': True,
                    'browser_type': 'chromium',
                    'fingerprint_generator': fingerprint_generator,
                }
            )

        crawler = PlaywrightCrawler(**crawler_options)

        @crawler.router.default_handler
        async def request_handler(context: PlaywrightCrawlingContext) -> None:
            try:
                try:
                    if await self.try_process_raw_html(context):
                        return
                except Exception as e:
                    logger.warning(
                        f"Raw HTML extraction failed for {context.request.url}; falling back to Playwright: {e}"
                    )

                try:
                    await context.page.wait_for_load_state('domcontentloaded', timeout=15000)
                except Exception:
                    logger.warning(f"Timeout waiting for page load at {context.request.url}; continuing.")

                await asyncio.sleep(random.uniform(self.request_delay_min_seconds, self.request_delay_max_seconds))
                await self._simulate_human_behavior(context.page)
                if self.job_read_time_seconds > 0:
                    min_read_seconds = 3
                    max_read_seconds = max(min_read_seconds, self.job_read_time_seconds)
                    await asyncio.sleep(random.uniform(min_read_seconds, max_read_seconds))
                await self.process_page(context)
            except Exception as e:
                logger.error(f"Unhandled exception while extracting {context.request.url}: {e}")
                link_id = context.request.user_data.get('link_id')
                if link_id:
                    await self.save_job_detail(link_id, {}, is_success=False)

        @crawler.failed_request_handler
        async def failed_request_handler(context: PlaywrightCrawlingContext, error: Exception) -> None:
            logger.error(f"Failed to fetch {context.request.url}: {type(error).__name__} - {error}")
            link_id = context.request.user_data.get('link_id')
            if link_id:
                await self.save_job_detail(link_id, {}, is_success=False)

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

    @abc.abstractmethod
    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        """Extractor-specific detail page parsing."""
        pass

    async def try_process_raw_html(self, context: PlaywrightCrawlingContext) -> bool:
        """Optionally process a page from its raw HTML before rendered fallback."""
        return False

    @sync_to_async
    def fetch_pending_links(self, batch_size: int = 10) -> List[JobLink]:
        """
        Lock eligible links with select_for_update(skip_locked=True) and move
        them to PROCESSING.
        """
        with transaction.atomic():
            now = timezone.now()
            failed_retry_filter = Q()
            for tried_count in range(3):
                cooldown_seconds = tried_count * self.failed_retry_cooldown_unit_seconds
                failed_retry_filter |= Q(
                    status='FAILED',
                    tried_count=tried_count,
                    updated_at__lte=now - timedelta(seconds=cooldown_seconds),
                )
            links = list(
                JobLink.objects.select_for_update(skip_locked=True)
                .filter(
                    Q(status='PENDING') | failed_retry_filter,
                    domain=self.domain,
                )[:batch_size]
            )

            if not links:
                return []

            link_ids = [link.id for link in links]
            JobLink.objects.filter(id__in=link_ids).update(
                status='PROCESSING',
                tried_count=F('tried_count') + 1,
            )

            return links

    @sync_to_async
    def save_job_detail(self, link_id: int, data: Dict[str, Any], is_success: bool = True) -> None:
        """Persist extracted details and update the JobLink status."""
        try:
            link = JobLink.objects.get(id=link_id)
            if is_success:
                JobDetail.objects.update_or_create(
                    job_url=link.url,
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
                link.status = 'SUCCESS'
            else:
                link.status = 'FAILED'

            link.save(update_fields=['status', 'updated_at'])
            status_text = 'SUCCESS' if is_success else 'FAILED'
            logger.info(f"Updated link {link.url} status to {status_text}")
        except Exception as e:
            logger.error(f"Error saving JobDetail for link_id={link_id}: {e}")

    async def extract(self, batch_size: int = 10) -> int:
        """Fetch one batch of links, crawl them, and exit."""
        from app_dashboard.models import TargetDomain

        started_at = timezone.localtime(timezone.now()).isoformat()
        logger.info(
            "Starting one-shot extractor domain=%s batch_size=%s started_at=%s",
            self.domain,
            batch_size,
            started_at,
        )

        domain_obj = await sync_to_async(TargetDomain.objects.filter(name=self.domain).first)()
        if not domain_obj or not domain_obj.is_active or not domain_obj.is_extract_enabled:
            logger.warning(f"Domain {self.domain} is inactive, extract-disabled, or deleted. Stopping extractor.")
            return 0

        links = await self.fetch_pending_links(batch_size=batch_size)
        if not links:
            logger.info(f"[{self.domain}] No eligible links to process.")
            return 0

        logger.info(f"Fetched {len(links)} links for extraction.")
        requests = [
            Request.from_url(
                url=link.url,
                user_data={'link_id': link.id},
            )
            for link in links
        ]

        crawler = self._setup_crawler()

        try:
            await crawler.run(requests)
        except Exception as e:
            logger.error(f"Error running crawler batch: {e}")

        link_ids = [link.id for link in links]

        @sync_to_async
        def cleanup_failed() -> None:
            failed_count = JobLink.objects.filter(id__in=link_ids, status='PROCESSING').update(status='FAILED')
            if failed_count > 0:
                logger.warning(f"Marked {failed_count} stuck PROCESSING links as FAILED.")

        await cleanup_failed()
        return len(links)
