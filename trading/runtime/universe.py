"""Persistent alphabetical round-robin coverage, independent of API listing order."""
from bisect import bisect_right
from time import time


def select_universe(store,symbols,limit):
    universe=sorted(set(str(s).strip().upper() for s in symbols if str(s).strip()))
    if not universe:return []
    with store.lock:
        previous=store.get('universe_rotation',{})
        start=bisect_right(universe,previous.get('last_symbol',''))%len(universe)
        count=min(max(1,limit),len(universe))
        selected=[universe[(start+i)%len(universe)] for i in range(count)]
        store.put('universe_rotation',dict(last_symbol=selected[-1],total_symbols=len(universe),
            selected=selected,selected_count=count,at=time(),mode='persistent_round_robin'))
        return selected
