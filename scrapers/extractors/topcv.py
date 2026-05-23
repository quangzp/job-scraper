import json
import logging
import os
from typing import Dict, Any

from crawlee.crawlers import PlaywrightCrawlingContext
from .base import BaseExtractor

logger = logging.getLogger(__name__)

class TopCVExtractor(BaseExtractor):
    def __init__(self, max_requests_per_crawl: int = 50, domain_config=None):
        super().__init__(domain='topcv', max_requests_per_crawl=max_requests_per_crawl, domain_config=domain_config)
        self.selectors = self._load_selectors()

    def _load_selectors(self) -> dict:
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'selectors.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('topcv', {}).get('extractor', {})

    async def _extract_text(self, page, selector: str) -> str:
        """Helper để lấy text từ một selector, trả về rỗng nếu không có."""
        if not selector:
            return ""
        try:
            loc = page.locator(selector)
            if await loc.count() > 0:
                text = await loc.first.inner_text()
                return text.strip()
        except Exception as e:
            logger.debug(f"Lỗi khi lấy dữ liệu selector {selector}: {e}")
        return ""

    async def _extract_multi_text(self, page, selector: str, separator: str = ', ') -> str:
        """Helper để lấy text từ nhiều element của một selector, nối lại bằng separator."""
        if not selector:
            return ""
        try:
            loc = page.locator(selector)
            count = await loc.count()
            if count > 0:
                texts = []
                for i in range(count):
                    text = await loc.nth(i).inner_text()
                    if text.strip():
                        texts.append(text.strip())
                return separator.join(texts)
        except Exception as e:
            logger.debug(f"Lỗi khi lấy dữ liệu multi selector {selector}: {e}")
        return ""

    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        """
        Bóc tách chi tiết công việc từ trang Job Detail của TopCV.
        """
        page = context.page
        request = context.request
        link_id = request.user_data.get('link_id')
        
        if not link_id:
            logger.error("Không tìm thấy link_id trong request. Bỏ qua.")
            return
            
        logger.info(f"Đang trích xuất dữ liệu từ: {request.url}")

        try:
            # Đợi cho trang load tương đối (chờ tiêu đề xuất hiện)
            title_sel = self.selectors.get('title', '')
            if not title_sel:
                logger.error("Thiếu cấu hình selector cho title.")
                await self.save_job_detail(link_id, {}, is_success=False)
                return
                
            await page.wait_for_selector(title_sel, timeout=10000)
            
            data: Dict[str, Any] = {
                'title': await self._extract_text(page, title_sel),
                'company_name': await self._extract_text(page, self.selectors.get('company_name', '')),
                'company_url': await self._extract_text(page, self.selectors.get('company_url', '')),
                'contract_type': await self._extract_text(page, self.selectors.get('contract_type', '')),
                'deadline': await self._extract_text(page, self.selectors.get('deadline', '')),
                'description': await self._extract_text(page, self.selectors.get('description', '')),
                'experience_level': await self._extract_text(page, self.selectors.get('experience_level', '')),
                'location': await self._extract_text(page, self.selectors.get('location', '')),
                'posted_time': await self._extract_text(page, self.selectors.get('posted_time', '')),
                'salary': await self._extract_text(page, self.selectors.get('salary', '')),
                'sector': await self._extract_multi_text(page, self.selectors.get('sector', ''))
            }
            
            # Lưu thành công
            await self.save_job_detail(link_id, data, is_success=True)
            
        except Exception as e:
            logger.error(f"Lỗi khi trích xuất dữ liệu {request.url}: {e}")
            await self.save_job_detail(link_id, {}, is_success=False)
