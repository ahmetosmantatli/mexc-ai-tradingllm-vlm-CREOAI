"""Apply Ahmet's explicitly supplied 2026-11-08 FREE quotas, not universal defaults.

No network, credentials printed, counter reset, exchange action or service start.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shutil


def supplied_routes():
    rows=[]
    def add(provider,model,rpm,tpm,rpd,tpd=0,modes=('text','vlm'),bucket=None):
        rows.append(dict(provider=provider,model=model,modes=list(modes),quota_bucket=bucket or model,
            rpm=rpm,tpm=tpm,rpd=rpd,tpd=tpd,free_access_verified=True))
    add('groq','openai/gpt-oss-120b',30,8000,1000,200000,modes=('text',))
    add('groq','qwen/qwen3.8-27b',30,8000,1000,200000)
    add('gemini','gemini-3.1-flash-lite',15,250000,500)
    # Conservative shared bucket until separate Gemma quota metrics are verified.
    add('gemini','gemma-4-26b-a4b-it',30,16000,14400,bucket='gemma-4-shared')
    add('gemini','gemma-4-31b-it',30,16000,14400,bucket='gemma-4-shared')
    add('gemini','gemini-2.5-flash',5,250000,20)
    add('gemini','gemini-2.5-flash-lite',10,250000,20)
    add('gemini','gemini-3.6-flash',5,250000,20)
    add('gemini','gemini-3.8-flash',5,250000,20)
    return rows


def configure(path):
    path=Path(path)
    original=path.read_text()
    values=dict(AI_PROVIDER_POOL_ENABLED='true',AI_PRIMARY_PROVIDER='groq',AI_VISION_PRIMARY_PROVIDER='gemini',
        AI_CLAUDE_FALLBACK_ENABLED='false',AI_BUDGET_ENABLED='true',AI_POOL_RETRY_SECONDS='5',AI_POOL_MAX_RETRY_ATTEMPTS='1',
        AI_FREE_MODEL_ROUTES="'"+json.dumps(supplied_routes(),separators=(',',':'))+"'")
    pattern=re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')
    lines=[];seen=set()
    for line in original.splitlines():
        match=pattern.match(line)
        if match and match[1] in values:
            if match[1] not in seen:lines.append(match[1]+'='+values[match[1]]);seen.add(match[1])
        else:lines.append(line)
    lines.extend(key+'='+value for key,value in values.items() if key not in seen)
    backup=path.with_name(path.name+'.backup-free-pool-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    shutil.copy2(path,backup);backup.chmod(0o600)
    path.write_text('\n'.join(lines)+'\n');path.chmod(0o600)
    return backup


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',default='.env')
    args=parser.parse_args()
    backup=configure(args.env_file)
    print('Ücretsiz model havuzu ayarlandı. Claude yedeği KAPALI. API anahtarları ve kota sayaçları korunuyor.')
    print('Yedek:',backup)
    print('Groq8K TPM bağlamı sığmazsa atlanır; Gemma modelleri proje başına ortak korumacı kota kullanır.')
