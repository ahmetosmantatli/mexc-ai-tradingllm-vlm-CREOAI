"""Account/binding/revision-scoped automatic LIVE policy; disabled by default.

Starting this policy is an explicit operator grant, not a fabricated per-order
human approval. Risk/price/account/native-protection gates remain mandatory.
"""
from dataclasses import dataclass
from time import time
from math import isfinite

from trading.brokers.live.execution import ApprovedLiveExecutor,LiveExecutionError


@dataclass(frozen=True)
class AutomaticLivePolicy:
    enabled: bool = False
    allowed_accounts: tuple[str,...] = ()
    operator_ids: tuple[str,...] = ()
    max_positions: int = 2


class AutomaticLiveExecutor(ApprovedLiveExecutor):
    def __init__(self,policy,gates,store,private,public,risk_engine,*,control_provider,**kwargs):
        super().__init__(gates,store,private,public,risk_engine,**kwargs)
        self.policy,self.control_provider=policy,control_provider

    def _grant_key(self):return 'live:automatic_grant:'+self.account_id

    def _policy_gate(self):
        if self.policy.enabled is not True or self.account_id not in self.policy.allowed_accounts:
            raise LiveExecutionError('automatic_live_policy_disabled')

    def grant_start(self,operator_id,control_revision,*,ttl_seconds=604800):
        self._gate()
        self._policy_gate()
        if str(operator_id) not in {str(value) for value in self.policy.operator_ids}:
            raise LiveExecutionError('automatic_start_operator_not_allowed')
        if not 5<=ttl_seconds<=604800:raise LiveExecutionError('automatic_grant_ttl_invalid')
        if (not isinstance(control_revision,(int,float,str)) or isinstance(control_revision,bool)
                or isinstance(control_revision,(int,float)) and not isfinite(control_revision)
                or control_revision==''):
            raise LiveExecutionError('automatic_control_revision_required')
        control=self.control_provider()
        if not control.get('enabled') or control.get('updated_at')!=control_revision:
            raise LiveExecutionError('automatic_start_control_changed')
        grant=dict(account_id=self.account_id,binding_id=self.binding_id,control_revision=control_revision,
            granted_by=str(operator_id),expires_at=time()+ttl_seconds,created_at=time(),revoked=False)
        self.store.put(self._grant_key(),grant)
        return grant

    def revoke_start(self):
        grant=self.store.get(self._grant_key(),{})
        grant.update(revoked=True,revoked_at=time())
        self.store.put(self._grant_key(),grant)

    def _current_grant(self):
        self._policy_gate()
        grant=self.store.get(self._grant_key(),{})
        control=self.control_provider()
        if (grant.get('account_id')!=self.account_id or grant.get('binding_id')!=self.binding_id
                or grant.get('revoked') or grant.get('expires_at',0)<=time()
                or not control.get('enabled') or control.get('stopped')
                or control.get('updated_at')!=grant.get('control_revision')):
            raise LiveExecutionError('automatic_start_grant_not_current')
        return grant

    def entry_ready(self):
        try:
            self._gate()
            self._current_grant()
            return True
        except LiveExecutionError:
            return False

    def authorize(self,proposal,*,control_revision,ttl_seconds=60):
        self._gate()
        grant=self._current_grant()
        if grant['control_revision']!=control_revision:raise LiveExecutionError('automatic_control_revision_changed')
        row=self.stage(proposal,ttl_seconds=ttl_seconds)
        with self.store.lock:
            row=self.state(row['intent_id'])
            if row.get('entry_write_attempted'):return row
            if row['status'] not in ('awaiting_manual_approval','policy_authorized'):
                raise LiveExecutionError('automatic_intent_state_invalid')
            row.update(status='policy_authorized',authorization_type='automatic_start_grant',
                policy_grant_created_at=grant['created_at'],policy_control_revision=control_revision,
                allow_emergency_close=True,policy_authorized_at=time())
            return self._save(row)

    def approve(self,*args,**kwargs):
        raise LiveExecutionError('automatic_policy_never_fabricates_manual_approval')

    def _authorized(self,row):
        grant=self._current_grant()
        return (row.get('status')=='policy_authorized' and row.get('authorization_type')=='automatic_start_grant'
            and row.get('policy_grant_created_at')==grant['created_at']
            and row.get('policy_control_revision')==grant['control_revision'])

    def _managed(self):
        rows=[row for row in self.intents() if row.get('status')=='protected' and row.get('position_id')]
        return rows

    def _preflight(self,row):
        self._current_grant()
        if len(self._managed())>=self.policy.max_positions:raise LiveExecutionError('automatic_position_cap')
        return super()._preflight(row)
