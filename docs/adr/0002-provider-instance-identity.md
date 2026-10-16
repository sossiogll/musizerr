# ADR-0002: Provider type vs provider instance

**Status:** Proposed — awaiting decision
**Date:** 2026-09-18
**Deciders:** @sossiogll (maintainer)

## Context

Today a provider's identity is its `name`. Everything that belongs to a provider is
keyed on that single string:

- credentials, in `provider_credentials`, keyed `(provider, credential_type, user_id)`;
- configuration, now in `settings`, keyed `provider.<name>.<short_key>`;
- identifiers, `Identifier(type="provider_id", value=..., provider=<name>)`;
- registration, where `ProviderRegistry` maps a name to one object per category.

That conflates two different things: the **type** of provider (how to talk to a
Navidrome, a catalog service) and the **instance** (which server, which account, in which role).

Three consequences are observable in the running system, not hypothetical.

**1. One server, two roles, one account.** `NavidromeProvider` is a single class
registered as both a metadata provider and a sync provider. The providers listing
reports it with `also_registered_in`, and the settings UI renders its editor once for
exactly this reason. But it has one credential row and one configuration row, so
browsing the library and writing sync state necessarily use the same account. The
owner raised this directly: he may want a read-only account for metadata and a
separate account for sync, as an ordinary least-privilege policy.

**2. Two different providers sharing a name.** The metadata provider and the download
provider of one catalog service are distinct classes both registered under the same
name. They therefore share one configuration row. Today their short keys happen not to collide except for
`client_id` and `client_secret`, which are plausibly meant to be shared, so nothing is
visibly broken. But the merged schema deduplicates by key: 20 settings are declared by
providers and 18 survive. A future key declared by both with different meanings would
silently overwrite.

**3. One server per type.** Two Navidrome servers, or two Lidarr instances, cannot be
configured at all. Nothing in the contract forbids it; the key space does.

The settings refactor has already introduced the seams this would need: a namespaced
key space, a declared schema per provider, identity reported by object rather than by
name in the providers listing, and one settings store with a cascade.

## Decision

**None yet.** This records the options and their costs so the decision can be made
deliberately rather than by accident, because every further feature keyed on `name`
raises the cost of changing it.

## Options considered

### A. Do nothing

Keep name as identity. Document the constraint: one account and one configuration per
provider type, and never declare a colliding key in two providers sharing a name.

*Cost:* the owner's least-privilege case stays impossible. The dedup stays a silent
trap. Multiple instances stay impossible.

*Effort:* none. *Reversibility:* full.

### B. Qualify identity by role

Make identity `(name, category)`. The Navidrome metadata role and the Navidrome sync
role become separate keys, so they can hold separate credentials and configuration.

*Solves:* case 1 and case 2, since the two providers of that catalog service differ by category.

*Does not solve:* case 3, two servers of the same type in the same role.

*Cost:* every stored key changes. A migration must rewrite credential rows and
`provider.<name>.*` settings keys, and must decide what a pre-existing shared row
becomes: copied into both roles, or assigned to one. Shared values, such as an OAuth
client used by both providers of that catalog service, would be duplicated and could then drift.
`Identifier.provider` also carries a bare name and is persisted in `track_identifiers`
across thousands of rows; either it keeps meaning "type", which is probably right since
an identifier identifies content rather than a connection, or it migrates too.

*Effort:* moderate. *Reversibility:* possible but a second migration.

### C. First-class instances

Introduce an explicit instance: a stable id, a type, a label, and the set of roles it
serves. Credentials and configuration key on the instance id. The registry maps a type
to a factory, and instances are created by the user.

*Solves:* all three cases, and makes the settings UI honest, since a card is an
instance rather than a class.

*Cost:* the largest. It changes the registry, the credential store, the settings key
space, the providers listing, the settings UI, and the external provider contract,
because an installed package today registers a class under a name. It also raises
questions this ADR does not answer: what a provider reference such as
`sync_provider` points at, presumably an instance id rather than a name; what happens
to `Identifier.provider`; and how an instance is named in the interface.

*Effort:* large, and it touches real user data. *Reversibility:* poor.

### D. Per-role credentials only

Leave identity alone; allow credentials, and only credentials, to be overridden per
role, falling back to the type's credentials when no override exists.

*Solves:* the owner's least-privilege case, which is the only one he has actually
asked for.

*Does not solve:* cases 2 and 3.

*Cost:* small and additive. A nullable role column on the credential row, and a
resolution rule. No key space change, no rewrite of existing rows, and existing
deployments keep working untouched.

*Effort:* small. *Reversibility:* good.

## Trade-off analysis

The three problems have very different weights. The least-privilege case is a real
request with a small, additive fix. The name collision is a latent trap that costs
nothing today and is contained by a conformance test. Multiple instances of one type is
a feature nobody has asked for.

Option C is the architecturally correct answer and the only one that solves all three,
but it is a large migration of live data for two problems that are currently theoretical
and one that option D solves for a fraction of the cost. Option B sits in between and
solves two, but it still rewrites every stored key, and its duplication of shared
credentials creates a drift problem that does not exist today.

## Recommendation

Take **D** now if the least-privilege case matters, because it is additive, cheap and
reversible, and leave the key space alone. Add a conformance test that fails when two
providers sharing a name declare the same short key, which turns the silent trap in
case 2 into a build failure. Revisit **C** only when someone genuinely needs two servers
of the same type, at which point the instance concept pays for itself instead of being
speculative.

Do not take **B**: it pays most of C's migration cost for less than C's benefit.

## Consequences

Whatever is chosen, two things should be recorded in the code rather than only here:
the assumption that a provider name identifies a type and not a connection, and the
merged schema's deduplication, which is currently the only thing standing between a
name collision and silent data loss.

## Action items

- [ ] Owner decides between A, C and D.
- [ ] Regardless of the decision: add the conformance test for colliding short keys
      across providers sharing a name.
- [ ] If D: design the role fallback rule and its migration, and decide whether the
      settings UI exposes a per-role account or keeps one account with an override.
