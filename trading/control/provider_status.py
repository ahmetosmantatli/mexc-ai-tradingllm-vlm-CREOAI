"""Local quota snapshot plus opt-in authenticated model catalog GETs only."""
import argparse
import json
from pathlib import Path
import re
import httpx
from trading.config import get_settings
from trading.runtime.store import RuntimeStore
from trading.agents.provider_pool import ProviderPool, key_slot_numbers


def catalog(settings,client):
    results=[];seen=set()
    for provider in ('gemini','groq'):
        for number in key_slot_numbers(provider):
            suffix='' if number==1 else '_'+str(number)
            key=getattr(settings,provider+'_api_key'+suffix,'')
            if not key or (provider,key) in seen:continue
            seen.add((provider,key))
            row=dict(credential=provider+'-'+str(number),free_access_verified=False,quota_verified=False)
            url='https://generativelanguage.googleapis.com/v1beta/models?pageSize=100' if provider=='gemini' else 'https://api.groq.com/openai/v1/models'
            headers={'x-goog-api-key':key} if provider=='gemini' else {'Authorization':'Bearer '+key}
            try:
                response=client.get(url,headers=headers)
                row['http_status']=response.status_code
                response.raise_for_status();data=response.json()
                names=[]
                for model in data.get('models' if provider=='gemini' else 'data',[]):
                    name=model.get('name' if provider=='gemini' else 'id','').removeprefix('models/')
                    if provider=='gemini' and 'generateContent' not in model.get('supportedGenerationMethods',[]):continue
                    if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9._/:-]{1,120}',name):names.append(name)
                row['models']=sorted(names)
                row['catalog_may_be_paginated']=bool(data.get('nextPageToken'))
            except Exception:row['status']='catalog_unavailable'
            results.append(row)
    return results


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--catalog',action='store_true')
    args=parser.parse_args();cfg=get_settings()
    if not Path(cfg.shared_budget_db_path).exists():parser.error('existing shared quota database not found')
    store=RuntimeStore(cfg.shared_budget_db_path)
    client=httpx.Client(timeout=10,follow_redirects=False,trust_env=False)
    pool=None
    try:
        pool=ProviderPool(cfg,None,store,client)
        result=pool.snapshot()
        result['paid_fallback_enabled']=cfg.ai_claude_fallback_enabled
        result['note']='Local usage only; other applications are not observed. Provider headers are from the last response, not a fresh balance check. Catalog is not proof of free access or independent quota.'
        if args.catalog:result['catalog']=catalog(cfg,client)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        if pool:pool.close()
        client.close();store.db.close()


if __name__=='__main__':main()
