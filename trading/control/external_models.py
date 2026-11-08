"""Discover provider capabilities without exchange access or trading actions."""
import argparse
from datetime import datetime
from getpass import getpass
import json
import logging
import os
from pathlib import Path
import re
import tempfile

import httpx
from dotenv import dotenv_values, set_key

from trading.agents.external_catalog import discover, load_routes
from trading.config import Settings


def save_manifest(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temp:
        staging = Path(temp.name)
        json.dump(data, temp, ensure_ascii=False, indent=2, allow_nan=False)
    try:
        staging.chmod(0o600)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def setup_env(path, *, free_plan=False, ask=getpass):
    """Hidden input, atomic replacement, mode 0600; preserve other env lines."""
    path = Path(path)
    existing = dotenv_values(path) if path.exists() else {}
    updates = {}
    for name, label in [('OPENROUTER_API_KEY', 'OpenRouter API anahtarı'),
                        ('CLOUDFLARE_API_TOKEN', 'Cloudflare Workers AI token'),
                        ('CLOUDFLARE_ACCOUNT_ID', 'Cloudflare Account ID')]:
        value = ask(label + ' (gizli giriş; boş = mevcut değeri koru): ').strip()
        if '\n' in value or '\r' in value:
            raise ValueError('Anahtar tek satır olmalı')
        if value:
            updates[name] = value
    account = updates.get('CLOUDFLARE_ACCOUNT_ID', existing.get('CLOUDFLARE_ACCOUNT_ID', ''))
    if account and not re.fullmatch('[a-fA-F0-9]{32}', account):
        raise ValueError('Cloudflare Account ID 32 onaltılık karakter olmalı')
    if free_plan:
        updates['CLOUDFLARE_FREE_PLAN_CONFIRMED'] = 'true'
    updates.update(EXTERNAL_MODELS_ENABLED='true', AI_PROVIDER_POOL_ENABLED='true')
    original = path.read_text() if path.exists() else ''
    # Backups contain credentials too: restrictive permissions from creation.
    if path.exists():
        backup = path.with_name(path.name + '.backup-external-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(original)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temp:
        temp.write(original)
        staging = Path(temp.name)
    try:
        for key, value in updates.items():
            set_key(staging, key, value)
        staging.chmod(0o600)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='Ücretsiz model keşfi / gizli anahtar kurulumu; MEXC emri göndermez')
    parser.add_argument('command', choices=['setup', 'discover', 'status', 'probe'])
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--cloudflare-free-plan', action='store_true', help='Cloudflare hesabının Workers Free planında olduğunu onayla')
    parser.add_argument('--model', help='probe için keşfedilmiş tam model ID; tek istek tüketir')
    args = parser.parse_args()
    logging.getLogger('httpx').setLevel(logging.WARNING)
    if args.command == 'setup':
        setup_env(args.env_file, free_plan=args.cloudflare_free_plan)
        print('Anahtarlar gizli kaydedildi. Mevcut MEXC, Claude ve risk ayarları korundu. Henüz keşif yapılmadı.')
        return
    cfg = Settings(_env_file=args.env_file)
    if args.command == 'discover':
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            data = discover(cfg, client)
        save_manifest(cfg.external_models_file, data)
        print('Keşif kaydedildi:', cfg.external_models_file)
        for row in data['reports']:
            print(json.dumps({k: v for k, v in row.items() if k != 'excluded'}, ensure_ascii=False))
        for route in data['routes']:
            print(route['provider'], route['model'], 'görsel+metin' if route['vision'] else 'metin', route['format'])
        print('Katalog uygunluğu üretim erişimi/karar kalitesi testi değildir. Tam eleme gerekçeleri rapor dosyasında.')
        if not data['routes']:
            raise SystemExit(1)
    elif args.command == 'status':
        routes = load_routes(cfg.external_models_file)
        print('Güncel ve uygun dış model:', len(routes))
        for route in routes:
            print(route.provider, route.model, 'VLM+metin' if route.vision else 'metin')
        from trading.agents.provider_pool import ProviderPool
        from trading.runtime.store import RuntimeStore
        store = RuntimeStore(cfg.shared_budget_db_path)
        pool = ProviderPool(cfg, None, store)
        try:
            groups = [g for g in pool.snapshot()['groups'] if g['provider'] in ('openrouter', 'cloudflare')]
            print(json.dumps(groups, ensure_ascii=False, indent=2))
        finally:
            pool.close()
            store.db.close()
    else:
        # No market data, proposal, MEXC import, or live execution path in probe.
        from trading.agents.provider_pool import ProviderPool
        from trading.runtime.store import RuntimeStore
        routes = [r for r in load_routes(cfg.external_models_file) if r.model == args.model]
        if len(routes) != 1:
            parser.error('--model keşifteki uygun model ID olmalı')
        store = RuntimeStore(cfg.shared_budget_db_path)
        pool = ProviderPool(cfg, None, store)
        try:
            selected = [s for s in pool._slots if s.provider == routes[0].provider and s.model == args.model]
            if len(selected) != 1:
                parser.error('Dış havuz etkin değil veya gerekli anahtar/Free plan onayı eksik')
            pool._slots = selected
            pool.response_validator = lambda r: None if r == {'action': 'hold'} else (_ for _ in ()).throw(ValueError('probe_schema'))
            # Never invoke a paid fallback during probes.
            pool.settings = cfg.model_copy(update=dict(ai_claude_fallback_enabled=False,ai_primary_provider='groq',ai_vision_primary_provider='gemini'))
            pool._external_signature = None
            pool._reload_external = lambda: None
            result = pool.complete_json(system='Return JSON only. No trading actions.', user='Return exactly {"action":"hold"}.', max_tokens=700)
            print(json.dumps(dict(ok=result == {'action': 'hold'}, attempts=pool.last_trace.get('attempts', [])), ensure_ascii=False))
            if result != {'action': 'hold'}:raise SystemExit(1)
        finally:
            pool.close()
            store.db.close()


if __name__ == '__main__':
    main()
