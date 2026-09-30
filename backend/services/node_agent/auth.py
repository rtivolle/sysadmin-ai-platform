"""Node identity gate for the node agent.

mTLS design (tranche decided in the architecture spec):
uvicorn does not expose the client certificate to the ASGI scope, so the
application cannot read the peer certificate itself. Authentication therefore
happens at the TLS handshake: `ssl_context.verify_mode = CERT_REQUIRED` with
the fleet CA as the trust anchor makes the handshake itself the membership
check — a node without a fleet-signed certificate cannot reach these
endpoints at all.

What uvicorn *cannot* give us is the authenticated client identity (the
certificate CN). The node name is therefore asserted by the caller in the
JSON body (`node_name`) and cross-checked here:

- the name must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`
- on `POST /api/v1/fleet/nodes/{name}/...`, the path name and the body's
  `node_name` must agree — the node's identity is asserted where it is used,
  and bound again at human approval time (approve records which node name was
  approved).

Honest limit: a party holding a valid fleet certificate could *claim* another
node's name in its heartbeats (there is no cryptographic binding of CN to
`node_name` at this layer). Mitigations in place: approve is human-in-the-loop
and binds the approved name, heartbeats for an unknown or non-approved name
are rejected by the platform, and drain/placement act only on approved nodes.
For the threat model of this prototype (LAN, operator-provisioned hosts) this
is accepted; see docs/gpu-fleet.md.

`NODE_AGENT_AUTH_MODE=disabled` bypasses the check (tests/dev only — never in
production); in `mtls` mode (default) a plain-HTTP request fails closed.
"""
import os
import re

from fastapi import HTTPException, Request

NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

AUTH_MODE_MTLS = "mtls"
AUTH_MODE_DISABLED = "disabled"


def auth_mode() -> str:
    mode = os.getenv("NODE_AGENT_AUTH_MODE", AUTH_MODE_MTLS).strip().lower()
    if mode not in (AUTH_MODE_MTLS, AUTH_MODE_DISABLED):
        raise RuntimeError(f"NODE_AGENT_AUTH_MODE must be 'mtls' or 'disabled', got '{mode}'")
    return mode


def validate_node_name(name: object) -> str:
    if not isinstance(name, str) or not NODE_NAME_RE.match(name):
        raise ValueError("node_name must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    return name


def require_node_identity(request: Request, body_node_name: object = None) -> str:
    """Assert the caller's node identity.

    Fails closed (403) when the auth mode is `mtls` and the request did not
    arrive over TLS — without TLS there was no certificate handshake, so no
    membership was established. Returns the validated node name.

    The handshake itself proves fleet membership (CERT_REQUIRED + fleet CA);
    this function additionally binds the asserted name to the request so the
    platform can bind it again at human-approval time.
    """
    mode = auth_mode()
    if mode == AUTH_MODE_MTLS and request.url.scheme != "https":
        raise HTTPException(
            status_code=403,
            detail="node identity required: this endpoint is only reachable over mTLS",
        )
    if body_node_name is None:
        raise HTTPException(status_code=400, detail="node_name is required")
    try:
        return validate_node_name(body_node_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def require_self(request: Request, path_name: str, body_node_name: object) -> str:
    """A node may only act as itself: the path name and the asserted name must
    match, and the assertion is gated by require_node_identity."""
    node_name = require_node_identity(request, body_node_name)
    try:
        path_name = validate_node_name(path_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if node_name != path_name:
        raise HTTPException(
            status_code=403,
            detail=f"node identity '{node_name}' does not match resource '{path_name}'",
        )
    return node_name
