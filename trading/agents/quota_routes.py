"""Explicit operator-verified free model routes; never discover/invent quota."""
import json
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field


class FreeRoute(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    provider: Literal['groq','gemini']
    model: str=Field(min_length=1,max_length=120,pattern=r'^[A-Za-z0-9._/:-]+$')
    modes: list[Literal['text','vlm']]=Field(min_length=1,max_length=2)
    quota_bucket: str=Field(min_length=1,max_length=120,pattern=r'^[A-Za-z0-9._/:-]+$')
    rpm: int=Field(ge=1)
    tpm: int=Field(ge=1)
    rpd: int=Field(ge=1)
    tpd: int=Field(ge=0)
    free_access_verified: Literal[True]


def parse_routes(raw):
    try:
        rows=json.loads(raw)
        if not isinstance(rows,list) or len(rows)>12:raise ValueError()
        return [FreeRoute.model_validate(row) for row in rows]
    except Exception:
        raise ValueError('invalid verified free-model route configuration') from None
