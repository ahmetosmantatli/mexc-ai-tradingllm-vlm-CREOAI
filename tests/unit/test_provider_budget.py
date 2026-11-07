from types import SimpleNamespace

import pytest

from trading.agents.anthropic_client import AnthropicClient
from trading.config import Settings
from trading.runtime.budget import Budget
from trading.runtime.store import RuntimeStore


class Rejected(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        self.body = {'error': {'message': 'rejected'}}


def client(tmp_path, *, error=None, raw='{"ok":true}', can_call=None):
    cfg = Settings(_env_file=None, anthropic_api_key='test')
    store = RuntimeStore(str(tmp_path/'budget.sqlite3'))
    budget = Budget(store, cfg)
    requests = []
    def count(**kwargs):
        requests.append('count')
        return SimpleNamespace(input_tokens=100)
    def create(**kwargs):
        requests.append('create')
        if error is not None:
            raise error
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=10),
                               content=[SimpleNamespace(text=raw)])
    llm = AnthropicClient(settings=cfg, budget=budget, can_call=can_call)
    llm._client = SimpleNamespace(messages=SimpleNamespace(count_tokens=count, create=create))
    return llm, store, requests


@pytest.mark.parametrize('status', [400, 401, 403, 429])
def test_definitive_rejections_release_pending_spend(tmp_path, status):
    llm, store, calls = client(tmp_path, error=Rejected(status))
    assert llm.complete_json(system='JSON', user='test') is None
    state = store.get('ai_budget')
    assert state['total'] == pytest.approx(0)
    assert state['reservations'] == {}
    assert state['successful_calls'] == 0


@pytest.mark.parametrize('error', [TimeoutError(), Rejected(408), Rejected(499), Rejected(500)])
def test_uncertain_failures_retain_separate_pending_reservation(tmp_path, error):
    llm, store, calls = client(tmp_path, error=error)
    assert llm.complete_json(system='JSON', user='test') is None
    state = store.get('ai_budget')
    assert len(state['reservations']) == 1
    assert state['successful_calls'] == 0


def test_invalid_json_does_not_erase_completed_usage(tmp_path):
    llm, store, calls = client(tmp_path, raw='not json')
    assert llm.complete_json(system='JSON', user='test') is None
    state = store.get('ai_budget')
    assert state['reservations'] == {}
    assert state['successful_calls'] == 1
    assert state['input_tokens'] == 100
    assert state['total'] > 0


def test_stop_before_count_makes_no_provider_calls(tmp_path):
    llm, store, calls = client(tmp_path, can_call=lambda:False)
    assert llm.complete_json(system='JSON', user='test') is None
    assert calls == []
    assert llm.last_status == 'stopped'
    assert store.get('ai_budget') is None


def test_stop_after_count_releases_reservation_before_generation(tmp_path):
    checks = iter([True, True, False])
    llm, store, calls = client(tmp_path, can_call=lambda:next(checks))
    assert llm.complete_json(system='JSON', user='test') is None
    assert calls == ['count']
    assert store.get('ai_budget')['total'] == pytest.approx(0)
    assert store.get('ai_budget')['reservations'] == {}
    assert llm.last_status == 'stopped'


def test_release_is_idempotent_and_preserves_settled_usage(tmp_path):
    llm, store, _ = client(tmp_path)
    settled = llm.budget.reserve(.01)
    llm.budget.settle(settled, 100, 10)
    pending = llm.budget.reserve(.02)
    assert llm.budget.release(pending)
    assert not llm.budget.release(pending)
    assert not llm.budget.release(settled)
    state = store.get('ai_budget')
    assert state['total'] == pytest.approx(.00045)
    assert sum(state['days'].values()) == pytest.approx(.00045)
