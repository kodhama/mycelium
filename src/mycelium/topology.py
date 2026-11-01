"""The org graph, built and checked from the clusters' declarations.

Covers effective names (cluster spec §3), `reports_at` and `exclude` (§2.2),
links (§2.1, KTD14, KTD15) and validation (§5.1). The input is what the
helper's `read --json` returned (mycelium.clusters); nothing here reads TOML.
Globs are expanded on the filesystem, from the home of the cluster that
declares them.
"""

import glob
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

MAINTAINER = "@maintainer"
RUNS = ("running", "parked", "done")
LINK_KINDS = ("peer", "interested", "escalation")
# Version 1 of mycelium's event vocabulary (KTD14).
EVENTS = ("status", "decision", "done", "blocked", "merge", "tag", "reparent", "question")
GLOB_CHARACTERS = frozenset("*?[")
CLUSTER_FILE = os.path.join(".kodhama", "org", "cluster.toml")


@dataclass(frozen=True)
class Declared:
    """One cluster's declaration, as the graph is built from it."""

    home: str
    """Canonical: symlinks resolved."""
    suffix: str | None
    declaration: Mapping[str, Any]


@dataclass(frozen=True)
class Peer:
    name: str
    topic: str | None


@dataclass(frozen=True)
class Party:
    name: str
    events: tuple[str, ...]


@dataclass(frozen=True)
class Route:
    to: str
    when: tuple[str, ...]


@dataclass(frozen=True)
class Slice:
    """What one agent knows of the org (R16, R21)."""

    name: str
    """The effective name: `<name>-<suffix>` in a suffixed checkout (§3)."""
    declared_name: str
    home: str
    role: str | None
    run: str
    mission: str
    manager: str | None
    """An effective name or `@maintainer`; None for an orphan's head, or when not known."""
    known: bool
    """False when the cluster fails validation and has no last good entry:
    manager, reports and links are then not known, which is not "none" (R23)."""
    orphan: bool
    """The cluster is outside the root's reach (§5.1)."""
    reports: tuple[str, ...] = ()
    peers: tuple[Peer, ...] = ()
    interested: tuple[Party, ...] = ()
    """Who wants this agent's events, and which kinds."""
    follows: tuple[Party, ...] = ()
    """Whose events this agent wants, and which kinds."""
    escalation: tuple[Route, ...] = ()
    stale: str | None = None
    """Why this slice may be out of date (KTD18); None when it is current."""
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "declared_name": self.declared_name,
            "home": self.home,
            "role": self.role,
            "run": self.run,
            "mission": self.mission,
            "manager": self.manager,
            "known": self.known,
            "orphan": self.orphan,
            "reports": list(self.reports),
            "peers": [[peer.name, peer.topic] for peer in self.peers],
            "interested": [[party.name, list(party.events)] for party in self.interested],
            "follows": [[party.name, list(party.events)] for party in self.follows],
            "escalation": [[route.to, list(route.when)] for route in self.escalation],
            "stale": self.stale,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Slice":
        return cls(
            name=data["name"],
            declared_name=data["declared_name"],
            home=data["home"],
            role=data["role"],
            run=data["run"],
            mission=data["mission"],
            manager=data["manager"],
            known=data["known"],
            orphan=data["orphan"],
            reports=tuple(data["reports"]),
            peers=tuple(Peer(name, topic) for name, topic in data["peers"]),
            interested=tuple(Party(name, tuple(events)) for name, events in data["interested"]),
            follows=tuple(Party(name, tuple(events)) for name, events in data["follows"]),
            escalation=tuple(Route(to, tuple(when)) for to, when in data["escalation"]),
            stale=data["stale"],
            warnings=tuple(data["warnings"]),
        )


@dataclass
class Graph:
    slices: dict[str, Slice] = field(default_factory=dict)
    errors: dict[str, list[str]] = field(default_factory=dict)
    """Validation failures, by the home of the cluster each one lands on."""
    parent: dict[str, str] = field(default_factory=dict)
    """Each pointed cluster's home, mapped to the home of the cluster pointing at it."""
    names: dict[str, list[str]] = field(default_factory=dict)
    """Each cluster's effective names, in declaration order."""


@dataclass
class _Agent:
    name: str
    declared: str
    home: str
    role: str | None
    run: str
    mission: str
    manager: str
    cwd: str
    reports_at: list[str]
    exclude: list[str]


def build(declared: Iterable[Declared]) -> Graph:
    """The graph over every declaration, with each validation failure on its cluster."""
    return _Builder(list(declared)).build()


def unknown(cluster: Declared, reason: str) -> list[Slice]:
    """Slices that name a failing cluster's agents and say nothing else is known."""
    slices = []
    for raw in _entries(cluster.declaration, "agent"):
        agent, _ = _agent(raw, cluster)
        if agent is not None:
            slices.append(_slice(agent, manager=None, known=False, orphan=False, stale=reason))
    return slices


def _slice(agent: _Agent, **rest: Any) -> Slice:
    """A slice carrying the agent's own fields, plus what the caller knows of the org."""
    return Slice(
        name=agent.name,
        declared_name=agent.declared,
        home=agent.home,
        role=agent.role,
        run=agent.run,
        mission=agent.mission,
        **rest,
    )


class _Builder:
    def __init__(self, declared: list[Declared]) -> None:
        self.declared = declared
        self.graph = Graph()
        self.agents: dict[str, _Agent] = {}
        self.homes = {cluster.home for cluster in declared}
        self.manager: dict[str, str | None] = {}
        self.owner: dict[str, str] = {}  # pointed cluster's home -> the pointing agent
        self.roots: set[str] = set()
        self.warnings: dict[str, list[str]] = defaultdict(list)
        # (home, declared name) -> effective name, once every agent is read.
        self.local: dict[tuple[str, str], str] = {}
        self.peers: dict[frozenset[str], str] = {}
        self.interested: dict[str, list[Party]] = defaultdict(list)
        self.follows: dict[str, list[Party]] = defaultdict(list)
        self.escalation: dict[str, list[Route]] = defaultdict(list)

    def fail(self, home: str, reason: str) -> None:
        self.graph.errors.setdefault(home, []).append(reason)

    def build(self) -> Graph:
        links = self.read_agents()
        self.in_cluster_managers()
        self.resolve_pointers()
        self.break_cycles()
        for home, heads in self.heads().items():
            for head in heads:
                owner = self.owner.get(home)
                self.manager[head] = owner
        self.check_ancestors()
        self.read_links(links)
        self.make_slices()
        return self.graph

    def read_agents(self) -> list[tuple[str, Any]]:
        links = []
        for cluster in self.declared:
            names = self.graph.names.setdefault(cluster.home, [])
            seen: set[str] = set()
            for raw in _entries(cluster.declaration, "agent"):
                agent, problem = _agent(raw, cluster)
                if agent is None:
                    self.fail(cluster.home, problem or "an agent can't be read")
                    continue
                if agent.declared in seen:
                    self.fail(cluster.home, f"agent {agent.declared} is declared twice")
                    continue
                seen.add(agent.declared)
                if agent.name in self.agents:
                    other = self.agents[agent.name].home
                    reason = f"the effective name {agent.name} is declared by two clusters"
                    self.fail(cluster.home, reason)
                    if other != cluster.home:
                        self.fail(other, reason)
                    continue
                if problem is not None:
                    self.fail(cluster.home, problem)
                self.agents[agent.name] = agent
                self.local[(cluster.home, agent.declared)] = agent.name
                names.append(agent.name)
            links.extend((cluster.home, raw) for raw in _entries(cluster.declaration, "link"))
            for problem in _shape_problems(cluster.declaration):
                self.fail(cluster.home, problem)
        return links

    def in_cluster_managers(self) -> None:
        for agent in self.agents.values():
            if agent.manager == "":
                self.manager[agent.name] = None  # a head: set once pointers resolve
            elif agent.manager == MAINTAINER:
                self.manager[agent.name] = MAINTAINER
                self.roots.add(agent.home)
            elif agent.manager == agent.declared:
                self.fail(agent.home, f"{agent.declared} is its own manager")
                self.manager[agent.name] = None
            elif (agent.home, agent.manager) in self.local:
                self.manager[agent.name] = self.local[(agent.home, agent.manager)]
            else:
                reason = (
                    f"{agent.declared}'s manager {agent.manager} is not an agent in this cluster"
                )
                self.fail(agent.home, reason)
                self.manager[agent.name] = None

    def heads(self) -> dict[str, list[str]]:
        heads: dict[str, list[str]] = defaultdict(list)
        for agent in self.agents.values():
            if agent.manager == "":
                heads[agent.home].append(agent.name)
        return heads

    def resolve_pointers(self) -> None:
        claims: dict[str, list[tuple[str, bool]]] = defaultdict(list)
        for agent in self.agents.values():
            if not agent.reports_at:
                continue
            excluded = set()
            for entry in agent.exclude:
                excluded.update(_expand(agent.home, entry))
            for entry in agent.reports_at:
                if _is_glob(entry):
                    for target in _expand(agent.home, entry):
                        if target not in excluded and target in self.homes:
                            claims[target].append((agent.name, False))
                    continue
                target = os.path.realpath(os.path.join(agent.home, entry))
                if target in excluded:
                    continue
                where = f"{agent.declared}'s reports_at {entry!r}"
                if not os.path.isdir(target):
                    self.fail(agent.home, f"{where} is missing")
                elif not os.path.isfile(os.path.join(target, CLUSTER_FILE)):
                    self.fail(agent.home, f"{where} holds no cluster")
                elif target not in self.homes:
                    self.fail(agent.home, f"{where} holds a cluster that is not registered")
                else:
                    claims[target].append((agent.name, True))
        for target, found in claims.items():
            if any(self.agents[name].home == target for name, _ in found):
                self.fail(target, "the cluster points at itself")
                continue
            explicit = sorted({name for name, is_explicit in found if is_explicit})
            globbed = sorted({name for name, is_explicit in found if not is_explicit})
            if len(explicit) > 1:
                self.fail(target, f"two explicit paths reach it, from {', '.join(explicit)}")
                continue
            if not explicit and len(globbed) > 1:
                self.fail(target, f"two globs reach it, from {', '.join(globbed)}")
                continue
            owner = (explicit or globbed)[0]
            if target in self.roots:
                self.fail(target, f"it holds a root head, but {owner} points at it")
                continue
            self.owner[target] = owner
            self.graph.parent[target] = self.agents[owner].home

    def break_cycles(self) -> None:
        done: set[str] = set()
        for start in list(self.graph.parent):
            path: list[str] = []
            home: str | None = start
            while home is not None and home not in done and home not in path:
                path.append(home)
                home = self.graph.parent.get(home)
            if home is not None and home in path:
                cycle = path[path.index(home) :]
                owners = [self.owner[member] for member in cycle]
                reason = "a cycle of pointers: " + " → ".join([*owners, owners[0]])
                for member in cycle:
                    self.fail(member, reason)
                for member in cycle:
                    self.graph.parent.pop(member, None)
                    self.owner.pop(member, None)
            done.update(path)

    def check_ancestors(self) -> None:
        for agent in self.agents.values():
            seen = {agent.name}
            above = self.manager.get(agent.name)
            while above is not None and above != MAINTAINER:
                if above in seen:
                    self.fail(agent.home, f"{agent.declared}'s managers form a loop")
                    break
                seen.add(above)
                if agent.run == "running" and self.agents[above].run != "running":
                    run = self.agents[above].run
                    reason = (
                        f"{agent.declared} is running under {above}, which is not running ({run})"
                    )
                    self.fail(agent.home, reason)
                    break
                above = self.manager.get(above)

    def matches(self, name: str) -> list[str]:
        """Every effective name a link's `to` names: itself, or each instance of a
        declared name (KTD15)."""
        if name in self.agents:
            return [name]
        return sorted(agent.name for agent in self.agents.values() if agent.declared == name)

    def read_links(self, links: list[tuple[str, Any]]) -> None:
        escalates: set[tuple[str, str]] = set()
        for home, raw in links:
            if not isinstance(raw, dict) or raw.get("kind") not in LINK_KINDS:
                kind = raw.get("kind") if isinstance(raw, dict) else raw
                self.fail(home, f"a link has an unknown kind {kind!r}")
                continue
            kind, source, target = raw["kind"], raw.get("from"), raw.get("to")
            label = f"the {kind} link from {source} to {target}"
            if not _is_name(source) or (home, source) not in self.local:
                self.fail(home, f"{label}: from names no agent in this cluster")
                continue
            if not _is_name(target):
                self.fail(home, f"{label}: to is not a name")
                continue
            source = self.local[(home, source)]
            targets = [MAINTAINER] if target == MAINTAINER else self.matches(target)
            if not targets:
                self.fail(home, f"{label}: no registered cluster declares {target}")
                continue
            if kind == "peer":
                topic = raw.get("topic")
                if not isinstance(topic, str) or not topic:
                    self.fail(home, f"{label} needs a topic")
                    continue
                for other in targets:
                    pair = frozenset((source, other))
                    if pair in self.peers:
                        self.fail(home, f"{label}: that pair already has a peer link")
                        continue
                    self.peers[pair] = topic
            elif kind == "interested":
                events = _words(raw.get("events"))
                if not events:
                    self.fail(home, f"{label} needs events")
                    continue
                strange = [event for event in events if event not in EVENTS]
                if strange:
                    self.warnings[source].append(
                        f"{label} names event kinds mycelium doesn't know: {', '.join(strange)}"
                    )
                for other in targets:
                    self.follows[source].append(Party(other, events))
                    self.interested[other].append(Party(source, events))
            else:
                when = _words(raw.get("when"))
                if not when:
                    self.fail(home, f"{label} needs when")
                    continue
                twice = [value for value in when if (source, value) in escalates]
                if twice:
                    self.fail(home, f"{label}: {source} already escalates {', '.join(twice)}")
                    continue
                escalates.update((source, value) for value in when)
                for other in targets:
                    self.escalation[source].append(Route(other, when))

    def reached(self, home: str) -> bool:
        seen = set()
        while home not in seen:
            if home in self.roots:
                return True
            seen.add(home)
            if home not in self.graph.parent:
                return False
            home = self.graph.parent[home]
        return False

    def make_slices(self) -> None:
        reports: dict[str, list[str]] = defaultdict(list)
        for name, manager in self.manager.items():
            if manager in self.agents:
                reports[manager].append(name)
        for agent in self.agents.values():
            manager = self.manager.get(agent.name)
            near: dict[str, str | None] = {}
            if manager is not None:
                for other, theirs in self.manager.items():
                    if theirs == manager and other != agent.name:
                        near[other] = None
            for pair, topic in self.peers.items():
                if agent.name in pair and len(pair) == 2:
                    (other,) = pair - {agent.name}
                    near[other] = topic
            self.graph.slices[agent.name] = _slice(
                agent,
                manager=manager,
                known=True,
                orphan=not self.reached(agent.home),
                reports=tuple(sorted(reports[agent.name])),
                peers=tuple(Peer(name, near[name]) for name in sorted(near)),
                interested=tuple(sorted(self.interested[agent.name], key=_party_key)),
                follows=tuple(sorted(self.follows[agent.name], key=_party_key)),
                escalation=tuple(self.escalation[agent.name]),
                warnings=tuple(self.warnings[agent.name]),
            )


def _party_key(party: Party) -> tuple[str, tuple[str, ...]]:
    return party.name, party.events


def _agent(raw: Any, cluster: Declared) -> tuple[_Agent | None, str | None]:
    """The agent an `[[agent]]` entry declares, and the first problem with it, if any."""
    if not isinstance(raw, dict) or not _is_name(raw.get("name")):
        return None, "an agent has no name"
    declared = raw["name"]
    if declared.startswith("@"):
        return None, f"agent {declared}: a name can't start with @ (reserved)"
    run = raw.get("run", "running")
    if run not in RUNS:
        return None, f"agent {declared}: run is {run!r}, not one of {', '.join(RUNS)}"
    manager = raw.get("manager", "")
    if not isinstance(manager, str):
        return None, f"agent {declared}: manager is not a string"
    reports_at = raw.get("reports_at", [])
    exclude = raw.get("exclude", [])
    if _words(reports_at) is None or _words(exclude) is None:
        return None, f"agent {declared}: reports_at and exclude must be lists of strings"
    cwd = raw.get("cwd", ".")
    role = raw.get("role")
    mission = raw.get("mission", "")
    agent = _Agent(
        name=f"{declared}-{cluster.suffix}" if cluster.suffix else declared,
        declared=declared,
        home=cluster.home,
        role=role if isinstance(role, str) else None,
        run=run,
        mission=mission if isinstance(mission, str) else "",
        manager=manager,
        cwd=cwd if isinstance(cwd, str) else ".",
        reports_at=list(reports_at),
        exclude=list(exclude),
    )
    if not isinstance(cwd, str) or not _inside(cluster.home, cwd):
        return agent, f"agent {declared}: cwd {cwd!r} leaves the home"
    return agent, None


def _shape_problems(declaration: Mapping[str, Any]) -> list[str]:
    problems = []
    for key in ("agent", "link"):
        if key in declaration and not isinstance(declaration[key], list):
            problems.append(f"{key} is not a list of tables")
    return problems


def _entries(declaration: Mapping[str, Any], key: str) -> list[Any]:
    value = declaration.get(key, [])
    return value if isinstance(value, list) else []


def _words(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _is_name(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_glob(entry: str) -> bool:
    return any(character in GLOB_CHARACTERS for character in entry)


def _expand(home: str, entry: str) -> set[str]:
    """Canonical folders an entry reaches; `root_dir` keeps the home's own name literal."""
    if not _is_glob(entry):
        return {os.path.realpath(os.path.join(home, entry))}
    found = glob.glob(entry, root_dir=home)
    return {
        os.path.realpath(os.path.join(home, match))
        for match in found
        if os.path.isdir(os.path.join(home, match))
    }


def _inside(home: str, cwd: str) -> bool:
    if os.path.isabs(cwd):
        return False
    target = os.path.realpath(os.path.join(home, cwd))
    return os.path.commonpath([home, target]) == home
