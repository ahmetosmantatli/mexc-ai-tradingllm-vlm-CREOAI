"""Opt-in text AI batch for the Runner 2 SHADOW coordinator."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from time import time
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from trading.runtime.runner2 import RUNNER_ID, Record
from trading.runtime.runner2_portfolio import Dispatch, Evidence, Retrieve, ReviewResult
from trading.runtime.usage import archive_trace


class Advice(Record):
    action: Literal['hold', 'trade', 'watch', 'reduce', 'close']
    reason: str = Field(min_length=12, max_length=800)
    close_fraction: float | None = Field(default=None, gt=0, lt=1, strict=True)

    @model_validator(mode='after')
    def validate_reduction(self):
        if (self.action == 'reduce') != (self.close_fraction is not None):
            raise ValueError('reduce_requires_fraction')
        return self


def run_review_batch(portfolio, pool, *, ceiling=2, clock=time):
    """Use the same shared provider quota/fallback router; archive proposals only."""
    batch_id = uuid4().hex
    capacity = pool.analysis_capacity(max_tokens=500, images=False, ceiling=ceiling)
    dispatched = portfolio.apply(Dispatch(kind='dispatch', command_id=batch_id, at=int(clock()),
                                           available_slots=capacity['parallel_candidates']))
    leases = dispatched['leases']
    if not leases:
        return dict(capacity=capacity, reviews=[])

    def evaluate(job, examples):
        child = pool.fork()
        allowed = ('hold', 'trade', 'watch') if job['trigger'] == 'entry' else ('hold', 'reduce', 'close')
        task = 'entry' if job['trigger'] == 'entry' else 'position_management'
        trace = dict(id=job['lease'], runner_id=RUNNER_ID, account_id=portfolio.account,
                     mode='SHADOW', analysis_version='text', task=task, strategy=job['trigger'],
                     symbol=job['evidence']['symbol'], queued_at=job['at'], started_at=clock(),
                     status='pending', attempts=[])

        def archive(update):
            trace.update(update)
            archive_trace(portfolio.store, trace)

        def validate(payload):
            advice = Advice.model_validate(payload)
            if advice.action not in allowed:
                raise ValueError('action_incompatible_with_review')
            return advice

        try:
            child.deadline = job['lease_until']
            child.decision_schema = Advice.model_json_schema()
            child.response_validator = validate
            child.on_trace = archive
            context = dict(allowed_actions=allowed, trigger=job['trigger'],
                           evidence=job['evidence'], similar_examples=examples)
            if job.get('target_proximity') is not None:
                context['target_proximity'] = job['target_proximity']
            prompt = json.dumps(context, ensure_ascii=False, allow_nan=False)
            trace['prompt_bytes'] = len(prompt.encode())
            reply = child.complete_json(
                system='Evaluate one simulated futures decision. Use only supplied evidence. '
                       'When evidence is insufficient choose hold. For reduce give a fraction '
                       'of remaining quantity. Return concise Turkish JSON. No order is placed.',
                user=prompt, max_tokens=500,
            )
            if reply is not None:
                advice = dict(validate(reply).model_dump(), provider=child.last_provider, model=child.last_model)
            else:
                attempts = getattr(child, 'last_trace', {}).get('attempts', [])
                delay = max((a.get('retry_after_seconds') or 0 for a in attempts), default=0)
                advice = dict(action='unavailable', reason='Uygun sağlayıcı yanıtı alınamadı.',
                              retry_after_seconds=max(30, min(3600, int(delay))))
        except Exception:
            advice = dict(action='unavailable', reason='AI incelemesi tamamlanamadı.')
        finally:
            trace.update(getattr(child, 'last_trace', {}))
            child.close()
        return advice, trace

    histories = {}
    for job in leases:
        history = portfolio.apply(Retrieve(
            kind='retrieve', command_id=job['lease']+'-history', at=int(clock()),
            evidence=Evidence.model_validate(job['evidence']), limit=3,
        ))
        histories[job['lease']] = history['examples']
    reviews = []
    with ThreadPoolExecutor(max_workers=len(leases)) as workers:
        jobs = {workers.submit(evaluate, job, histories[job['lease']]): job for job in leases}
        for future in as_completed(jobs):
            job = jobs[future]
            advice, trace = future.result()
            outcome = portfolio.apply(ReviewResult(
                kind='result', command_id=job['lease']+'-result', at=int(clock()),
                lease=job['lease'], **advice,
            ))
            valid = outcome['status'] == 'recorded' and advice['action'] != 'unavailable'
            trace.update(completed_at=clock(), status='completed' if valid else outcome['status'])
            if valid:
                trace['model_action'] = advice['action']
            archive_trace(portfolio.store, trace)
            reviews.append(dict(subject=job['subject'], **outcome))
    return dict(capacity=capacity, reviews=reviews)
