"""Process-shared public MEXC pacing and bounded immutable response cache.

Never touches authenticated/private requests. 510/429 cools down all public
clients on the same host instead of starting per-symbol retry storms.
"""
import copy
import threading
import time
from urllib.parse import urlsplit


class PublicDataDeferred(RuntimeError):
    def __init__(self,seconds):
        super().__init__('public_market_cooldown')
        self.retry_after_seconds=max(0,seconds)


class PublicGovernor:
    def __init__(self,*,rate=3.,clock=time.monotonic,sleep=time.sleep):
        self.rate,self.clock,self.sleep=rate,clock,sleep
        self.lock=threading.RLock();self.next_at=0.;self.until=0.;self.cache={}

    def acquire(self):
        with self.lock:
            now=self.clock()
            if self.until>now:raise PublicDataDeferred(self.until-now)
            at=max(now,self.next_at)
            if at-now>20:raise PublicDataDeferred(at-now)
            self.next_at=at+1/self.rate
        if at>now:self.sleep(at-now)
        with self.lock:
            if self.until>self.clock():raise PublicDataDeferred(self.until-self.clock())

    def penalize(self,seconds=30.):
        with self.lock:self.until=max(self.until,self.clock()+max(1,seconds))

    def get(self,key):
        with self.lock:
            row=self.cache.get(key)
            if row and row[0]>self.clock():return copy.deepcopy(row[1])
            self.cache.pop(key,None)
            return None

    def put(self,key,value,ttl):
        if ttl<=0:return
        with self.lock:
            if len(self.cache)>=256:
                oldest=min(self.cache,key=lambda key:self.cache[key][0]);self.cache.pop(oldest)
            self.cache[key]=(self.clock()+ttl,copy.deepcopy(value))

    def snapshot(self):
        with self.lock:return dict(requests_per_second=self.rate,cooldown_seconds=max(0,self.until-self.clock()))


_GOVERNORS={}
_LOCK=threading.Lock()


def shared_governor(base_url):
    host=urlsplit(base_url).netloc.lower()
    with _LOCK:return _GOVERNORS.setdefault(host,PublicGovernor())
