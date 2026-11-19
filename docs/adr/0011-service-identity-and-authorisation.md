# ADR 0011: Service identity with signed tokens, permissions decided by the receiver

## Context
Until Slice 4, anything on the Compose network could call anything: an agent could
call `remember` on the memory server, a compromised news agent could fetch prices or
ask the LLM gateway for unlimited tokens, and the gateway's ledger recorded whatever
`caller` name a request claimed. Slice 4 adds a broker that changes trading state, so
"who is calling, and may they?" must have a real answer at every receiver.

## Options
| Option | How it works | Why not (here) |
|---|---|---|
| Network policy only | Only listed containers can reach a port | Coarse (all of a service or nothing); no identity in logs; Compose has no per-port policy |
| Shared API key per receiver | Callers send a static secret | A leaked key is valid forever and names no caller; every caller shares it |
| mTLS (service mesh) | Each service has a certificate; the mesh checks both ends | The production answer; needs a CA, rotation and a mesh. Heavy for Compose |
| OAuth client credentials | An authorisation server issues scoped tokens | Also production-grade; adds a token service and a network hop to every first call |
| **Self-signed JWTs + receiver policy** | Each service signs its own short-lived token; each receiver checks it against one policy file | Chosen |

## Decision
1. **Identity: one Ed25519 key pair per service** (`uv run desk-keygen`, keys in the
   untracked `.keys/`). A container mounts its own private key and everyone's public
   keys. Every outgoing request carries a fresh JWT: `iss = sub = me`, `aud = the
   receiver`, 60-second life. Receivers refuse a token with an unknown signer, a bad
   signature, another audience, an expiry, or a lifetime over 300 s.
2. **Permission: decided by the receiver, from `config/access.toml`.** The token says
   who you are; only the receiver says what that allows. A caller cannot grant itself a
   scope. One file holds the whole matrix, so it can be reviewed in one sitting.
3. **Enforced at every receiver, in the protocol each already speaks:**
   * MCP servers use the SDK's own resource-server auth (`token_verifier`). Bad tokens
     get a 401 before any tool runs, and each tool asks for its scope: the news agent
     holds `news:read` and gets a refusal if it asks for prices.
   * A2A agents keep their Agent Card public (discovery) and require `agent:call`, held
     by the orchestrator alone, to send work.
   * The LLM gateway needs `llm:chat` and writes the *proven* caller to the ledger. A
     service may still name a role (`debate-agent/bull-agent`); the identity part can't
     be forged.
4. **Fail-closed in containers, open in unit tests.** `AUTH_REQUIRED=1` on every
   container: a missing key or policy section stops the service at startup. Without it
   the receiver runs open and logs that it is open.
5. **Asymmetric keys, not a shared secret.** A receiver holds only public keys, so a
   compromised service cannot mint tokens as any other.

## Consequences
- One stolen private key lets an attacker act as that service, within its scopes, until
  the key is rotated (`desk-keygen --rotate NAME`, restart). That's the same blast radius
  as the service itself, which is the point.
- No revocation list: a token lives at most 60 s, so revoking a key means rotating it.
- No replay cache: a captured token is reusable against the same receiver for its
  remaining seconds. Acceptable on a private network with 60 s tokens; TLS would close it.
- Traffic is still plain HTTP inside the Compose network. Signatures prove who sent a
  request, not that nobody read it. mTLS is the next step if this ever leaves one host.
- Redis (the event bus) has no auth yet: a container on the network could publish a
  `brief.requested` event. The effect is bounded (it can only request research, which is
  idempotent and rate-limited), but it is the remaining unauthenticated path.
- What would change this: more than one host, or a second team's services. Then mTLS or
  an OAuth server pays for itself, and `access.toml` becomes their policy input.
