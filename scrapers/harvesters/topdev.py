import logging
from typing import List
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

from asgiref.sync import sync_to_async
from crawlee.crawlers import PlaywrightCrawlingContext

from app_dashboard.models import Keyword
from scrapers.config.selector_loader import load_domain_selectors
from .base import BaseHarvester

logger = logging.getLogger(__name__)


class TopDevHarvester(BaseHarvester):
    def __init__(self, max_requests_per_crawl: int = 100, domain_config=None):
        super().__init__(domain='topdev', max_requests_per_crawl=max_requests_per_crawl, domain_config=domain_config)
        self.selectors = self._load_selectors()

    def _load_selectors(self) -> dict:
        return load_domain_selectors('topdev').get('harvester', {})

    def _build_page_url(self, url: str, page_number: int) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query['page'] = str(page_number)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    async def get_initial_requests(self, keyword: Keyword) -> List[str]:
        search_template = self.selectors.get('search_url_template', '')
        if not search_template:
            logger.error("Missing TopDev harvester search_url_template.")
            return []

        search_query = quote_plus(keyword.name)
        return [search_template.format(query=search_query, keyword=search_query)]

    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        page = context.page
        request = context.request
        logger.info(f"Processing TopDev list page: {request.url}")

        item_sel = self.selectors.get('job_list_item', '')
        link_sel = self.selectors.get('job_link', '')
        next_sel = self.selectors.get('next_page', '')
        no_results_sel = self.selectors.get('no_results', '')

        if not item_sel or not link_sel:
            logger.error("Missing TopDev harvester selectors: job_list_item or job_link.")
            return

        if await self.has_no_results(page, no_results_sel):
            logger.info(f"No TopDev results for page: {request.url}")
            return

        try:
            await page.wait_for_selector(item_sel, timeout=10000)
        except Exception:
            logger.warning(f"No TopDev job items found at {request.url}")
            return

        job_links = await page.locator(f"{item_sel} {link_sel}").evaluate_all(
            "elements => elements.map(e => e.href || e.getAttribute('href')).filter(Boolean)"
        )
        clean_links = [urljoin(request.url, link) for link in job_links]
        new_links = await self.filter_existing_links(clean_links)

        keyword_name = request.user_data.get('keyword_name')
        total_saved_count = self.saved_count_from_request(request)
        if new_links and keyword_name:
            keyword = await sync_to_async(Keyword.objects.get)(name=keyword_name)
            saved_now = await self.save_job_links_for_page(new_links, keyword)
            total_saved_count += saved_now
            logger.info(
                "TopDev saved %s new links on %s. Harvest saved_count=%s/%s.",
                saved_now,
                request.url,
                total_saved_count,
                self.max_jobs_per_keyword,
            )
        elif not new_links:
            logger.info(f"All TopDev links on {request.url} already exist. Checking pagination.")

        if self.is_job_limit_reached(request, total_saved_count):
            return

        current_page = int(request.user_data.get('page_number', 1) or 1)
        if current_page >= self.max_pages_per_keyword:
            logger.info("Reached TopDev max_pages_per_keyword=%s. Stop pagination.", self.max_pages_per_keyword)
            return

        if next_sel and self.should_enqueue_next_page(request):
            next_page_number = current_page + 1
            next_page_button = page.locator(next_sel).filter(has_text=str(next_page_number)).first
            try:
                if await next_page_button.count() == 0:
                    logger.info(f"No TopDev page {next_page_number} button found at {request.url}. Stop pagination.")
                    return
            except Exception as exc:
                logger.debug(f"Error checking TopDev next page {next_page_number} button: {exc}")
                return

            next_page_url = self._build_page_url(request.url, next_page_number)
            logger.info(f"Detected TopDev next page: {next_page_url}")
            await context.add_requests([
                self.build_harvest_request(
                    url=next_page_url,
                    label='LIST_PAGE',
                    keyword_name=keyword_name or '',
                    user_data=self.next_page_user_data(
                        request,
                        keyword_name=keyword_name,
                        saved_count=total_saved_count,
                    ),
                )
            ])

    async def harvest(self, keyword_name: str) -> None:
        logger.info(f"Starting TopDev harvest for keyword={keyword_name}")

        keyword, _ = await sync_to_async(Keyword.objects.get_or_create)(
            name=keyword_name,
            defaults={'is_active': True},
        )

        if not keyword.is_active:
            logger.warning(f"Keyword {keyword_name!r} is inactive.")
            return

        start_urls = await self.get_initial_requests(keyword)
        if not start_urls:
            logger.warning(f"No TopDev start URLs generated for keyword: {keyword_name}")
            return

        requests = [
            self.build_harvest_request(
                url=url,
                label='LIST_PAGE',
                keyword_name=keyword_name,
                user_data={'page_number': 1},
            )
            for url in start_urls
        ]

        await self.crawler.run(requests)
        logger.info(f"Finished TopDev harvest for keyword={keyword_name}")
