from types import SimpleNamespace
import json
from trading.control import Account, TelegramController
from trading.control.connections import ConnectionFlow
from trading.runtime.store import RuntimeStore
from trading.runtime.paper import PaperLedger
from trading.models.domain import Side
from trading.agents.graph import size_proposal_from_signal
from trading.quant.signals import CandidateSignal, StrategyName
from trading.exchange.contracts import ContractRules


def test_private_link_delete_and_owner_disconnect(tmp_path):
    ctl=TelegramController('fake',[Account('a',1,1,'','')],RuntimeStore(str(tmp_path/'control')))
    sent=[]; calls=[]; linked=[]; removed=[]
    ctl.send=lambda ident,text,**kw:sent.append((ident,text))
    ctl.call=lambda method,**kw:calls.append((method,kw))
    flow=ConnectionFlow(ctl,object(),lambda *a:linked.append(a),lambda a:removed.append(a))
    def update(text,user=1):
        return {'message':{'message_id':3,'chat':{'id':user,'type':'private'},'from':{'id':user},'text':text}}
    assert not flow.handle(update('/connect',2))
    assert flow.handle(update('/connect'))
    assert flow.handle(update(json.dumps({'api_key':'private-key','api_secret':'private-secret'})))
    assert linked==[('a','private-key','private-secret')]
    assert any(method=='deleteMessage' for method,_ in calls)
    assert not any('private-key' in text or 'private-secret' in text for _,text in sent)
    assert flow.handle(update('/disconnect'))
    assert removed==['a'] and not ctl.control('a')['enabled']


def test_manual_close_books_pnl_and_rejects_stale_position(tmp_path):
    book=PaperLedger(RuntimeStore(str(tmp_path/'ledger')),500)
    signal=CandidateSignal(symbol='BTC_USDT',side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)
    proposal=size_proposal_from_signal(signal,equity=500,risk_per_trade_pct=.01,target_leverage=3)
    book.open(proposal,ContractRules(.001,1,1,1e8,5,.005,.01),99.99,100.01,'key',.01,.02)
    stamp=book.positions['BTC_USDT']['opened_at']
    import pytest
    with pytest.raises(ValueError):book.close_manual('BTC_USDT',102,102.01,stamp+1)
    book.close_manual('BTC_USDT',102,102.01,stamp)
    assert book.state['events'][-1]['reason']=='MANUAL'
    assert book.equity>500 and not book.positions
