import logging
from typing import Any, Dict
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from crawlee.crawlers import PlaywrightCrawlingContext
from curl_cffi import requests as curl_requests

from scrapers.config.selector_loader import load_domain_selectors
from .base import BaseExtractor

logger = logging.getLogger(__name__)


def _mask_proxy_url(proxy_url) -> str:
    if not proxy_url:
        return 'none'

    raw_url = str(proxy_url)
    try:
        parts = urlsplit(raw_url)
    except Exception:
        return '<configured>'

    if not parts.netloc:
        return raw_url

    host = parts.hostname or ''
    port = f':{parts.port}' if parts.port else ''
    if parts.username or parts.password:
        netloc = f'***:***@{host}{port}'
    else:
        netloc = parts.netloc

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class TopCVExtractor(BaseExtractor):
    def __init__(self, max_requests_per_crawl: int = 50, domain_config=None):
        super().__init__(domain='topcv', max_requests_per_crawl=max_requests_per_crawl, domain_config=domain_config)
        extractor_config = self._load_selector_config()
        self.bs_selectors = extractor_config.get('bs', {}) if isinstance(extractor_config.get('bs'), dict) else {}
        self.playwright_selectors = (
            extractor_config.get('playwright', {}) if isinstance(extractor_config.get('playwright'), dict) else {}
        )

        if not self.bs_selectors and not self.playwright_selectors:
            self.playwright_selectors = extractor_config

        self.selectors = self.playwright_selectors

    def _load_selector_config(self) -> dict:
        return load_domain_selectors('topcv').get('extractor', {})

    def _is_brand_url(self, url: str) -> bool:
        return '/brand/' in urlsplit(url).path

    def _selector_candidates(self, selector_config: dict[str, Any], url: str) -> list[dict[str, Any]]:
        if not selector_config:
            return []

        has_typed_groups = any(name in selector_config for name in ('default', 'brand'))
        if not has_typed_groups:
            return [selector_config]

        group_name = 'brand' if self._is_brand_url(url) else 'default'
        candidates = self._normalize_selector_candidates(selector_config.get(group_name))
        if candidates:
            return candidates

        return self._normalize_selector_candidates(selector_config.get('default'))

    def _normalize_selector_candidates(self, selector_group: Any) -> list[dict[str, Any]]:
        if isinstance(selector_group, dict):
            return [selector_group]
        if isinstance(selector_group, list):
            return [item for item in selector_group if isinstance(item, dict)]
        return []

    def _selector_group(self, selector_config: dict[str, Any], url: str) -> dict[str, Any]:
        candidates = self._selector_candidates(selector_config, url)
        return candidates[0] if candidates else {}

    def _selector_list(self, selector: Any) -> list[str]:
        if not selector:
            return []
        if isinstance(selector, list):
            return [item for item in selector if item]
        return [selector]

    async def _extract_text(self, page, selector: Any) -> str:
        if not selector:
            return ''
        for item in self._selector_list(selector):
            try:
                loc = page.locator(item)
                if await loc.count() > 0:
                    text = await loc.first.inner_text()
                    return text.strip()
            except Exception as e:
                logger.debug(f'Error extracting text with Playwright selector {item}: {e}')
        return ''

    async def _extract_href(self, page, selector: Any) -> str:
        if not selector:
            return ''
        for item in self._selector_list(selector):
            try:
                loc = page.locator(item)
                if await loc.count() > 0:
                    href = await loc.first.get_attribute('href')
                    if href:
                        return urljoin(page.url, href)
            except Exception as e:
                logger.debug(f'Error extracting href with Playwright selector {item}: {e}')
        return ''

    async def _extract_multi_text(self, page, selector: Any, separator: str = ', ') -> str:
        if not selector:
            return ''
        texts = []
        for item in self._selector_list(selector):
            try:
                loc = page.locator(item)
                count = await loc.count()
                for index in range(count):
                    text = await loc.nth(index).inner_text()
                    if text.strip():
                        texts.append(text.strip())
            except Exception as e:
                logger.debug(f'Error extracting multi text with Playwright selector {item}: {e}')
        return separator.join(texts)

    def _extract_text_from_soup(self, soup: BeautifulSoup, selector: Any) -> str:
        if not selector:
            return ''
        for item in self._selector_list(selector):
            try:
                element = soup.select_one(item)
            except Exception as e:
                logger.debug(f'Error selecting raw HTML text with selector {item}: {e}')
                continue
            if element:
                text = element.get_text(' ', strip=True)
                if text:
                    return text
        return ''

    def _extract_href_from_soup(self, soup: BeautifulSoup, selector: Any, base_url: str) -> str:
        if not selector:
            return ''
        for item in self._selector_list(selector):
            try:
                element = soup.select_one(item)
            except Exception as e:
                logger.debug(f'Error selecting raw HTML href with selector {item}: {e}')
                continue
            if element and element.get('href'):
                return urljoin(base_url, str(element.get('href')))
        return ''

    def _extract_multi_text_from_soup(self, soup: BeautifulSoup, selector: Any, separator: str = ', ') -> str:
        if not selector:
            return ''
        texts = []
        for item in self._selector_list(selector):
            try:
                elements = soup.select(item)
            except Exception as e:
                logger.debug(f'Error selecting raw HTML multi text with selector {item}: {e}')
                continue
            for element in elements:
                text = element.get_text(' ', strip=True)
                if text:
                    texts.append(text)
        return separator.join(texts)

    async def try_process_raw_html(self, context: PlaywrightCrawlingContext) -> bool:
        link_id = context.request.user_data.get('link_id')
        if not link_id or not self.bs_selectors:
            return False

        selector_candidates = self._selector_candidates(self.bs_selectors, context.request.url)
        if not selector_candidates:
            logger.warning('Missing TopCV raw HTML title selector; falling back to Playwright.')
            return False

        proxy_info = getattr(context, 'proxy_info', None)
        proxy_url = str(proxy_info.url) if proxy_info and getattr(proxy_info, 'url', None) else None
        request_kwargs = {
            'impersonate': 'chrome',
            'timeout': 15,
            'http_version': 'v2',
        }
        if proxy_url:
            request_kwargs['proxy'] = proxy_url

        async with curl_requests.AsyncSession() as session:
            response = await session.get(context.request.url, **request_kwargs)

        content_type = response.headers.get('content-type', '')
        final_url = str(getattr(response, 'url', '') or context.request.url)
        logger.info(
            'TopCV curl_cffi raw HTML response status=%s final_url=%s proxy=%s content_type=%s.',
            response.status_code,
            final_url,
            _mask_proxy_url(proxy_url),
            content_type or 'unknown',
        )

        if response.status_code < 200 or response.status_code >= 300:
            logger.info(f'TopCV curl_cffi raw HTML request returned status={response.status_code}; falling back to Playwright.')
            return False
        if content_type and 'text/html' not in content_type.lower():
            logger.info(f'TopCV curl_cffi raw HTML request returned content-type={content_type}; falling back to Playwright.')
            return False

        html = response.text
        soup = BeautifulSoup(html, 'lxml')
        selectors = {}
        title = ''
        for candidate in selector_candidates:
            candidate_title = self._extract_text_from_soup(soup, candidate.get('title', ''))
            if candidate_title:
                selectors = candidate
                title = candidate_title
                break

        if not title:
            logger.info(f'TopCV raw HTML title not found for {context.request.url}; falling back to Playwright.')
            return False

        data: Dict[str, Any] = {
            'title': title,
            'company_name': self._extract_text_from_soup(soup, selectors.get('company_name', '')),
            'company_url': self._extract_href_from_soup(soup, selectors.get('company_url', ''), final_url),
            'contract_type': self._extract_text_from_soup(soup, selectors.get('contract_type', '')),
            'deadline': self._extract_text_from_soup(soup, selectors.get('deadline', '')),
            'description': self._extract_text_from_soup(soup, selectors.get('description', '')),
            'experience_level': self._extract_text_from_soup(soup, selectors.get('experience_level', '')),
            'location': self._extract_text_from_soup(soup, selectors.get('location', '')),
            'posted_time': self._extract_text_from_soup(soup, selectors.get('posted_time', '')),
            'salary': self._extract_text_from_soup(soup, selectors.get('salary', '')),
            'sector': self._extract_multi_text_from_soup(soup, selectors.get('sector', '')),
        }

        await self.save_job_detail(link_id, data, is_success=True)
        logger.info(f'TopCV raw HTML extraction succeeded: {context.request.url}')
        return True

    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        page = context.page
        request = context.request
        link_id = request.user_data.get('link_id')

        if not link_id:
            logger.error('Missing link_id in request. Skipping.')
            return

        logger.info(f'Extracting TopCV data with Playwright fallback: {request.url}')

        try:
            selector_candidates = self._selector_candidates(self.playwright_selectors, request.url)
            if not selector_candidates:
                logger.error('Missing TopCV Playwright title selector.')
                await self.save_job_detail(link_id, {}, is_success=False)
                return

            selectors = {}
            title = ''
            for candidate in selector_candidates:
                title_sel = candidate.get('title', '')
                if not title_sel:
                    continue
                try:
                    await page.wait_for_selector(title_sel, timeout=10000)
                    title = await self._extract_text(page, title_sel)
                    if title:
                        selectors = candidate
                        break
                except Exception as e:
                    logger.debug(f'TopCV Playwright title selector failed: {title_sel}: {e}')

            if not title:
                logger.error('TopCV Playwright fallback did not find a title.')
                await self.save_job_detail(link_id, {}, is_success=False)
                return

            data: Dict[str, Any] = {
                'title': title,
                'company_name': await self._extract_text(page, selectors.get('company_name', '')),
                'company_url': await self._extract_href(page, selectors.get('company_url', '')),
                'contract_type': await self._extract_text(page, selectors.get('contract_type', '')),
                'deadline': await self._extract_text(page, selectors.get('deadline', '')),
                'description': await self._extract_text(page, selectors.get('description', '')),
                'experience_level': await self._extract_text(page, selectors.get('experience_level', '')),
                'location': await self._extract_text(page, selectors.get('location', '')),
                'posted_time': await self._extract_text(page, selectors.get('posted_time', '')),
                'salary': await self._extract_text(page, selectors.get('salary', '')),
                'sector': await self._extract_multi_text(page, selectors.get('sector', '')),
            }

            await self.save_job_detail(link_id, data, is_success=True)
        except Exception as e:
            logger.error(f'Error extracting TopCV data from {request.url}: {e}')
            await self.save_job_detail(link_id, {}, is_success=False)
