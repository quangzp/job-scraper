import json
import logging
from typing import Any, Dict
from urllib.parse import urljoin

from crawlee.crawlers import PlaywrightCrawlingContext

from scrapers.config.selector_loader import load_domain_selectors
from .base import BaseExtractor

logger = logging.getLogger(__name__)


class ITViecExtractor(BaseExtractor):
    def __init__(self, max_requests_per_crawl: int = 50, domain_config=None):
        super().__init__(domain='itviec', max_requests_per_crawl=max_requests_per_crawl, domain_config=domain_config)
        extractor_config = self._load_selector_config()
        self.playwright_selectors = (
            extractor_config.get('playwright', {}) if isinstance(extractor_config.get('playwright'), dict) else {}
        )
        if not self.playwright_selectors:
            self.playwright_selectors = extractor_config
        self.selectors = self.playwright_selectors
        self._json_ld_cache: dict[str, list[Any]] = {}

    def _load_selector_config(self) -> dict:
        return load_domain_selectors('itviec').get('extractor', {})

    def _clean_text(self, text: str) -> str:
        return ' '.join(text.split())

    def _selector_list(self, selector: Any) -> list[str]:
        if not selector:
            return []
        if isinstance(selector, list):
            return [item for item in selector if item]
        return [selector]

    def _field_selector(self, config: Any) -> Any:
        if isinstance(config, dict):
            if config.get('type') == 'first':
                for candidate in config.get('candidates', []):
                    selector = self._field_selector(candidate)
                    if selector:
                        return selector
            return config.get('selector')
        return config

    def _first_selector(self, config: Any) -> str:
        selectors = self._selector_list(self._field_selector(config))
        return selectors[0] if selectors else ''

    async def _extract_text(self, page, selector: Any) -> str:
        for item in self._selector_list(selector):
            try:
                locator = page.locator(item)
                if await locator.count() > 0:
                    text = await locator.first.inner_text()
                    return self._clean_text(text)
            except Exception as exc:
                logger.debug(f"Error extracting ITViec text with selector {item}: {exc}")
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
                logger.debug(f"Error extracting ITViec multi text with selector {item}: {exc}")
        return separator.join(dict.fromkeys(texts))

    async def _extract_href(self, page, selector: Any) -> str:
        for item in self._selector_list(selector):
            try:
                locator = page.locator(item)
                if await locator.count() > 0:
                    href = await locator.first.get_attribute('href')
                    if href:
                        return urljoin(page.url, href)
            except Exception as exc:
                logger.debug(f"Error extracting ITViec href with selector {item}: {exc}")
        return ''

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

    def _stringify_value(self, value: Any, separator: str = ', ') -> str:
        if value is None:
            return ''
        if isinstance(value, str):
            return self._clean_text(value)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return separator.join(
                item
                for item in (self._stringify_value(child, separator) for child in value)
                if item
            )
        if isinstance(value, dict):
            return separator.join(
                item
                for item in (self._stringify_value(child, separator) for child in value.values())
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
                        logger.debug(f"Error parsing ITViec JSON-LD with selector {item}: {exc}")
                        continue
                    items.extend(self._flatten_json_ld_items(parsed))
            except Exception as exc:
                logger.debug(f"Error loading ITViec JSON-LD with selector {item}: {exc}")

        self._json_ld_cache[cache_key] = items
        return items

    async def _extract_json_ld(self, page, config: dict) -> str:
        selector = config.get('selector')
        schema_type = config.get('schema_type', '')
        separator = config.get('separator', ', ')
        paths = config.get('paths')
        path = config.get('path')
        template = config.get('template')

        if not selector or (not path and not paths):
            return ''

        items = await self._load_json_ld_items(page, selector)
        for item in items:
            if not self._matches_schema_type(item, schema_type):
                continue

            if paths:
                values = [
                    self._stringify_value(self._resolve_path(item, item_path), separator)
                    for item_path in paths
                ]
                text = separator.join(dict.fromkeys(value for value in values if value))
            else:
                text = self._stringify_value(self._resolve_path(item, path), separator)

            if text:
                return template.format(value=text) if template else text
        return ''

    async def _extract_field(self, page, config: Any) -> str:
        if not config:
            return ''

        if not isinstance(config, dict):
            return await self._extract_text(page, config)

        field_type = config.get('type', 'text')
        if field_type == 'first':
            for candidate in config.get('candidates', []):
                value = await self._extract_field(page, candidate)
                if value:
                    return value
            return ''
        selector = config.get('selector')
        if field_type == 'href':
            return await self._extract_href(page, selector)
        if field_type == 'multi_text':
            return await self._extract_multi_text(page, selector, config.get('separator', ', '))
        if field_type == 'json_ld':
            return await self._extract_json_ld(page, config)
        return await self._extract_text(page, selector)

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

    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        page = context.page
        request = context.request
        link_id = request.user_data.get('link_id')

        if not link_id:
            logger.error("Missing link_id in ITViec extractor request. Skipping.")
            return

        logger.info(f"Extracting ITViec data with Playwright: {request.url}")

        try:
            selectors = self.playwright_selectors or self.selectors
            title_sel = self._first_selector(selectors.get('title', ''))
            if not title_sel:
                logger.error("Missing ITViec title selector.")
                await self.save_job_detail(link_id, {}, is_success=False)
                return

            await page.wait_for_selector(title_sel, timeout=15000)

            data = await self._data_from_page(page, selectors)

            if not data['title']:
                logger.error(f"ITViec title was empty after selector matched: {request.url}")
                await self.save_job_detail(link_id, {}, is_success=False)
                return

            await self.save_job_detail(link_id, data, is_success=True)
        except Exception as exc:
            logger.error(f"Error extracting ITViec data from {request.url}: {exc}")
            await self.save_job_detail(link_id, {}, is_success=False)
