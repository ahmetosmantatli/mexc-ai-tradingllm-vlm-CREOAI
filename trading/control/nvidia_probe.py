"""Check one NVIDIA Build model outside the trading runtime."""

import argparse
import base64
from getpass import getpass
import json
import sys
from io import BytesIO
import os
from pathlib import Path
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='mistralai/mistral-large-4-1t-a52b')
    vision = parser.add_mutually_exclusive_group()
    vision.add_argument('--image', type=Path, help='PNG for the vision request')
    vision.add_argument('--vision', action='store_true', help='Send a generated image with no account data')
    args = parser.parse_args()
    key = os.environ.get('NVIDIA_API_KEY', '')
    if not key and sys.stdin.isatty():
        key = getpass('NVIDIA API key: ')
    if not key.strip():
        parser.error('NVIDIA API key is required')
    content = [{'type': 'text', 'text': 'Return only this JSON object: {"action":"hold"}'}]
    if args.image or args.vision:
        if args.vision:
            from PIL import Image
            buffer = BytesIO()
            Image.new('RGB', (256, 256), 'blue').save(buffer, format='PNG')
            png = buffer.getvalue()
        else:
            png = args.image.read_bytes()
        encoded = base64.b64encode(png).decode('ascii')
        content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + encoded}})
    payload = {
        'model': args.model,
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'max_tokens': 1024,
    }
    result = {'model': args.model, 'vision_input': bool(args.image or args.vision)}
    start = time.monotonic()
    try:
        response = httpx.post('https://integrate.api.nvidia.com/v1/chat/completions',
                              headers={'Authorization': 'Bearer ' + key.strip()},
                              json=payload, timeout=20, follow_redirects=False)
        result['http_status'] = response.status_code
        for header in ('retry-after', 'x-ratelimit-limit-requests', 'x-ratelimit-remaining-requests',
                       'x-ratelimit-reset-requests', 'x-ratelimit-limit-tokens',
                       'x-ratelimit-remaining-tokens', 'x-ratelimit-reset-tokens'):
            if header in response.headers:
                result[header] = response.headers[header]
        if response.status_code == 200:
            try:
                choice = response.json()['choices'][0]
                result['valid_probe_decision'] = (choice.get('finish_reason') == 'stop'
                    and json.loads(choice['message']['content']) == {'action': 'hold'})
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                result['valid_probe_decision'] = False
    except httpx.RequestError as exc:
        result['error_type'] = type(exc).__name__
    result['latency_ms'] = round((time.monotonic() - start) * 1000)
    print(json.dumps(result))
    return 0 if result.get('valid_probe_decision') else 1


if __name__ == '__main__':
    raise SystemExit(main())
