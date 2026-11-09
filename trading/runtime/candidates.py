"""Durable, bounded candidate rotation independent of provider call cadence."""
from time import time

from trading.quant.signals import CandidateSignal


def candidate_key(candidate):
    key = f'{candidate.symbol}:{candidate.strategy.value}:{candidate.side.value}'
    timeframe = candidate.features.get('seed_timeframe')
    return f'{key}:{timeframe}' if timeframe else key


class CandidateQueue:
    def __init__(self, store, ttl_seconds=900, limit=80):
        self.store,self.ttl,self.limit=store,ttl_seconds,limit

    def refresh(self, candidates, now=None, *, refreshed_symbols=None):
        now=time() if now is None else now
        with self.store.lock:
            previous={row['key']:row for row in self.store.get('candidate_queue',[])}
            touched=set(refreshed_symbols) if refreshed_symbols is not None else None
            current={key:row for key,row in previous.items() if row['expires_at']>now
                and touched is not None and row['signal']['symbol'] not in touched}
            for candidate in candidates:
                key=candidate_key(candidate)
                old=previous.get(key,{})
                closed_at=candidate.features.get('source_closed_at',now)
                max_age = 14400 if candidate.features.get('seed_timeframe') == '4h' else 1200
                if closed_at>now or now-closed_at>max_age:
                    continue
                current[key]=dict(key=key,signal=candidate.model_dump(mode='json'),
                    first_seen=old.get('first_seen',now),refreshed_at=now,
                    expires_at=min(now+self.ttl,closed_at+max_age),
                    last_considered_at=old.get('last_considered_at',0),
                    last_evaluated_at=old.get('last_evaluated_at',0),
                    lease_until=old.get('lease_until',0))
            # Keep the currently strongest candidates if the bounded queue is full;
            # rotation is by last-considered time inside that eligible population.
            rows=sorted(current.values(),key=lambda r:(-r['signal']['confidence'],r['first_seen'],r['key']))[:self.limit]
            self.store.put('candidate_queue',rows)
            return len(rows)

    def next(self, *, now=None, open_symbols=(), seen=(), lease_seconds=0):
        now=time() if now is None else now
        with self.store.lock:
            rows=[r for r in self.store.get('candidate_queue',[]) if r['expires_at']>now]
            self.store.put('candidate_queue',rows)
            eligible=[r for r in rows if r.get('lease_until',0)<=now and r['signal']['symbol'] not in open_symbols and r['signal'].get('features',{}).get('dedup_key') not in seen]
            if not eligible:
                return None
            row=min(eligible,key=lambda r:(not bool(r['signal'].get('features',{}).get('watch_review')),r['last_considered_at'],r['first_seen'],-r['signal']['confidence'],r['key']))
            # Reserve consideration before slow provider work; concurrent scans
            # preserve this timestamp, so a refreshed top signal cannot starve peers.
            row['last_considered_at']=now
            row['lease_until']=now+max(0,lease_seconds)
            self.store.put('candidate_queue',rows)
            return CandidateSignal.model_validate(row['signal'])

    def release(self,candidate):
        with self.store.lock:
            rows=self.store.get('candidate_queue',[])
            for row in rows:
                if row['key']==candidate_key(candidate):
                    row['lease_until']=0
            self.store.put('candidate_queue',rows)

    def is_current(self,candidate,now=None):
        now=time() if now is None else now
        key=candidate_key(candidate)
        with self.store.lock:
            return any(row['key']==key and row['expires_at']>now
                and row['signal'].get('features',{}).get('dedup_key')==candidate.features.get('dedup_key')
                for row in self.store.get('candidate_queue',[]))

    def evaluated(self, candidate, now=None):
        now=time() if now is None else now
        with self.store.lock:
            rows=self.store.get('candidate_queue',[])
            for row in rows:
                if row['key']==candidate_key(candidate):
                    row['last_evaluated_at']=now
            self.store.put('candidate_queue',rows)

    def drop(self, candidate):
        with self.store.lock:
            key=candidate_key(candidate)
            self.store.put('candidate_queue',[r for r in self.store.get('candidate_queue',[]) if r['key']!=key])
