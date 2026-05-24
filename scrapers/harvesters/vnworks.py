import logging
from typing import List
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from crawlee import Request
from crawlee.crawlers import PlaywrightCrawlingContext
from asgiref.sync import sync_to_async

from app_dashboard.models import Keyword
from scrapers.config.selector_loader import load_domain_selectors
from .base import BaseHarvester

logger = logging.getLogger(__name__)

class VNWorksHarvester(BaseHarvester):
    PAGE_ITEM_LIMIT = 50

    def __init__(self, max_requests_per_crawl: int = 100, domain_config=None):
        super().__init__(domain='vnworks', max_requests_per_crawl=max_requests_per_crawl, domain_config=domain_config)
        self.selectors = self._load_selectors()

    def _load_selectors(self) -> dict:
        return load_domain_selectors('vnworks').get('harvester', {})

    def _build_page_url(self, url: str, page_number: int) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query['page'] = str(page_number)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    async def _simulate_human_behavior(self, page) -> None:
        """VNWorks lazy-loads list items, so process_page performs domain-specific scrolling."""
        return

    async def _scroll_until_jobs_stable(
        self,
        page,
        item_selector: str,
        max_items: int = PAGE_ITEM_LIMIT,
        max_rounds: int = 20,
        stable_rounds: int = 3,
    ) -> int:
        try:
            await page.wait_for_selector(item_selector, timeout=10000)
        except Exception:
            logger.warning(f"Không tìm thấy job items nào sau khi đợi tại {page.url}")
            return 0

        last_count = -1
        last_height = -1
        stable_count = 0

        for round_index in range(1, max_rounds + 1):
            item_count = await page.locator(item_selector).count()
            scroll_height = await page.evaluate("document.body.scrollHeight")
            logger.info(
                "VNWorks lazy scroll round=%s item_count=%s scroll_height=%s",
                round_index,
                item_count,
                scroll_height,
            )

            if item_count >= max_items:
                logger.info("VNWorks reached page item limit=%s. Stop lazy scrolling.", max_items)
                return item_count

            if item_count == last_count and scroll_height == last_height:
                stable_count += 1
                if stable_count >= stable_rounds:
                    logger.info("VNWorks lazy list stabilized at item_count=%s. Stop lazy scrolling.", item_count)
                    return item_count
            else:
                stable_count = 0

            last_count = item_count
            last_height = scroll_height

            await page.evaluate(
                """
                () => {
                    window.scrollTo({
                        top: document.body.scrollHeight,
                        behavior: 'instant',
                    });
                }
                """
            )
            await page.wait_for_timeout(1200)

        final_count = await page.locator(item_selector).count()
        logger.info("VNWorks reached max lazy scroll rounds=%s with item_count=%s.", max_rounds, final_count)
        return final_count

    async def get_initial_requests(self, keyword: Keyword) -> List[str]:
        """
        Tạo URL tìm kiếm của VNWorks dựa trên keyword.
        VD: https://www.vietnamworks.com/viec-lam?q={keyword}
        """
        search_query = keyword.name.replace(' ', '+')
        url = f"https://www.vietnamworks.com/viec-lam?q={search_query}"
        return [url]

    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        """
        Xử lý trang danh sách công việc của VNWorks.
        """
        page = context.page
        request = context.request
        logger.info(f"Đang xử lý trang: {request.url}")

        # Lấy selectors
        item_sel = self.selectors.get('job_list_item', '')
        link_sel = self.selectors.get('job_link', '')
        next_sel = self.selectors.get('next_page', '')
        no_results_sel = self.selectors.get('no_results', '')

        if not item_sel or not link_sel:
            logger.error(f"Thiếu cấu hình selector job_list_item hoặc job_link cho {self.domain}.")
            return

        if await self.has_no_results(page, no_results_sel):
            logger.info(f"Không có kết quả VNWorks cho trang: {request.url}")
            return

        # VNWorks lazy-loads job cards while scrolling. Scroll until the page-size limit
        # or until the list stops growing, then extract links.
        item_count = await self._scroll_until_jobs_stable(page, item_sel)
        if item_count == 0:
            return

        # Trích xuất URL
        job_links = await page.locator(f"{item_sel} {link_sel}").evaluate_all(
            "elements => elements.map(e => e.href)"
        )
        
        # Lọc các URL đã tồn tại trong DB
        clean_links = [urljoin(request.url, link) for link in job_links]
        new_links = await self.filter_existing_links(clean_links)

        # Lưu vào DB
        keyword_name = request.user_data.get('keyword_name')
        total_saved_count = self.saved_count_from_request(request)
        if new_links and keyword_name:
            keyword = await sync_to_async(Keyword.objects.get)(name=keyword_name)
            saved_now = await self.save_job_links_for_page(new_links, keyword)
            total_saved_count += saved_now
            logger.info(
                "VNWorks saved %s new links on %s. Harvest saved_count=%s/%s.",
                saved_now,
                request.url,
                total_saved_count,
                self.max_jobs_per_keyword,
            )
        elif not new_links:
            logger.info(f"Tất cả link trên trang {request.url} đều đã tồn tại. Vẫn kiểm tra phân trang.")
        
        if self.is_job_limit_reached(request, total_saved_count):
            return

        # Xử lý Next Page
        if next_sel and self.should_enqueue_next_page(request):
            current_page = int(request.user_data.get('page_number', 1) or 1)
            next_page_number = current_page + 1
            next_button = page.locator(next_sel).filter(has_text=str(next_page_number)).first
            try:
                if await next_button.count() == 0:
                    logger.info(f"Không tìm thấy nút trang tiếp theo {next_page_number} tại {request.url}. Dừng phân trang.")
                    return
            except Exception as exc:
                logger.debug(f"Lỗi khi kiểm tra nút trang tiếp theo VNWorks: {exc}")
                return

            next_page_url = self._build_page_url(request.url, next_page_number)
            logger.info(f"Phát hiện trang tiếp theo VNWorks: {next_page_url}")
            await context.add_requests([
                Request.from_url(
                    url=next_page_url,
                    label='LIST_PAGE',
                    user_data=self.next_page_user_data(
                        request,
                        keyword_name=keyword_name,
                        saved_count=total_saved_count,
                    ),
                )
            ])

    async def harvest(self, keyword_name: str) -> None:
        """
        Override harvest để inject user_data chứa thông tin keyword vào Request.
        """
        logger.info(f"Bắt đầu Harvest cho từ khóa: {keyword_name} trên domain: {self.domain}")
        
        keyword, _ = await sync_to_async(Keyword.objects.get_or_create)(
            name=keyword_name,
            defaults={'is_active': True}
        )
        
        if not keyword.is_active:
            logger.warning(f"Từ khóa '{keyword_name}' đang bị vô hiệu hóa.")
            return

        start_urls = await self.get_initial_requests(keyword)
        if not start_urls:
            return

        # Tạo list Request objects
        requests = [
            Request.from_url(
                url=url, 
                user_data={'keyword_name': keyword_name, 'page_number': 1}, 
                label='LIST_PAGE'
            )
            for url in start_urls
        ]
        
        await self.crawler.run(requests)
        logger.info(f"Hoàn thành Harvest cho từ khóa: {keyword_name} trên domain: {self.domain}")
