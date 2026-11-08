"""Run a Runner 2 exit-plan replay from a JSON scenario. No exchange client."""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from trading.runtime.runner2 import Opening, Runner2ExitReplay, events_adapter
from trading.runtime.runner2_portfolio import Runner2Portfolio, commands_adapter
from trading.runtime.store import RuntimeStore


def replay_scenario(scenario):
    replay = Runner2ExitReplay(Opening.model_validate(scenario['opening']))
    for event in events_adapter.validate_python(scenario.get('events', [])):
        replay.apply(event)
    return replay.snapshot()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', type=Path)
    parser.add_argument('--portfolio-db', type=Path, help='Dedicated Runner 2 SHADOW journal')
    parser.add_argument('--account', default='shadow')
    parser.add_argument('--compare', type=Path, help='Matched exit scenario with the same opening and costs')
    parser.add_argument('--ai-reviews', action='store_true', help='Run one real free-provider text batch for current SHADOW jobs')
    args = parser.parse_args()
    try:
        scenario = json.loads(args.scenario.read_text())
        if args.portfolio_db is None:
            if args.ai_reviews:
                raise ValueError('ai-reviews requires a SHADOW portfolio database')
            result = replay_scenario(scenario)
            if args.compare:
                other = replay_scenario(json.loads(args.compare.read_text()))
                if any(result['opening'][k] != other['opening'][k] for k in result['opening'] if k != 'plan'):
                    raise ValueError('comparison_requires_matching_opening_and_costs')
                metric = 'realized_net_before_funding_usdt'
                delta = None
                if all(Decimal(row['remaining_contracts']) == 0 and metric in row['performance']
                       for row in (result, other)):
                    delta = str(Decimal(other['performance'][metric]) - Decimal(result['performance'][metric]))
                result = dict(mode='SHADOW', first=result, second=other, second_minus_first_net_usdt=delta)
        else:
            if args.compare:
                raise ValueError('compare requires exit replay scenarios')
            if not args.portfolio_db.name.startswith('runner2-shadow-'):
                raise ValueError('portfolio database filename must start with runner2-shadow-')
            commands = commands_adapter.validate_python(scenario['commands'])
            store = RuntimeStore(str(args.portfolio_db))
            try:
                portfolio = Runner2Portfolio(store, args.account)
                outcomes = [portfolio.apply(command) for command in commands]
                batch = None
                if args.ai_reviews:
                    from trading.agents.provider_pool import ProviderPool
                    from trading.config import get_settings
                    from trading.runtime.runner2_reviews import run_review_batch

                    cfg = get_settings()
                    settings = cfg.model_copy(update=dict(
                        ai_claude_fallback_enabled=False,
                        ai_primary_provider='groq' if cfg.ai_primary_provider == 'anthropic' else cfg.ai_primary_provider,
                    ))
                    quota_store = RuntimeStore(settings.shared_budget_db_path)
                    try:
                        pool = ProviderPool(settings, None, quota_store)
                        try:
                            batch = run_review_batch(portfolio, pool, ceiling=settings.ai_parallel_candidates)
                        finally:
                            pool.close()
                    finally:
                        quota_store.db.close()
                result = dict(portfolio.snapshot(), results=outcomes)
                if batch is not None:
                    result['ai_batch'] = batch
            finally:
                store.db.close()
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(2, f'Runner 2: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
