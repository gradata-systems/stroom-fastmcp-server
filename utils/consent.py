"""Confirmations (key details) and approvals (risky actions) before a write tool acts.

The user is asked directly, as a form, when the client supports elicitation, so the model never holds the
answer:
- MCP 2026-07-28 connections have no server-initiated requests: the tool returns an input-required result
  holding the form, the client asks the user and repeats the call with the answer (SEP-2322). The request
  state, sealed by the framework, names the exact request (and any gates already passed in this call), so
  an answer cannot be replayed for something else.
- Earlier connections: the server sends the elicitation request during the call.
Otherwise the tool returns a pending id with a plain-language summary; the client shows it to the user
and repeats the call with the id once they agree. The id is a sealed token bound to the action, the exact
details and the caller, signed with the request-state keys and expiring, so it cannot be reused for
something else and any replica sharing the keys can verify it.
"""
import base64
import contextvars
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Literal

import mcp_types
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from security.audit import audit, spent_waiting_for_user

logger = logging.getLogger(__name__)
Kind = Literal['confirmation', 'approval']
TTL_SECONDS = 3600


def _user() -> str | None:
    token = get_access_token()
    return (token.claims or {}).get('preferred_username') if token else None


def _digest(action: str, details: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([action, details], sort_keys=True, default=str).encode()).hexdigest()


CAPABILITIES_META_KEY = 'io.modelcontextprotocol/clientCapabilities'


def _modern(ctx: Any) -> bool:
    """A 2026-07-28 connection: no server-initiated requests, input is gathered by repeating the call."""
    check = getattr(ctx, '_is_modern_protocol', None)
    try:
        return bool(check()) if check else False
    except Exception:
        return False


def _client_can_answer_forms(ctx: Any) -> bool:
    """Whether this request's client declared form elicitation (sent per request on modern connections)."""
    meta = getattr(getattr(ctx, 'request_context', None), 'meta', None)
    if meta is not None and not isinstance(meta, dict):
        meta = getattr(meta, 'model_extra', None) or (meta.model_dump(by_alias=True) if hasattr(meta, 'model_dump') else {})
    caps = (meta or {}).get(CAPABILITIES_META_KEY) or {}
    if hasattr(caps, 'model_dump'):
        caps = caps.model_dump(by_alias=True, exclude_none=True)
    return isinstance(caps, dict) and caps.get('elicitation') is not None


def _call_state(ctx: Any) -> dict[str, Any]:
    """This call's consent state: gates granted so far, from the sealed request state of earlier rounds."""
    state = getattr(ctx, '_consent_call_state', None)
    if state is None:
        raw = None
        try:
            raw = ctx.request_state
        except Exception:
            pass
        try:
            state = json.loads(raw) if raw else {}
        except ValueError:
            state = {}
        state.setdefault('granted', [])
        try:
            setattr(ctx, '_consent_call_state', state)
        except Exception:
            pass
    return state


class ConsentStore:
    """Pending ids are self-contained: a sealed token naming the kind, action, details digest, user and expiry,
    signed with the shared request-state keys, so the repeated call may land on any replica. Single use is
    enforced per replica (a token is remembered once spent); the binding to the exact details and the short
    expiry are what keep a replay from doing anything but the same action again."""

    def __init__(self, use_elicitation: bool = True, keys: list[str] | None = None):
        self.use_elicitation = use_elicitation
        self._keys = [k for k in (keys or []) if k] or [secrets.token_hex(32)]
        self._spent: dict[str, float] = {}      # token -> expiry: used (or discarded) on this replica
        self._granted: dict[str, float] = {}    # token -> expiry: already audited as granted (keep rounds)

    # An id is short enough for a small model to pass back exactly: a 281-character id came back from Haiku with one
    # character changed, every time. It packs the expiry, a nonce and a binding to the exact request (kind, action,
    # details digest, user) into 15 bytes, signed with 10 bytes of HMAC, in lower-case base32 (no case or -/_ to
    # confuse): kind-xxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxx, 46 characters.
    @staticmethod
    def _binding(kind: str, action: str, digest: str, user: str | None) -> bytes:
        return hashlib.sha256(json.dumps([kind, action, digest, user]).encode()).digest()[:8]

    @staticmethod
    def _b32(data: bytes) -> str:
        return base64.b32encode(data).decode().rstrip('=').lower()

    def _sign(self, body: str, key: str) -> str:
        return self._b32(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()[:10])

    def _seal(self, kind: str, binding: bytes, expires: int) -> str:
        body = self._b32(expires.to_bytes(4, 'big') + secrets.token_bytes(3) + binding)
        return f"{kind[:4]}-{body}.{self._sign(body, self._keys[0])}"

    def _unseal(self, token: str) -> dict[str, Any] | None:
        """{'expires', 'binding'} of an id this server (any replica sharing the keys) issued, expired or not; None
        when it is not one (not signed by these keys, or altered)."""
        _, _, rest = token.strip().lower().partition('-')
        body, _, signature = rest.rpartition('.')
        if not body or not any(hmac.compare_digest(self._sign(body, key), signature) for key in self._keys):
            return None
        try:
            raw = base64.b32decode(body.upper() + '=' * (-len(body) % 8))
        except ValueError:
            return None
        if len(raw) != 15:
            return None
        return {'expires': int.from_bytes(raw[:4], 'big'), 'binding': raw[7:]}

    def _sweep(self) -> None:
        now = time.time()
        for table in (self._spent, self._granted):
            for token in [t for t, expiry in table.items() if expiry < now]:
                table.pop(token, None)

    async def require(self, ctx: Any, kind: Kind, action: str, summary: str, details: dict[str, Any],
                      token: str | None, keep: bool = False,
                      editable: dict[str, tuple[str, str]] | None = None) -> dict[str, Any] | None:
        """None when the user has agreed; otherwise the response the tool should return.

        Raises ToolError when the user declines or the id does not match this exact request. Ids are single
        use. When a later gate in the same call may still stop it, pass keep=True so the id survives the
        call being repeated for that gate, and discard() it once the action is done.

        editable: values the user may correct in the form itself, {key: (title, proposed)}, e.g. {'name': ('Feed
        name', 'ACME-VPN-V1.0')}; edited(ctx, key, proposed) then gives what they settled on. Without a form (an
        id passed back), the proposal stands: a user who wants another value says so, and the tool is called again.
        """
        editable = editable or {}
        ctx_edits(ctx).clear()
        digest = _digest(action, details)
        if token:
            token = token.strip().lower()   # one id however it is cased, so single use holds
            payload = self._unseal(token)
            if payload is None:
                raise ToolError(f"This {kind} id is not one the server issued: pass it back exactly as it was given "
                                f"(46 characters, e.g. {kind[:4]}-…….……), or request the {kind} again")
            if payload['expires'] <= time.time():
                raise ToolError(f"This {kind} id has expired; request the {kind} again")
            if payload['binding'] != self._binding(kind, action, digest, _user()):
                raise ToolError(f"This {kind} id was issued for a different request; request the {kind} again")
            self._sweep()
            if token in self._spent:
                raise ToolError(f"This {kind} id was already used; request the {kind} again")
            if not keep:
                self._spent[token] = payload['expires']
            if token not in self._granted:
                audit(kind, action=action, details=details, outcome='granted', via='id')
                self._granted[token] = payload['expires']
            return None

        if self.use_elicitation and _modern(ctx):
            outcome = self._form_round(ctx, kind, action, summary, details, digest, editable)
            if outcome is not False:
                return outcome
        elif self.use_elicitation and hasattr(ctx, 'elicit'):
            try:
                asked = time.perf_counter()
                try:
                    answer = await ctx.elicit(f"{summary}\n\n{_format(details)}", _answer_type(kind, editable))
                finally:
                    spent_waiting_for_user(round((time.perf_counter() - asked) * 1000))
            except Exception as e:  # client without elicitation support
                logger.info("Elicitation unavailable, falling back to %s id: %s", kind, e)
            else:
                data = getattr(answer, 'data', None)
                # With a value to edit, accepting the form is the confirmation; otherwise its yes/no is.
                confirmed = True if editable else data
                agreed = getattr(answer, 'action', None) == 'accept' and bool(confirmed)
                edits = _settled(editable, {k: getattr(data, k, None) for k in editable})
                changed = _changed(editable, edits)
                audit(kind, action=action, details=details, outcome='granted' if agreed else 'declined', via='elicitation',
                      **({'edited': changed} if agreed and changed else {}))
                if not agreed:
                    raise ToolError(f"The user did not agree to: {summary}")
                ctx_edits(ctx).update(edits)
                ctx_changes(ctx).update({k: (editable[k][1], v) for k, v in changed.items()})
                # The user's answer as an id, for the same call repeated with what they settled on: if their access
                # token ran out while the form was open (VS Code holds the call until they answer), the call is made
                # again with a fresh one, and they aren't asked twice.
                settled = _with_edits(details, editable, changed)
                agreed_id = self._seal(kind, self._binding(kind, action, _digest(action, settled), _user()),
                                       int(time.time()) + TTL_SECONDS)
                _AGREED.set({'kind': kind, 'action': action, 'id': agreed_id, 'changed': dict(changed)})
                if _token_expired(margin=15):
                    audit(kind, action=action, details=details, outcome='kept for retry', id=agreed_id[-16:])
                    return retry_after_expiry(kind, action, summary)
                return None

        pending_id = self._seal(kind, self._binding(kind, action, digest, _user()), int(time.time()) + TTL_SECONDS)
        audit(kind, action=action, details=details, outcome='requested', id=pending_id[-16:])
        return {'status': f'needs_{kind}', f'{kind}_id': pending_id, 'summary': summary, 'details': details,
                'hint': f"Show the summary and details to the user. If they agree, call {action} again with the "
                        f"same arguments plus {kind}_id='{pending_id}'. If they change a detail, call again with "
                        f"the new values and no id to get a fresh {kind}."}


    async def choose(self, ctx: Any, action: str, question: str, options: list[str]) -> str | None:
        """The option the user picks in a form, or None when the client can't show one here (the agent then asks
        them). Seen: an agent given the choices asked in the chat, on Gemma and on Claude alike, with no picker.
        Cancelling the form stops the call, as declining a confirmation does."""
        if not (self.use_elicitation and hasattr(ctx, 'elicit')) or _modern(ctx) or not options:
            return None
        try:
            asked = time.perf_counter()
            try:
                answer = await ctx.elicit(question, list(options))
            finally:
                spent_waiting_for_user(round((time.perf_counter() - asked) * 1000))
        except Exception as e:  # client without elicitation support
            logger.info("Elicitation unavailable for a choice, left to the agent to ask: %s", e)
            return None
        chosen = getattr(answer, 'data', None) if getattr(answer, 'action', None) == 'accept' else None
        audit('choice', action=action, question=question, outcome='chosen' if chosen else 'declined',
              via='elicitation', **({'choice': chosen} if chosen else {}))
        if not chosen:
            raise ToolError(f"The user made no choice: {question}")
        return chosen if chosen in options else None

    def _form_round(self, ctx: Any, kind: Kind, action: str, summary: str, details: dict[str, Any],
                    digest: str, editable: dict[str, tuple[str, str]] | None = None) -> Any:
        """Modern connections: None if agreed, an input-required result to ask, False if the client cannot."""
        editable = editable or {}
        state = _call_state(ctx)
        bound = f"{digest}:{_user()}"
        if bound in state['granted']:
            # The call is repeated for a later gate: what the user settled on in this one still holds.
            kept = (state.get('edits') or {}).get(bound) or {}
            ctx_edits(ctx).update(kept)
            ctx_changes(ctx).update({k: (editable[k][1], v) for k, v in _changed(editable, kept).items() if k in editable})
            return None
        key = f"{kind}-{digest[:16]}"
        try:
            responses = ctx.input_responses
        except Exception:
            responses = None
        answer = (responses or {}).get(key) if responses else None
        if answer is not None and state.get('asked') == bound:
            action_taken = getattr(answer, 'action', None) or (answer.get('action') if isinstance(answer, dict) else None)
            content = getattr(answer, 'content', None) or (answer.get('content') if isinstance(answer, dict) else None) or {}
            agreed = action_taken == 'accept' and (bool(editable) or bool(content.get('value')))
            edits = _settled(editable, {k: content.get(k) for k in editable})
            changed = _changed(editable, edits)
            audit(kind, action=action, details=details, outcome='granted' if agreed else 'declined', via='form',
                  **({'edited': changed} if agreed and changed else {}))
            if not agreed:
                raise ToolError(f"The user did not agree to: {summary}")
            state['granted'].append(bound)
            if edits:
                state.setdefault('edits', {})[bound] = edits
            state.pop('asked', None)
            ctx_edits(ctx).update(edits)
            ctx_changes(ctx).update({k: (editable[k][1], v) for k, v in changed.items()})
            return None
        if not _client_can_answer_forms(ctx):
            return False
        state['asked'] = bound
        audit(kind, action=action, details=details, outcome='requested', via='form')
        form = mcp_types.ElicitRequest(params=mcp_types.ElicitRequestFormParams(
            message=f"{summary}\n\n{_format(details)}",
            # With a value to edit, the form is just that field, prefilled: accepting it confirms (clients step
            # through fields one at a time, so a yes/no beside it would be a second prompt).
            requested_schema=({'type': 'object', 'properties': {
                key: {'type': 'string', 'title': title, 'default': proposed,
                      'description': f"Proposed: {proposed}. Accept to confirm it, or change it first; left empty, the "
                                     f"proposal stands. Decline to stop."}
                for key, (title, proposed) in editable.items()}} if editable else
                {'type': 'object', 'required': ['value'], 'properties': {
                    'value': {'type': 'boolean', 'title': 'Approve' if kind == 'approval' else 'Confirm',
                              'description': summary}}})))
        return mcp_types.InputRequiredResult(input_requests={key: form},
                                            request_state=json.dumps(state, sort_keys=True))

    def discard(self, token: str | None) -> None:
        """Spend a kept token once the action it covered is done."""
        if token:
            token = token.strip().lower()
            payload = self._unseal(token)
            self._spent[token] = payload['expires'] if payload else time.time() + TTL_SECONDS


# The answer a form gave in this call, kept as an id (see require); read when the access token runs out later on.
_AGREED: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar('consent_agreed', default=None)


def _token_expired(margin: int = 0) -> bool:
    """Whether the caller's access token has run out (or will within margin seconds): None without one."""
    try:
        token = get_access_token()
    except Exception:
        return False
    expires = getattr(token, 'expires_at', None) if token is not None else None
    return bool(expires) and expires - margin <= time.time()


def _with_edits(details: dict[str, Any], editable: dict[str, tuple[str, str]], changed: dict[str, str]) -> dict[str, Any]:
    """The details as the call repeated with the user's values builds them: each proposed value they changed,
    replaced by theirs."""
    swaps = {editable[k][1]: v for k, v in changed.items() if k in editable}
    if not swaps:
        return details
    def swap(value: Any) -> Any:
        if isinstance(value, str):
            return swaps.get(value, value)
        if isinstance(value, dict):
            return {k: swap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [swap(v) for v in value]
        return value
    return swap(details)


def retry_after_expiry(kind: str | None = None, action: str | None = None, summary: str | None = None) -> dict[str, Any]:
    """What to tell the agent when the caller's access token ran out during a call: call again, which the client
    makes with a fresh token; with the user's answer from this call kept, so they aren't asked again."""
    agreed = _AGREED.get()
    keep = ''
    if agreed:
        kind, action = agreed['kind'], agreed['action']
        values = ', '.join(f"{k}='{v}'" for k, v in agreed['changed'].items())
        keep = (f" plus {kind}_id='{agreed['id']}'" + (f", and {values} as the user changed it" if values else '')
                + ": the user already agreed in the form, so they are not asked again")
    return {'status': 'needs_retry', **({f'{kind}_id': agreed['id']} if agreed else {}),
            **({'summary': summary} if summary else {}),
            'hint': (f"The user's sign-in (access token) expired while this call was waiting"
                     f"{' for their answer' if agreed else ''}: nothing was changed in Stroom after that. Call "
                     f"{action or 'the same tool'} again now with the same arguments{keep}. Your client sends a "
                     f"fresh token with every new call, so there is nothing for the user to do; only if that call "
                     f"fails the same way, ask them to sign in again.")}


def ctx_edits(ctx: Any) -> dict[str, str]:
    """The values the user settled on in this request's confirmation forms."""
    edits = getattr(ctx, '_consent_edits', None)
    if edits is None:
        edits = {}
        try:
            setattr(ctx, '_consent_edits', edits)
        except Exception:   # a context that takes no attributes: nothing to carry
            pass
    return edits


def ctx_changes(ctx: Any) -> dict[str, tuple[str, str]]:
    """The values the user changed in this request's forms: {key: (proposed, theirs)}."""
    changes = getattr(ctx, '_consent_changes', None)
    if changes is None:
        changes = {}
        try:
            setattr(ctx, '_consent_changes', changes)
        except Exception:
            pass
    return changes


def edited(ctx: Any, key: str, proposed: str) -> str:
    """What the user settled on for an editable value: their correction in the form, else the proposal."""
    return ctx_edits(ctx).get(key) or proposed


def _settled(editable: dict[str, tuple[str, str]], answered: dict[str, Any]) -> dict[str, str]:
    """Each editable value as answered, or the proposal where the answer is empty."""
    return {k: (str(answered.get(k) or '').strip() or proposed) for k, (_, proposed) in editable.items()}


def _changed(editable: dict[str, tuple[str, str]], edits: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in edits.items() if v != editable[k][1]}


def _answer_type(kind: str, editable: dict[str, tuple[str, str]]) -> Any:
    """The classic elicitation's answer: a yes/no, or with values to edit, just a text field for each, its proposal
    as the default (accepting the form confirms; clients step through fields one at a time)."""
    if not editable:
        return bool
    from dataclasses import field, make_dataclass
    return make_dataclass('Answer', [(key, str, field(default=proposed, metadata={'title': title}))
                                     for key, (title, proposed) in editable.items()])


def _format(details: dict[str, Any]) -> str:
    return '\n'.join(f"- {k}: {v}" for k, v in details.items())


def consent_from(ctx: Any) -> ConsentStore:
    return ctx.lifespan_context['consent']
