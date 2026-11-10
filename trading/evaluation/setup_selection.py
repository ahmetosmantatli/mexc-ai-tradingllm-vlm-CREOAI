"""Grounded same-symbol alternatives; never accept an arbitrary model direction."""
from trading.models.domain import Side
from trading.quant.signals import CandidateSignal,StrategyName


def _positive(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and value>0


def awaiting_touch(option):
    """A zone that has not been touched is not an entry."""
    if not isinstance(option,dict):
        return False
    evidence=option.get('evidence') if isinstance(option.get('evidence'),dict) else {}
    return evidence.get('stage')=='awaiting_touch'


def closed_structure_card(option):
    """Closed CHoCH at a block or flow, with a stop. Awaiting touch is not this card."""
    if not isinstance(option,dict) or awaiting_touch(option):
        return False
    evidence=option.get('evidence') if isinstance(option.get('evidence'),dict) else {}
    zone=evidence.get('zone') if isinstance(evidence.get('zone'),dict) else {}
    shift=evidence.get('shift') if isinstance(evidence.get('shift'),dict) else {}
    if zone.get('kind') not in ('block','flow') or shift.get('kind') not in ('choch','mss'):
        return False
    return _positive(option.get('stop_loss_price', option.get('stop')))


def selected_candidate(source,setup):
    if not setup:return source
    clone=CandidateSignal.model_validate(source.model_dump())
    clone.side=Side(setup['side'])
    clone.strategy=StrategyName.STRUCTURE_SETUP
    clone.features.update(setup_id=setup['setup_id'],setup_family=setup['family'],
        setup_state=setup['state'],structure_seed=True)
    # These are hypothesis levels; a TRADE must still supply its own validated
    # current-market price plan. WATCH never sends an order from these levels.
    clone.stop_loss_price=setup['stop_loss_price']
    clone.take_profit_price=setup['take_profit_price']
    return clone


def validate_selection(decision,source,options,enabled):
    if source.features.get('structure_seed') and not enabled and decision.action in ('trade','watch'):
        raise ValueError('structure seed requires explicit setup selection')
    ident=decision.setup_id
    if enabled and decision.action=='trade' and ident is None:
        raise ValueError('structure seed requires explicit setup selection')
    if ident is None:
        if enabled and source.features.get('structure_seed') and decision.action in ('trade','watch'):
            raise ValueError('structure seed requires explicit setup selection')
        return source,None
    option=next((row for row in options if row['setup_id']==ident),None) if enabled else None
    if option is None or decision.action not in ('trade','watch','hold'):
        raise ValueError('unknown or inapplicable setup selection')
    if decision.action=='hold':
        return source,option
    if decision.action=='trade' and (option['state']!='ready' or option.get('requires_full_context') or decision.price_plan is None):
        # A parseable trade on a card that already has a closed CHoCH, a block or
        # flow, and a stop is kept. The caller grounds the plan. Awaiting touch
        # is not this card. Incomplete cards still fail closed.
        if not closed_structure_card(option):
            raise ValueError('selected setup requires confirmed full context and fresh price plan')
    return selected_candidate(source,option),option
