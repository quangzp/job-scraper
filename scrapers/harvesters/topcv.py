import logging
from typing import List
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from crawlee.crawlers import PlaywrightCrawlingContext
from asgiref.sync import sync_to_async

from scrapers.config.selector_loader import load_domain_selectors
from scrapers.utils.text_cleaner import remove_query_and_fragment

from app_dashboard.models import Keyword
from .base import BaseHarvester

logger = logging.getLogger(__name__)

class TopCVHarvester(BaseHarvester):
    def __init__(self, max_requests_per_crawl: int = 100, domain_config=None):
        super().__init__(domain='topcv', max_requests_per_crawl=max_requests_per_crawl, domain_config=domain_config)
        self.selectors = self._load_selectors()

    def _load_selectors(self) -> dict:
        return load_domain_selectors('topcv').get('harvester', {})

    async def get_initial_requests(self, keyword: Keyword) -> List[str]:
        """
        Tạo URL tìm kiếm của TopCV dựa trên keyword.
        """
        search_query = keyword.name.replace(' ', '-')
        url = f"https://www.topcv.vn/tim-viec-lam-{search_query}"
        return [url]

    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        """
        Xử lý trang danh sách công việc của TopCV.
        """
        page = context.page
        request = context.request
        logger.info(f"Đang xử lý trang: {request.url}")
        # await asyncio.sleep(100000000000)
        # Lấy selectors
        item_sel = self.selectors.get('job_list_item', '')
        link_sel = self.selectors.get('job_link', '')
        next_sel = self.selectors.get('next_page', '')
        no_results_sel = self.selectors.get('no_results', '')

        if not item_sel or not link_sel:
            logger.error(f"Thiếu cấu hình selector job_list_item hoặc job_link cho {self.domain}.")
            return
        # print(f"next_sel: {next_sel}")

        if await self.has_no_results(page, no_results_sel):
            logger.info(f"Không có kết quả TopCV cho trang: {request.url}")
            return

        # Đợi các item xuất hiện
        try:
            await page.wait_for_selector(item_sel, timeout=5000)
        except Exception:
            logger.warning(f"Không tìm thấy job items nào tại {request.url}")
            return

        # Trích xuất URL
        job_links = await page.locator(f"{item_sel} {link_sel}").evaluate_all(
            "elements => elements.map(e => e.href)"
        )
        
        # Làm sạch và lọc các URL đã tồn tại trong DB
        clean_links = [remove_query_and_fragment(urljoin(request.url, link)) for link in job_links]
        new_links = await self.filter_existing_links(clean_links)

        # Lưu vào DB
        keyword_name = request.user_data.get('keyword_name')
        total_saved_count = self.saved_count_from_request(request)
        if new_links and keyword_name:
            keyword = await sync_to_async(Keyword.objects.get)(name=keyword_name)
            saved_now = await self.save_job_links_for_page(new_links, keyword)
            total_saved_count += saved_now
            logger.info(
                "TopCV saved %s new links on %s. Harvest saved_count=%s/%s.",
                saved_now,
                request.url,
                total_saved_count,
                self.max_jobs_per_keyword,
            )
        elif not new_links:
            logger.info(f"Tất cả link trên trang {request.url} đều đã tồn tại. Vẫn kiểm tra phân trang.")

        if self.is_job_limit_reached(request, total_saved_count):
            return
        
        # Xử lý Next Page: Vì TopCV dùng data-href thay vì href, ta phải lấy thủ công
        if next_sel and self.should_enqueue_next_page(request):
            # Lấy HTML content và parse bằng BeautifulSoup
            content = await page.content()
            soup = BeautifulSoup(content, 'lxml')
            next_page_element = soup.select_one(next_sel)
            if next_page_element:
                next_href = next_page_element.get('data-href')
                print(f"next_href (bs4): {next_href}")
                
                if next_href:
                    abs_next_link = urljoin(request.url, next_href)
                    logger.info(f"Phát hiện trang tiếp theo: {abs_next_link}")
                    await context.add_requests([
                        self.build_harvest_request(
                            url=abs_next_link,
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

        # Tạo list Request objects chứa thông tin user_data
        requests = [
            self.build_harvest_request(
                url=url, 
                keyword_name=keyword_name,
                user_data={'page_number': 1}, 
                label='LIST_PAGE',
            )
            for url in start_urls
        ]
        
        await self.crawler.run(requests)
        logger.info(f"Hoàn thành Harvest cho từ khóa: {keyword_name} trên domain: {self.domain}")
