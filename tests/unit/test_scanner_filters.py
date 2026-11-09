"""Market scanner liquidity filters (fixture-backed public client)."""

from __future__ import annotations

from trading.exchange.mexc_public import MexcPublicClient, TickerSnapshot
from trading.quant.scanner import LiquidityFilter, MarketScanner
from trading.quant.strategies import TrendStrategy


def _rising_klines(start: float = 100.0, n: int = 40, step: float = 0.5) -> list[list[float]]:
    out: list[list[float]] = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        out.append([i, o, c + 0.1, o - 0.1, c, 1000.0])
        price = c
    return out


def test_liquidity_filter_rejects_low_volume_and_wide_spread() -> None:
    filt = LiquidityFilter(min_quote_volume_24h=1_000_000, max_spread_bps=10)
    low_vol = TickerSnapshot(
        symbol="ILLIQ_USDT",
        last_price=1.0,
        bid1=1.0,
        ask1=1.001,
        quote_volume_24h=100.0,
    )
    ok, reason = filt.passes(low_vol)
    assert not ok
    assert reason == "LOW_QUOTE_VOLUME"

    wide = TickerSnapshot(
        symbol="WIDE_USDT",
        last_price=1.0,
        bid1=1.0,
        ask1=1.05,
        quote_volume_24h=5_000_000.0,
    )
    ok, reason = filt.passes(wide)
    assert not ok
    assert reason == "WIDE_SPREAD"


def test_scanner_filters_symbols_and_emits_candidates() -> None:
    client = MexcPublicClient(
        use_fixtures=True,
        fixture_symbols=["BTC_USDT", "JUNK_USDT", "ETH_USDT"],
        fixture_fee_free=["BTC_USDT"],
        fixture_tickers={
            "BTC_USDT": TickerSnapshot(
                symbol="BTC_USDT",
                last_price=120.0,
                bid1=119.99,
                ask1=120.01,
                quote_volume_24h=10_000_000.0,
                fee_free=True,
            ),
            "JUNK_USDT": TickerSnapshot(
                symbol="JUNK_USDT",
                last_price=1.0,
                bid1=1.0,
                ask1=1.0,
                quote_volume_24h=10.0,
            ),
            "ETH_USDT": TickerSnapshot(
                symbol="ETH_USDT",
                last_price=120.0,
                bid1=119.9,
                ask1=120.1,
                quote_volume_24h=8_000_000.0,
            ),
        },
        fixture_klines={
            "BTC_USDT": _rising_klines(),
            "ETH_USDT": _rising_klines(start=200.0),
            "JUNK_USDT": _rising_klines(start=1.0),
        },
    )
    scanner = MarketScanner(
        client=client,
        liquidity=LiquidityFilter(min_quote_volume_24h=500_000, max_spread_bps=50),
        strategies=(TrendStrategy(fast_window=5, slow_window=15),),
        prefer_fee_free=True,
        max_symbols=10,
    )
    result = scanner.scan()
    assert result.scanned == 3
    assert result.kept == 2
    assert "JUNK_USDT" in result.filtered_out
    assert result.filtered_out["JUNK_USDT"] == "LOW_QUOTE_VOLUME"
    assert any(c.symbol == "BTC_USDT" for c in result.candidates)
    assert all(c.stop_loss_price > 0 for c in result.candidates)
    assert all(0.0 <= c.confidence <= 1.0 for c in result.candidates)


def test_scan_records_safe_http_category_not_response_body():
    import httpx
    client=MexcPublicClient(use_fixtures=True,fixture_symbols=['BTC_USDT'])
    def rejected(symbol):
        response=httpx.Response(429,request=httpx.Request('GET','https://example.test'),text='PRIVATE RESPONSE')
        response.raise_for_status()
    client.get_ticker=rejected
    result=MarketScanner(client).scan()
    assert result.errors=={'BTC_USDT':'HTTP_429'}
    assert 'PRIVATE' not in str(result)


def test_runner2_swing_scan_does_not_need_a_15m_signal(monkeypatch):
    from types import SimpleNamespace

    now=1440000
    monkeypatch.setattr('trading.quant.scanner.time.time',lambda:now)
    ticker=TickerSnapshot(symbol='BTC_USDT',last_price=101,bid1=100.99,ask1=101.01,quote_volume_24h=10_000_000)
    calls=[]

    def klines(symbol,interval,limit):
        calls.append(interval)
        seconds={'Hour4':14400,'Min5':300,'Min15':900}[interval]
        rows=[[now-seconds*(35-i),98.,99.,96.,98.,1.] for i in range(35)]
        if interval=='Min15':
            rows=[]
        if interval=='Hour4':
            rows[-11][2]=112.
            rows[-7][2]=100.
            rows[-4][3]=94.
            rows[-1][1:5]=[99.,102.,98.,101.]
        return dict(symbol=symbol,interval=interval,candles=rows)

    client=SimpleNamespace(list_contract_symbols=lambda:['BTC_USDT'],list_fee_free_candidates=lambda:[],
        get_all_tickers=lambda:[ticker],get_klines=klines)
    scanner=MarketScanner(client,strategies=(),bulk_tickers=True,structure_setups_enabled=True,multihorizon=True)
    scan=scanner.scan()
    assert set(calls)=={'Min5','Min15','Hour4'}
    assert [c.features.get('seed_timeframe') for c in scan.candidates]==['4h']
    assert scan.candidates[0].features['source_closed_at']==now
