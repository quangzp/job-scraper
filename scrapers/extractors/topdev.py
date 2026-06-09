import json
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


class TopDevExtractor(BaseExtractor):
    def __init__(self, max_requests_per_crawl: int = 50, domain_config=None):
        super().__init__(domain='topdev', max_requests_per_crawl=max_requests_per_crawl, domain_config=domain_config)
        extractor_config = self._load_selector_config()
        self.bs_selectors = extractor_config.get('bs', {}) if isinstance(extractor_config.get('bs'), dict) else {}
        self.playwright_selectors = (
            extractor_config.get('playwright', {}) if isinstance(extractor_config.get('playwright'), dict) else {}
        )

        if not self.bs_selectors and not self.playwright_selectors:
            self.playwright_selectors = extractor_config

        self.selectors = self.playwright_selectors
        self._json_ld_cache: dict[str, list[Any]] = {}

    def _load_selector_config(self) -> dict:
        return load_domain_selectors('topdev').get('extractor', {})

    def _selector_list(self, selector: Any) -> list[str]:
        if not selector:
            return []
        if isinstance(selector, list):
            return [item for item in selector if item]
        return [selector]

    def _clean_text(self, text: str) -> str:
        return ' '.join(text.split())

    def _path_parts(self, path: Any) -> list[Any]:
        if isinstance(path, list):
            return path
        if not isinstance(path, str):
            return []
        return [int(part) if part.isdigit() else part for part in path.split('.') if part != '']

    def _resolve_path(self, data: Any, path: Any) -> Any:
        value = data
        for part in self._path_parts(path):
            if isinstance(value, list):
                if not isinstance(part, int) or part >= len(value):
                    return None
                value = value[part]
                continue
            if isinstance(value, dict):
                value = value.get(part)
                continue
            return None
        return value

    def _html_to_text(self, html: str) -> str:
        return self._clean_text(BeautifulSoup(html, 'lxml').get_text(' ', strip=True))

    def _stringify_value(self, value: Any, separator: str = ', ', value_format: str = '') -> str:
        if value is None:
            return ''
        if isinstance(value, str):
            return self._html_to_text(value) if value_format == 'html_text' else self._clean_text(value)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return separator.join(
                item
                for item in (self._stringify_value(child, separator, value_format) for child in value)
                if item
            )
        if isinstance(value, dict):
            return separator.join(
                item
                for item in (self._stringify_value(child, separator, value_format) for child in value.values())
                if item
            )
        return self._clean_text(str(value))

    def _matches_schema_type(self, item: Any, schema_type: str) -> bool:
        if not schema_type or not isinstance(item, dict):
            return True
        item_type = item.get('@type')
        if isinstance(item_type, list):
            return schema_type in item_type
        return item_type == schema_type

    def _flatten_json_ld_items(self, raw_item: Any) -> list[Any]:
        if isinstance(raw_item, list):
            flattened = []
            for item in raw_item:
                flattened.extend(self._flatten_json_ld_items(item))
            return flattened
        if isinstance(raw_item, dict) and isinstance(raw_item.get('@graph'), list):
            return self._flatten_json_ld_items(raw_item['@graph'])
        return [raw_item]

    async def _extract_text(self, page, selector: Any) -> str:
        for item in self._selector_list(selector):
            try:
                locator = page.locator(item)
                if await locator.count() > 0:
                    text = await locator.first.inner_text()
                    return self._clean_text(text)
            except Exception as exc:
                logger.debug(f"Error extracting TopDev text with Playwright selector {item}: {exc}")
        return ''

    async def _extract_href(self, page, selector: Any) -> str:
        for item in self._selector_list(selector):
            try:
                locator = page.locator(item)
                if await locator.count() > 0:
                    href = await locator.first.get_attribute('href')
                    if href:
                        return urljoin(page.url, href)
            except Exception as exc:
                logger.debug(f"Error extracting TopDev href with Playwright selector {item}: {exc}")
        return ''

    async def _extract_multi_text(self, page, selector: Any, separator: str = ', ') -> str:
        texts = []
        for item in self._selector_list(selector):
            try:
                locator = page.locator(item)
                count = await locator.count()
                for index in range(count):
                    text = await locator.nth(index).inner_text()
                    cleaned_text = self._clean_text(text)
                    if cleaned_text:
                        texts.append(cleaned_text)
            except Exception as exc:
                logger.debug(f"Error extracting TopDev multi text with Playwright selector {item}: {exc}")
        return separator.join(dict.fromkeys(texts))

    def _extract_text_from_soup(self, soup: BeautifulSoup, selector: Any) -> str:
        for item in self._selector_list(selector):
            try:
                element = soup.select_one(item)
            except Exception as exc:
                logger.debug(f"Error selecting TopDev raw HTML text with selector {item}: {exc}")
                continue
            if element:
                text = element.get_text(' ', strip=True)
                if text:
                    return self._clean_text(text)
        return ''

    def _extract_href_from_soup(self, soup: BeautifulSoup, selector: Any, base_url: str) -> str:
        for item in self._selector_list(selector):
            try:
                element = soup.select_one(item)
            except Exception as exc:
                logger.debug(f"Error selecting TopDev raw HTML href with selector {item}: {exc}")
                continue
            if element and element.get('href'):
                return urljoin(base_url, str(element.get('href')))
        return ''

    def _extract_multi_text_from_soup(self, soup: BeautifulSoup, selector: Any, separator: str = ', ') -> str:
        texts = []
        for item in self._selector_list(selector):
            try:
                elements = soup.select(item)
            except Exception as exc:
                logger.debug(f"Error selecting TopDev raw HTML multi text with selector {item}: {exc}")
                continue
            for element in elements:
                text = self._clean_text(element.get_text(' ', strip=True))
                if text:
                    texts.append(text)
        return separator.join(dict.fromkeys(texts))

    async def _load_json_ld_items(self, page, selector: Any) -> list[Any]:
        cache_key = f'{page.url}|{selector}'
        if cache_key in self._json_ld_cache:
            return self._json_ld_cache[cache_key]

        items: list[Any] = []
        for item in self._selector_list(selector):
            try:
                locator = page.locator(item)
                count = await locator.count()
                for index in range(count):
                    raw_text = await locator.nth(index).text_content()
                    if not raw_text or not raw_text.strip():
                        continue
                    try:
                        parsed = json.loads(raw_text.strip())
                    except json.JSONDecodeError as exc:
                        logger.debug(f"Error parsing TopDev JSON-LD with selector {item}: {exc}")
                        continue
                    items.extend(self._flatten_json_ld_items(parsed))
            except Exception as exc:
                logger.debug(f"Error loading TopDev JSON-LD with selector {item}: {exc}")

        self._json_ld_cache[cache_key] = items
        return items

    async def _extract_json_ld(self, page, field_config: dict) -> str:
        selector = field_config.get('selector')
        schema_type = field_config.get('schema_type', '')
        separator = field_config.get('separator', ', ')
        paths = field_config.get('paths')
        path = field_config.get('path')
        template = field_config.get('template')
        value_format = field_config.get('format', '')

        if not selector or (not path and not paths):
            return ''

        for item in await self._load_json_ld_items(page, selector):
            if not self._matches_schema_type(item, schema_type):
                continue
            if paths:
                values = [
                    self._stringify_value(self._resolve_path(item, item_path), separator, value_format)
                    for item_path in paths
                ]
                text = separator.join(dict.fromkeys(value for value in values if value))
            else:
                text = self._stringify_value(self._resolve_path(item, path), separator, value_format)
            if text:
                return template.format(value=text) if template else text
        return ''

    def _load_json_ld_items_from_soup(self, soup: BeautifulSoup, selector: Any) -> list[Any]:
        items: list[Any] = []
        for item in self._selector_list(selector):
            try:
                elements = soup.select(item)
            except Exception as exc:
                logger.debug(f"Error selecting TopDev raw HTML JSON-LD with selector {item}: {exc}")
                continue
            for element in elements:
                raw_text = element.string or element.get_text('', strip=True)
                if not raw_text or not raw_text.strip():
                    continue
                try:
                    parsed = json.loads(raw_text.strip())
                except json.JSONDecodeError as exc:
                    logger.debug(f"Error parsing TopDev raw HTML JSON-LD with selector {item}: {exc}")
                    continue
                items.extend(self._flatten_json_ld_items(parsed))
        return items

    def _extract_json_ld_from_soup(self, soup: BeautifulSoup, field_config: dict) -> str:
        selector = field_config.get('selector')
        schema_type = field_config.get('schema_type', '')
        separator = field_config.get('separator', ', ')
        paths = field_config.get('paths')
        path = field_config.get('path')
        template = field_config.get('template')
        value_format = field_config.get('format', '')

        if not selector or (not path and not paths):
            return ''

        for item in self._load_json_ld_items_from_soup(soup, selector):
            if not self._matches_schema_type(item, schema_type):
                continue
            if paths:
                values = [
                    self._stringify_value(self._resolve_path(item, item_path), separator, value_format)
                    for item_path in paths
                ]
                text = separator.join(dict.fromkeys(value for value in values if value))
            else:
                text = self._stringify_value(self._resolve_path(item, path), separator, value_format)
            if text:
                return template.format(value=text) if template else text
        return ''

    def _field_type(self, field_config: Any) -> str:
        if isinstance(field_config, dict):
            return field_config.get('type', 'text')
        return 'text'

    def _field_selector(self, field_config: Any) -> Any:
        if isinstance(field_config, dict):
            if field_config.get('type') == 'first':
                for candidate in field_config.get('candidates', []):
                    selector = self._field_selector(candidate)
                    if selector:
                        return selector
            return field_config.get('selector')
        return field_config

    def _extract_field_from_soup(self, soup: BeautifulSoup, field_config: Any, base_url: str) -> str:
        field_type = self._field_type(field_config)
        if field_type == 'first' and isinstance(field_config, dict):
            for candidate in field_config.get('candidates', []):
                value = self._extract_field_from_soup(soup, candidate, base_url)
                if value:
                    return value
            return ''
        selector = self._field_selector(field_config)
        if field_type == 'href':
            return self._extract_href_from_soup(soup, selector, base_url)
        if field_type == 'multi_text':
            separator = field_config.get('separator', ', ') if isinstance(field_config, dict) else ', '
            return self._extract_multi_text_from_soup(soup, selector, separator)
        if field_type == 'json_ld' and isinstance(field_config, dict):
            return self._extract_json_ld_from_soup(soup, field_config)
        return self._extract_text_from_soup(soup, selector)

    async def _extract_field(self, page, field_config: Any) -> str:
        field_type = self._field_type(field_config)
        if field_type == 'first' and isinstance(field_config, dict):
            for candidate in field_config.get('candidates', []):
                value = await self._extract_field(page, candidate)
                if value:
                    return value
            return ''
        selector = self._field_selector(field_config)
        if field_type == 'href':
            return await self._extract_href(page, selector)
        if field_type == 'multi_text':
            separator = field_config.get('separator', ', ') if isinstance(field_config, dict) else ', '
            return await self._extract_multi_text(page, selector, separator)
        if field_type == 'json_ld' and isinstance(field_config, dict):
            return await self._extract_json_ld(page, field_config)
        return await self._extract_text(page, selector)

    def _data_from_soup(self, soup: BeautifulSoup, selectors: dict[str, Any], base_url: str) -> Dict[str, Any]:
        return {
            'title': self._extract_field_from_soup(soup, selectors.get('title', ''), base_url),
            'company_name': self._extract_field_from_soup(soup, selectors.get('company_name', ''), base_url),
            'company_url': self._extract_field_from_soup(soup, selectors.get('company_url', ''), base_url),
            'contract_type': self._extract_field_from_soup(soup, selectors.get('contract_type', ''), base_url),
            'deadline': self._extract_field_from_soup(soup, selectors.get('deadline', ''), base_url),
            'description': self._extract_field_from_soup(soup, selectors.get('description', ''), base_url),
            'experience_level': self._extract_field_from_soup(soup, selectors.get('experience_level', ''), base_url),
            'location': self._extract_field_from_soup(soup, selectors.get('location', ''), base_url),
            'posted_time': self._extract_field_from_soup(soup, selectors.get('posted_time', ''), base_url),
            'salary': self._extract_field_from_soup(soup, selectors.get('salary', ''), base_url),
            'sector': self._extract_field_from_soup(soup, selectors.get('sector', ''), base_url),
        }

    async def _data_from_page(self, page, selectors: dict[str, Any]) -> Dict[str, Any]:
        return {
            'title': await self._extract_field(page, selectors.get('title', '')),
            'company_name': await self._extract_field(page, selectors.get('company_name', '')),
            'company_url': await self._extract_field(page, selectors.get('company_url', '')),
            'contract_type': await self._extract_field(page, selectors.get('contract_type', '')),
            'deadline': await self._extract_field(page, selectors.get('deadline', '')),
            'description': await self._extract_field(page, selectors.get('description', '')),
            'experience_level': await self._extract_field(page, selectors.get('experience_level', '')),
            'location': await self._extract_field(page, selectors.get('location', '')),
            'posted_time': await self._extract_field(page, selectors.get('posted_time', '')),
            'salary': await self._extract_field(page, selectors.get('salary', '')),
            'sector': await self._extract_field(page, selectors.get('sector', '')),
        }

    async def try_process_raw_html(self, context: PlaywrightCrawlingContext) -> bool:
        link_id = context.request.user_data.get('link_id')
        if not link_id or not self.bs_selectors:
            return False

        title_selector = self._field_selector(self.bs_selectors.get('title', ''))
        if not title_selector:
            logger.warning("Missing TopDev raw HTML title selector; falling back to Playwright.")
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
            'TopDev curl_cffi raw HTML response status=%s final_url=%s proxy=%s content_type=%s.',
            response.status_code,
            final_url,
            _mask_proxy_url(proxy_url),
            content_type or 'unknown',
        )

        if response.status_code < 200 or response.status_code >= 300:
            logger.info(f"TopDev curl_cffi raw HTML request returned status={response.status_code}; falling back to Playwright.")
            return False
        if content_type and 'text/html' not in content_type.lower():
            logger.info(f"TopDev curl_cffi raw HTML request returned content-type={content_type}; falling back to Playwright.")
            return False

        html = response.text
        soup = BeautifulSoup(html, 'lxml')
        data = self._data_from_soup(soup, self.bs_selectors, final_url)
        if not data['title']:
            logger.info(f"TopDev raw HTML title not found for {context.request.url}; falling back to Playwright.")
            return False

        await self.save_job_detail(link_id, data, is_success=True)
        logger.info(f"TopDev raw HTML extraction succeeded: {context.request.url}")
        return True

    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        page = context.page
        request = context.request
        link_id = request.user_data.get('link_id')

        if not link_id:
            logger.error("Missing link_id in TopDev extractor request. Skipping.")
            return

        logger.info(f"Extracting TopDev data with Playwright fallback: {request.url}")

        try:
            title_selector = self._field_selector(self.playwright_selectors.get('title', ''))
            if not title_selector:
                logger.error("Missing TopDev Playwright title selector.")
                await self.save_job_detail(link_id, {}, is_success=False)
                return

            await page.wait_for_selector(title_selector, timeout=15000)
            data = await self._data_from_page(page, self.playwright_selectors)

            if not data['title']:
                logger.error(f"TopDev title was empty after selector matched: {request.url}")
                await self.save_job_detail(link_id, {}, is_success=False)
                return

            await self.save_job_detail(link_id, data, is_success=True)
        except Exception as exc:
            logger.error(f"Error extracting TopDev data from {request.url}: {exc}")
            await self.save_job_detail(link_id, {}, is_success=False)
