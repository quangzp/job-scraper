import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

from django.db import close_old_connections

from crawlee.proxy_configuration import ProxyConfiguration

logger = logging.getLogger(__name__)


def mask_proxy_url(proxy_url) -> str:
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


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _clean_proxy_urls(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [str(value).strip() for value in values if value and str(value).strip()]


def _is_supported_proxy_url(url: str) -> bool:
    try:
        scheme = urlsplit(url).scheme.lower()
    except Exception:
        return False
    return scheme in {'http', 'https'}


def _fetch_proxy_urls_from_db(domain: str | None) -> list[str]:
    from app_dashboard.models import ProxyConfig, TargetDomain

    close_old_connections()
    try:
        if domain:
            domain_config = TargetDomain.objects.filter(name=domain).only('is_proxy_enabled').first()
            if domain_config and not domain_config.is_proxy_enabled:
                logger.info(f'Proxy is disabled for domain={domain}. Running without proxy.')
                return []

            domain_proxy_urls = list(
                ProxyConfig.objects.filter(is_active=True, domain__name=domain)
                .order_by('priority', 'id')
                .values_list('proxy_url', flat=True)
            )
            if domain_proxy_urls:
                return domain_proxy_urls

        return list(
            ProxyConfig.objects.filter(is_active=True, domain__isnull=True)
            .order_by('priority', 'id')
            .values_list('proxy_url', flat=True)
        )
    finally:
        close_old_connections()


def _fetch_proxy_urls_safely(domain: str | None) -> list[str]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _fetch_proxy_urls_from_db(domain)

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_fetch_proxy_urls_from_db, domain).result()


def load_proxy_configuration(domain: str | None = None, component: str = 'Worker') -> ProxyConfiguration | None:
    """Load Crawlee ProxyConfiguration from ProxyConfig rows in the database."""
    domain_label = domain or 'global'
    enabled = _env_bool('CRAWLEE_PROXY_ENABLED', True)
    if not enabled:
        logger.info(
            '%s proxy config domain=%s status=disabled_by_env proxy=none',
            component,
            domain_label,
        )
        return None

    try:
        proxy_urls = _clean_proxy_urls(_fetch_proxy_urls_safely(domain))
    except Exception as exc:
        logger.error(
            '%s proxy config domain=%s status=load_failed error=%s',
            component,
            domain_label,
            exc,
        )
        return None

    invalid_proxy_urls = [url for url in proxy_urls if not _is_supported_proxy_url(url)]
    if invalid_proxy_urls:
        logger.warning(
            '%s proxy config domain=%s ignored_invalid_proxy_count=%s. '
            'Only http/https are accepted by Crawlee ProxyConfiguration.',
            component,
            domain_label,
            len(invalid_proxy_urls),
        )
    proxy_urls = [url for url in proxy_urls if _is_supported_proxy_url(url)]
    if not proxy_urls:
        logger.info(
            '%s proxy config domain=%s status=empty proxy=none',
            component,
            domain_label,
        )
        return None

    masked_proxy_urls = [mask_proxy_url(url) for url in proxy_urls]
    logger.info(
        '%s proxy config domain=%s status=enabled proxy_count=%s proxies=%s',
        component,
        domain_label,
        len(proxy_urls),
        masked_proxy_urls,
    )
    return ProxyConfiguration(proxy_urls=proxy_urls)
