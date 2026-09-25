# ADR-0006: Local PBKDF2 password login instead of LDAP/OIDC/PAM IdP integration

- **Date:** 2026-09-24
- **Status:** Proposed — owner acceptance pending
- **Source of divergence:** `docs/specs/00` ("Authentification SSO (LDAP / OIDC / PAM)"); `docs/specs/02` ("Traefik (TLS + Auth LDAP)"); `docs/plans/DEVELOPMENT_PLAN.md` §3 (S6: "Integrate an existing on-prem IdP") and §4 ("Use the existing on-prem OIDC provider if present. If only LDAP exists, choose and validate a local adapter in AUTH-01").

## Context

The specs and plan route authentication through an existing enterprise IdP —
LDAP/OIDC/PAM — with Traefik ForwardAuth delegating to a validated auth adapter.
Identity must derive from the authenticated session, never a client header.

The implementation does **not** integrate any IdP. The auth gateway
(`backend/services/auth_gateway/server.py`) authenticates against locally
provisioned credentials: per-user random bearer keys in `backend/config/keys/*.key`
and PBKDF2-SHA256 (600,000 iterations, constant-time compare) login hashes in
`login-credentials.json`, issuing a 256-bit session cookie in Valkey (24 h TTL).
There is no LDAP, OIDC, PAM, or external IdP adapter. ForwardAuth still strips
and re-derives identity, so the *identity-integrity* property holds; what is
missing is the enterprise identity *source*.

## Decision

For the prototype, authenticate ten fixed sysadmin accounts (plus
`emergency-p1-oncall`) from locally provisioned bearer keys and PBKDF2 login
hashes, with sessions in Valkey. Defer IdP integration until an on-prem IdP is
available.

## Consequences

- **Positive:** No dependency on an external IdP; the prototype runs hermetically.
  The credential-derived-identity invariant is preserved end to end.
- **Negative:** User lifecycle (add/remove/disable users, group/role sync,
  password policy, SSO) is manual; roles are hard-coded from the username. The
  plan's S6 correction (validated IdP adapter) is unfulfilled. Login cookies are
  `secure=False` for the loopback HTTP prototype (recorded in
  `docs/security.md` §9).

## Evidence

- **Local credential login is tested:**
  `backend/tests/tier1_unit/test_auth_login.py::test_login_routes_use_provisioned_hashes`.
- **Identity integrity (no header trust) is tested:**
  `backend/tests/tier3_concurrency/test_empirical_challenger.py::test_forwardauth_identity_spoofing_prevented`,
  `::test_forwardauth_spoofed_x_user_rejected`,
  `::test_forwardauth_spoofed_x_forwarded_user_rejected`,
  `::test_traefik_header_stripping_and_protection`;
  `backend/tests/tier3_concurrency/test_approval_http.py::test_agent_router_ignores_forwarded_identity`.
- **No qualifying test for LDAP/OIDC/PAM** — no IdP adapter exists to test. This
  is an unimplemented plan correction, not a tested contract.

## Corrective work

- Implement and validate an IdP adapter (AUTH-01) when an on-prem IdP/LDAP is
  available, or record the owner's decision that local credentials are the
  permanent identity model for these ten accounts.
- Enable TLS and mark the login cookie `secure=True` before any non-loopback
  exposure (tracked in `docs/security.md` §9).
