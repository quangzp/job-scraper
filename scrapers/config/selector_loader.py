import json
from pathlib import Path
from typing import Any


SELECTOR_CONFIG_DIR = Path(__file__).resolve().parent / 'selectors'


def load_domain_selectors(domain: str) -> dict[str, Any]:
    config_path = SELECTOR_CONFIG_DIR / f'{domain}.json'
    if not config_path.exists():
        raise FileNotFoundError(f'Selector config not found for domain={domain}: {config_path}')

    with config_path.open('r', encoding='utf-8-sig') as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f'Selector config for domain={domain} must be a JSON object.')
    return data
