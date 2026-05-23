import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crawlee.proxy_configuration import ProxyConfiguration

logger = logging.getLogger(__name__)

DEFAULT_PROXY_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'proxies.json'


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _clean_proxy_urls(values: list[Any] | None) -> list[str]:
    if not values:
        return []
    return [str(value).strip() for value in values if value and str(value).strip()]


def _is_supported_proxy_url(url: str) -> bool:
    try:
        scheme = urlsplit(url).scheme.lower()
    except Exception:
        return False
    return scheme in {'http', 'https'}


def _pick_domain_proxy_config(config: dict[str, Any], domain: str | None) -> dict[str, Any]:
    if not domain:
        return {}
    domains = config.get('domains', {})
    if not isinstance(domains, dict):
        return {}
    domain_config = domains.get(domain, {})
    if isinstance(domain_config, dict):
        return domain_config
    return {}


def load_proxy_configuration(domain: str | None = None) -> ProxyConfiguration | None:
    """Load Crawlee ProxyConfiguration from JSON config.

    Config format:
    {
      "enabled": true,
      "global": { "proxy_urls": ["http://user:pass@host:port"] },
      "domains": {
        "topcv": { "proxy_urls": ["http://..."] }
      }
    }
    """
    config_path = Path(os.getenv('CRAWLEE_PROXY_CONFIG', str(DEFAULT_PROXY_CONFIG_PATH)))
    if not config_path.exists():
        logger.info(f'Proxy config not found at {config_path}. Running without proxy.')
        return None

    try:
        with config_path.open('r', encoding='utf-8') as file:
            raw_config = json.load(file)
    except Exception as exc:
        logger.error(f'Failed to read proxy config {config_path}: {exc}')
        return None

    if not isinstance(raw_config, dict):
        logger.error(f'Invalid proxy config format in {config_path}: root must be object.')
        return None

    enabled = _env_bool('CRAWLEE_PROXY_ENABLED', raw_config.get('enabled', False))
    if not enabled:
        logger.info('Proxy is disabled by config/env. Running without proxy.')
        return None

    global_config = raw_config.get('global', {})
    if not isinstance(global_config, dict):
        global_config = {}

    domain_config = _pick_domain_proxy_config(raw_config, domain)

    proxy_urls = _clean_proxy_urls(domain_config.get('proxy_urls')) or _clean_proxy_urls(global_config.get('proxy_urls'))
    invalid_proxy_urls = [url for url in proxy_urls if not _is_supported_proxy_url(url)]
    if invalid_proxy_urls:
        logger.warning(
            f'Ignoring {len(invalid_proxy_urls)} proxy URL(s) with unsupported scheme. '
            f'Only http/https are accepted by Crawlee ProxyConfiguration.'
        )
    proxy_urls = [url for url in proxy_urls if _is_supported_proxy_url(url)]
    if not proxy_urls:
        logger.warning(f'Proxy is enabled but no proxy_urls configured for domain={domain}.')
        return None

    logger.info(f'Loaded {len(proxy_urls)} proxy URL(s) for domain={domain or "global"}.')
    return ProxyConfiguration(proxy_urls=proxy_urls)
