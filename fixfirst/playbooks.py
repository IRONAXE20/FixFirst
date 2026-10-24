"""Troubleshooting playbooks: decision trees stored as data (knowledge/playbooks/*.yaml).

The agent speaks node scripts verbatim and the LLM only picks one of the current node's
outcomes; `PlaybookRun.advance` rejects anything else. This is what keeps every spoken
instruction inside reviewed content.
"""
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .config import KNOWLEDGE_DIR, now_utc
from .safety import HAZARD_KINDS

TERMINAL_KINDS = ("resolve", "escalate", "followup")


class PlaybookError(ValueError):
    pass


@dataclass
class Outcome:
    id: str
    label: str
    next: str | None = None
    hazard: str | None = None


@dataclass
class Node:
    id: str
    kind: str
    say: dict
    help: dict = field(default_factory=dict)
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    resolution: str | None = None
    likely_cause: str | None = None
    technician_note: str | None = None
    followup_minutes: int | None = None
    sources: list[str] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS

    def text(self, language: str) -> str:
        return " ".join((self.say.get("hi" if language == "hi" else "en") or "").split())

    def help_text(self, language: str) -> str:
        return " ".join((self.help.get("hi" if language == "hi" else "en") or "").split())


@dataclass
class Playbook:
    id: str
    appliance: str
    title: dict
    status: str
    entry: str
    nodes: dict[str, Node]
    triage_node: str | None = None
    routes: dict[str, str] = field(default_factory=dict)
    reviewed_by: str | None = None

    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise PlaybookError(f"{self.id}: unknown node '{node_id}'") from None

    @property
    def symptoms(self) -> dict[str, str]:
        """Triage outcomes: what the LLM can jump straight to when the caller already said it."""
        if not self.triage_node:
            return {}
        return {o.id: o.label for o in self.node(self.triage_node).outcomes.values()}

    def to_dict(self) -> dict:
        return {
            "id": self.id, "appliance": self.appliance, "title": self.title, "status": self.status,
            "reviewed_by": self.reviewed_by, "entry": self.entry, "triage_node": self.triage_node,
            "routes": self.routes,
            "nodes": [{
                "id": n.id, "kind": n.kind, "say": n.say, "help": n.help, "sources": n.sources,
                "likely_cause": n.likely_cause, "technician_note": n.technician_note,
                "resolution": n.resolution, "followup_minutes": n.followup_minutes,
                "outcomes": [{"id": o.id, "label": o.label, "next": o.next, "hazard": o.hazard}
                             for o in n.outcomes.values()],
            } for n in self.nodes.values()],
        }


def _parse(raw: dict) -> Playbook:
    nodes = {}
    for node_id, n in raw["nodes"].items():
        outcomes = {oid: Outcome(id=oid, label=o.get("label", oid), next=o.get("next"), hazard=o.get("hazard"))
                    for oid, o in (n.get("outcomes") or {}).items()}
        nodes[node_id] = Node(
            id=node_id, kind=n.get("kind", "step"), say=n.get("say") or {}, help=n.get("help") or {},
            outcomes=outcomes, resolution=n.get("resolution"), likely_cause=n.get("likely_cause"),
            technician_note=n.get("technician_note"), followup_minutes=n.get("followup_minutes"),
            sources=n.get("sources") or [])
    return Playbook(id=raw["id"], appliance=raw["appliance"], title=raw.get("title") or {},
                    status=raw.get("status", "draft-unreviewed"), entry=raw["entry"], nodes=nodes,
                    triage_node=raw.get("triage_node"), routes=raw.get("routes") or {},
                    reviewed_by=raw.get("reviewed_by"))


def validate(pb: Playbook, source_ids: set[str]) -> list[str]:
    errors = []
    if pb.entry not in pb.nodes:
        errors.append(f"entry '{pb.entry}' missing")
    if pb.triage_node and pb.triage_node not in pb.nodes:
        errors.append(f"triage_node '{pb.triage_node}' missing")
    for route, target in pb.routes.items():
        if target not in pb.nodes:
            errors.append(f"route '{route}' -> missing node '{target}'")
    for n in pb.nodes.values():
        where = f"node '{n.id}'"
        if n.kind not in ("step", "question") + TERMINAL_KINDS:
            errors.append(f"{where}: unknown kind '{n.kind}'")
        if (not n.terminal or n.kind != "escalate") and not (n.say.get("en") and n.say.get("hi")):
            errors.append(f"{where}: needs say.en and say.hi")
        if n.say and not (n.say.get("en") and n.say.get("hi")):
            errors.append(f"{where}: say must have both en and hi")
        if n.help and not (n.help.get("en") and n.help.get("hi")):
            errors.append(f"{where}: help must have both en and hi")
        if n.terminal and n.outcomes:
            errors.append(f"{where}: terminal nodes can't have outcomes")
        if not n.terminal and not n.outcomes:
            errors.append(f"{where}: needs at least one outcome")
        if n.kind == "escalate" and not n.likely_cause:
            errors.append(f"{where}: escalate needs likely_cause")
        if n.kind == "followup" and not n.followup_minutes:
            errors.append(f"{where}: followup needs followup_minutes")
        for o in n.outcomes.values():
            if not isinstance(o.id, str):
                errors.append(f"{where}: outcome id {o.id!r} isn't text (YAML reads yes/no/on/off as booleans; rename it)")
            if bool(o.next) == bool(o.hazard):
                errors.append(f"{where}: outcome '{o.id}' needs exactly one of next/hazard")
            if o.next and o.next not in pb.nodes:
                errors.append(f"{where}: outcome '{o.id}' -> missing node '{o.next}'")
            if o.hazard and o.hazard not in HAZARD_KINDS:
                errors.append(f"{where}: outcome '{o.id}' -> unknown hazard '{o.hazard}'")
        for s in n.sources:
            if s not in source_ids:
                errors.append(f"{where}: unknown source '{s}'")
    reachable, stack = set(), [pb.entry, *pb.routes.values()]
    while stack:
        nid = stack.pop()
        if nid in reachable or nid not in pb.nodes:
            continue
        reachable.add(nid)
        stack.extend(o.next for o in pb.nodes[nid].outcomes.values() if o.next)
    for nid in pb.nodes:
        if nid not in reachable:
            errors.append(f"node '{nid}' is unreachable")
    return [f"{pb.id}: {e}" for e in errors]


class PlaybookLibrary:
    def __init__(self, root: Path | None = None):
        root = root or KNOWLEDGE_DIR
        self.sources: dict = yaml.safe_load((root / "sources.yaml").read_text(encoding="utf-8")) or {}
        self.playbooks: dict[str, Playbook] = {}
        for path in sorted((root / "playbooks").glob("*.yaml")):
            pb = _parse(yaml.safe_load(path.read_text(encoding="utf-8")))
            self.playbooks[pb.id] = pb
        self.errors = [e for pb in self.playbooks.values() for e in validate(pb, set(self.sources))]
        if self.errors:
            raise PlaybookError("Invalid playbooks:\n  " + "\n  ".join(self.errors))
        self.error_codes: dict = {}
        for path in sorted((root / "errorcodes").glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.error_codes[raw["appliance"]] = raw["brands"]

    def for_appliance(self, appliance: str) -> Playbook | None:
        return next((pb for pb in self.playbooks.values() if pb.appliance == appliance), None)

    def get(self, playbook_id: str) -> Playbook:
        return self.playbooks[playbook_id]

    def lookup_code(self, appliance: str, brand: str, code: str) -> dict | None:
        """Map a spoken brand + code to a documented meaning and route, or None if unknown."""
        brands = self.error_codes.get(appliance) or {}
        b = brand.strip().lower()
        brand_id = next((bid for bid, info in brands.items()
                         if b == bid or b in info.get("aliases", []) or bid in b.split()), None)
        if not brand_id:
            return None
        info = brands[brand_id]
        for candidate in _code_variants(code):
            entry = info["codes"].get(candidate)
            if entry:
                return {"brand": brand_id, "code": candidate, "meaning": entry["meaning"],
                        "route": entry["route"], "source": entry.get("source", info.get("source"))}
        return None


def _code_variants(code: str) -> list[str]:
    c = "".join(ch for ch in code.upper() if ch.isalnum())
    variants = [c, c.replace("0", "O"), c.replace("O", "0")]
    return list(dict.fromkeys(v for v in variants if v))


@dataclass
class Advance:
    node: Node | None = None
    hazard: str | None = None


class PlaybookRun:
    """One customer's walk through a playbook."""

    def __init__(self, playbook: Playbook):
        self.playbook = playbook
        self.current = playbook.entry
        self.path: list[dict] = []
        self.pending_symptom: str | None = None

    @property
    def node(self) -> Node:
        return self.playbook.node(self.current)

    @property
    def finished(self) -> bool:
        return self.node.terminal

    def start(self, symptom: str | None = None) -> Node:
        """Begin at the entry node; a symptom the caller already described skips the triage question."""
        if symptom and symptom in self.playbook.symptoms:
            self.pending_symptom = symptom
            if self.current == self.playbook.triage_node:
                return self._auto_triage()
        return self.node

    def advance(self, outcome: str, caller_said: str | None = None) -> Advance:
        """Take an outcome of the current node. `caller_said` marks a step answered from the caller's
        earlier words instead of being asked; the quote goes into the path for ops and the technician."""
        node = self.node
        if node.terminal:
            raise PlaybookError(f"'{node.id}' is already finished")
        if outcome not in node.outcomes:
            raise PlaybookError(f"'{outcome}' is not a valid answer at '{node.id}'; "
                                f"valid: {', '.join(node.outcomes)}")
        o = node.outcomes[outcome]
        entry = {"node": node.id, "outcome": outcome, "label": o.label, "at": now_utc().isoformat()}
        if caller_said:
            entry.update(label=f'{o.label} (answered earlier: "{caller_said}")', caller_said=caller_said, skipped=True)
        self.path.append(entry)
        if o.hazard:
            return Advance(hazard=o.hazard)
        self.current = o.next
        if self.current == self.playbook.triage_node and self.pending_symptom:
            return Advance(node=self._auto_triage())
        return Advance(node=self.node)

    def jump(self, route: str, reason: str) -> Node:
        """Move to a declared route (used by error-code lookups); anything else is rejected."""
        if route not in self.playbook.routes:
            raise PlaybookError(f"{self.playbook.id} has no route '{route}'")
        self.path.append({"node": self.current, "outcome": f"route:{route}", "label": reason,
                          "at": now_utc().isoformat()})
        self.current = self.playbook.routes[route]
        return self.node

    def jump_to(self, node_id: str, reason: str) -> Node:
        """Move to a terminal node of this playbook (e.g. caller asks for a technician)."""
        target = self.playbook.node(node_id)
        if not target.terminal:
            raise PlaybookError(f"can only jump to a terminal node, not '{node_id}'")
        self.path.append({"node": self.current, "outcome": "jump", "label": reason, "at": now_utc().isoformat()})
        self.current = node_id
        return target

    def _auto_triage(self) -> Node:
        symptom, self.pending_symptom = self.pending_symptom, None
        result = self.advance(symptom)
        return result.node or self.node

    def steps_tried(self) -> list[dict]:
        return [p for p in self.path if not p["outcome"].startswith("route:") and p["outcome"] != "jump"]


@lru_cache
def get_library() -> PlaybookLibrary:
    return PlaybookLibrary()
