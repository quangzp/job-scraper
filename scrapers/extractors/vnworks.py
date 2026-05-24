import logging
from typing import Any, Dict
from urllib.parse import urljoin

from crawlee.crawlers import PlaywrightCrawlingContext

from scrapers.config.selector_loader import load_domain_selectors
from .base import BaseExtractor

logger = logging.getLogger(__name__)


class VNWorksExtractor(BaseExtractor):
    def __init__(self, max_requests_per_crawl: int = 50, domain_config=None):
        super().__init__(domain='vnworks', max_requests_per_crawl=max_requests_per_crawl, domain_config=domain_config)
        self.selectors = self._load_selectors()

    def _load_selectors(self) -> dict:
        return load_domain_selectors('vnworks').get('extractor', {})

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
            return config.get('selector')
        return config

    def _first_selector(self, config: Any) -> str:
        selectors = self._selector_list(self._field_selector(config))
        return selectors[0] if selectors else ''

    async def _extract_text(self, page, selector: Any) -> str:
        if not selector:
            return ''
        for item in self._selector_list(selector):
            try:
                locator = page.locator(item)
                if await locator.count() > 0:
                    text = await locator.first.inner_text()
                    return self._clean_text(text)
            except Exception as exc:
                logger.debug(f'Error extracting VNWorks text with selector {item}: {exc}')
        return ''

    async def _extract_href(self, page, selector: Any) -> str:
        if not selector:
            return ''
        for item in self._selector_list(selector):
            try:
                locator = page.locator(item)
                if await locator.count() > 0:
                    href = await locator.first.get_attribute('href')
                    if href:
                        return urljoin(page.url, href)
            except Exception as exc:
                logger.debug(f'Error extracting VNWorks href with selector {item}: {exc}')
        return ''

    async def _extract_label_text(self, page, config: dict) -> str:
        label = config.get('label', '')
        label_selector = config.get('label_selector', '')
        value_selector = config.get('value_selector', '')
        if not label or not label_selector or not value_selector:
            return ''

        try:
            text = await page.locator(label_selector).evaluate_all(
                """
                (labels, args) => {
                    const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toUpperCase();
                    const target = normalize(args.label);

                    for (const label of labels) {
                        if (normalize(label.textContent) !== target) continue;

                        let node = label.parentElement;
                        for (let depth = 0; node && depth < 8; depth += 1) {
                            const value = node.querySelector(args.valueSelector);
                            if (value && value !== label) {
                                return value.innerText || value.textContent || '';
                            }
                            node = node.parentElement;
                        }
                    }

                    return '';
                }
                """,
                {'label': label, 'valueSelector': value_selector},
            )
            return self._clean_text(text)
        except Exception as exc:
            logger.debug(f'Error extracting VNWorks label {label}: {exc}')
            return ''

    async def _extract_section_text(self, page, config: dict) -> str:
        heading = config.get('heading', '')
        heading_selector = config.get('heading_selector', '')
        value_selector = config.get('value_selector') or config.get('content_selector', '')
        separator = config.get('separator', '\n')
        if not heading or not heading_selector or not value_selector:
            return ''

        try:
            values = await page.locator(heading_selector).evaluate_all(
                """
                (headings, args) => {
                    const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const target = normalize(args.heading).toUpperCase();

                    for (const heading of headings) {
                        if (normalize(heading.textContent).toUpperCase() !== target) continue;

                        let node = heading.parentElement;
                        for (let depth = 0; node && depth < 8; depth += 1) {
                            const values = Array.from(node.querySelectorAll(args.valueSelector))
                                .filter((value) => value !== heading && !value.contains(heading))
                                .map((value) => normalize(value.innerText || value.textContent))
                                .filter(Boolean);

                            if (values.length > 0) return values;
                            node = node.parentElement;
                        }
                    }

                    return [];
                }
                """,
                {'heading': heading, 'valueSelector': value_selector},
            )
            return separator.join(dict.fromkeys(values))
        except Exception as exc:
            logger.debug(f'Error extracting VNWorks section {heading}: {exc}')
            return ''

    async def _extract_sections_text(self, page, config: dict) -> str:
        headings = config.get('headings', [])
        separator = config.get('separator', '\n\n')
        if not headings:
            return ''

        sections = []
        for heading in headings:
            section_config = {
                **config,
                'heading': heading,
                'separator': '\n',
            }
            text = await self._extract_section_text(page, section_config)
            if text:
                sections.append(text)
        return separator.join(sections)

    async def _expand_hidden_sections(self, page) -> None:
        selectors = self._selector_list(self.selectors.get('expand_buttons', []))
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                for index in range(count):
                    button = locator.nth(index)
                    if not await button.is_visible():
                        continue
                    await button.click(timeout=3000)
                    await page.wait_for_timeout(500)
            except Exception as exc:
                logger.debug(f'Error clicking VNWorks expand button {selector}: {exc}')

    async def _extract_field(self, page, config: Any) -> str:
        if not config:
            return ''

        if not isinstance(config, dict):
            return await self._extract_text(page, config)

        field_type = config.get('type', 'text')
        if field_type == 'href':
            return await self._extract_href(page, config.get('selector'))
        if field_type == 'label':
            return await self._extract_label_text(page, config)
        if field_type == 'section':
            return await self._extract_section_text(page, config)
        if field_type == 'sections':
            return await self._extract_sections_text(page, config)
        return await self._extract_text(page, config.get('selector'))

    async def process_page(self, context: PlaywrightCrawlingContext) -> None:
        page = context.page
        request = context.request
        link_id = request.user_data.get('link_id')

        if not link_id:
            logger.error('Missing link_id in VNWorks extractor request. Skipping.')
            return

        logger.info(f'Extracting VNWorks data with Playwright: {request.url}')

        try:
            title_sel = self._first_selector(self.selectors.get('title', ''))
            if not title_sel:
                logger.error('Missing VNWorks title selector.')
                await self.save_job_detail(link_id, {}, is_success=False)
                return

            await page.wait_for_selector(title_sel, timeout=15000)
            await self._expand_hidden_sections(page)

            data: Dict[str, Any] = {
                'title': await self._extract_field(page, self.selectors.get('title', '')),
                'company_name': await self._extract_field(page, self.selectors.get('company_name', '')),
                'company_url': await self._extract_field(page, self.selectors.get('company_url', '')),
                'contract_type': await self._extract_field(page, self.selectors.get('contract_type', '')),
                'deadline': await self._extract_field(page, self.selectors.get('deadline', '')),
                'description': await self._extract_field(page, self.selectors.get('description', '')),
                'experience_level': await self._extract_field(page, self.selectors.get('experience_level', '')),
                'location': await self._extract_field(page, self.selectors.get('location', '')),
                'posted_time': await self._extract_field(page, self.selectors.get('posted_time', '')),
                'salary': await self._extract_field(page, self.selectors.get('salary', '')),
                'sector': await self._extract_field(page, self.selectors.get('sector', '')),
            }

            if not data['title']:
                logger.error(f'VNWorks title was empty after selector matched: {request.url}')
                await self.save_job_detail(link_id, {}, is_success=False)
                return

            await self.save_job_detail(link_id, data, is_success=True)
        except Exception as exc:
            logger.error(f'Error extracting VNWorks data from {request.url}: {exc}')
            await self.save_job_detail(link_id, {}, is_success=False)
