from trading.control.live_protection_audit import inspect, safe_protection


def test_protection_audit_reads_finished_records_and_filters_positions():
    class Client:
        def __init__(self):self.calls=[]
        def protection_orders(self,symbol,*,page_num,page_size,finished):
            self.calls.append((symbol,page_num,page_size,finished))
            return [dict(symbol=symbol,positionId='12',state=2,isFinished=finished,
                lossTrend=1,profitTrend=1,profitLossVolType='SAME',vol='19',
                private_key='SECRET'),dict(symbol=symbol,positionId='999')]
    client=Client()
    groups=inspect(client,'ANTHROPIC_USDT',{'12'})
    assert len(client.calls)==2
    assert [g['finished'] for g in groups]==[0,1]
    assert all(g['complete'] and len(g['records'])==1 for g in groups)
    assert 'stopLossType' in groups[1]['records'][0]['missing_fields']
    assert 'SECRET' not in str(groups)


def test_protection_audit_does_not_print_arbitrary_provider_strings():
    row=safe_protection(dict(id='SECRET',state=True,profitLossVolType='SECRET',vol='19.0'))
    assert 'id' not in row and 'state' not in row
    assert row['vol']=='19.0'
    assert 'SECRET' not in str(row)
