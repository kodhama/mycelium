---
type: decision
title: "The org index: one helper boundary, graph checks in mycelium"
description: How mycelium reads clusters before the helper ships, where the graph is resolved and validated, and what a failing or vanished cluster leaves in state.
decided: 2026-11-01
ticket: GRO-48
---

# The org index

**mycelium reads clusters only through `mycelium.clusters`, the one module
that runs `kodhama-cluster read --json`.** It follows hub plan U14 and KTD1,
KTD7 and KTD18 (`areas/agent-comms/plans/2026-10-27-feat-mycelium-plan.md`,
revision `b22d0b38`), and the cluster spec (`areas/hq-resume/spec-clusters.md`
at `d2e622f`). mycelium never parses TOML (KTD1, spec §2.4).

**2026-11-01 decision: a stub shape until the helper ships.** The helper
isn't released. The spec says only that `read --json` returns every
registered cluster and its revision. The shape mycelium reads was posted on
GRO-48 and passed to the helper's owner. Each record carries:

- `home` and `status` (`ok`, `missing` or `unreadable`), plus a `reason`;
- `revision`, computed now, and `stored_revision`, the one in the file;
- `instance`, the checkout's id and suffix (§3);
- `declaration`, the `cluster.toml` as data.

When the real helper ships, `clusters.py` is the only module that changes.
Until then the tests run a fake helper, `tests/fake_cluster_helper.py`. Its
fixtures under `tests/fixtures/clusters/` stand in for the helper's shared
set, which doesn't exist yet.

**2026-11-01 decision: mycelium resolves and validates the graph itself**, in
`mycelium.topology`: effective names (§3), `reports_at` and `exclude` (§2.2),
links (§2.1, KTD15) and the §5.1 checks. The spec doesn't say the helper does
this, so mycelium does it from the declarations. If the helper later returns
resolved pointers or validation results, `topology.py` is what that replaces.

- Turned down: waiting for the helper to resolve the graph. U4, U5 and U6
  need the index first.

**2026-11-01 decision: what a cluster's state leaves behind.**

- **A cluster that fails validation keeps its last good entry, marked stale**
  (KTD18). Failing means unreadable TOML, an unknown schema, a hand edit
  (`stored_revision` ≠ `revision`) or any §5.1 error. Its names stay routable.
- **A failing cluster with no last good entry yet** keeps its names routable.
  Their slice says nothing else is known (`known = false`, manager unknown),
  which is not "no manager" (R23). When the cluster's file still parses (a
  hand edit, a graph error), its names and pointers still place the clusters
  around it, which are marked stale through it. When it can't be read at
  all, a pointer to it stays valid. A link to a name no readable cluster
  declares becomes a warning, since it can't be checked. Every orphan's slice
  is marked stale, since the unreadable cluster may be its manager.
- **A registered home that is gone, or holds no `cluster.toml`, is
  `missing`.** Its names leave the clusters and get a hold record. That
  record keeps the last-known manager, taken from the graph before the
  removal (KTD7).
- **A slice is stale when any cluster on its path to the root fails** (R21).
- **A last-good snapshot is kept for every cluster that validates**, not only
  for failing ones. A cluster can fall back only to a snapshot taken while it
  was good. The snapshots also give each hold its last-known manager after a
  cache wipe.

**Where the files live** (R28): the index in `cache/index.json`; snapshots in
`state/index/last-good/`, holds in `state/index/holds.json`, and the rebuild
lock in `state/index/lock`. A rebuild runs under an exclusive `flock`, and
every file is replaced whole. Hold records are written before any snapshot
is removed, so a process killed mid-write can't lose a hold.

- A caller that can't take the lock within a second builds the index in
  memory and writes nothing, so a hook stays inside its timeout.
- The cache records when its helper answer was read. A caller holding an
  older answer uses a newer cache rather than overwrite it.
- Like the store, the index refuses a symlink below the XDG folder.
- A damaged cache or snapshot is rebuilt from the clusters. Damaged hold
  records are reported, never read as "no holds". A rebuild with no clusters and no earlier state
writes nothing (KTD13).

**A known limit:** the index rebuilds when a cluster's registration, status,
reason or revision changes. A move or a new checkout changes one of them. A
symlink retargeted with no other change isn't seen until one does.

**Left to later units:** ending a hold, forwarding its mail and raising
alerts belong to U5 and `sweep`. Org events come from the audit log (KTD2,
U6). A rebuild only returns its changes: names added, names held, managers
changed.

**2026-11-01 entry: the lead's review of `da79b49` (GRO-48 comment
`42b48dd8`).** Four rules change. The entries above stand as the record of
what came first.

- **A graph error above keeps the clusters below where they were.** A
  cluster can fail on a graph check alone, for example a sealed `reports_at`
  typo. A cluster with a snapshot falls back to its last good pointers while
  the graph puts an error on it. It returns to its current declaration once
  that declaration passes against everyone else's final one, so an error that
  a rejected declaration caused on another cluster doesn't outlive it. The
  clusters below keep their last good place and are marked stale through it.
  No snapshot is written from a stale view, and an existing one is kept. When a failing
  cluster has no snapshot, every orphan is marked stale, because the failing
  cluster may be its manager. Before, only an unreadable cluster did this.
  `topology.build(declarations)` is still the module's one entry.
- **Names the last rebuild published are kept in state**, in
  `state/index/published.json`, each with its last-known manager or a flag
  that none is known. A name that leaves gets a hold whether its manager was
  known or not, and a name that returns as "unknown" keeps the manager it
  had. A cache wipe reports nothing as added. A stale slice doesn't move the
  recorded manager, so a manager change made while stale is reported once the
  slice is current. A name a hold has ended for isn't held again from an old
  snapshot. A change says when a manager
  isn't known (`old_known`, `new_known`), so `None` never reads as "no
  manager" (R23). Like hold records, a damaged file is reported, not read as
  empty.
- **A state or cache folder that can't be read, made or written stops the
  rebuild before it writes anything**, with `IndexUnavailable`. A write that
  fails partway also raises `IndexUnavailable`. Holds, then published names,
  are still written first.
- **A NUL in an agent's `cwd`, `reports_at` or `exclude` fails that
  cluster.** The agent stays routable, and the rest of the graph is built.

Turned down, as the review asked: holding every name on an empty helper
answer while state exists is the designed KTD7 outcome. A decision may use a
cache read slightly later than its own answer, and the next refresh corrects
it.
