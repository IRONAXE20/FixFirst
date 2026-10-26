"""Playbooks in the database (Supabase in production), so the service team can edit them on /knowledge
without a deploy. Every save is checked by the same validator as the YAML files (both languages, every
answer leads somewhere, every step reachable, sources exist) and kept as a numbered version.

knowledge/playbooks/*.yaml seed an empty table, add playbooks the table doesn't have yet, and are the
export format (`python -m fixfirst playbooks export`). Once a playbook is in the table, the table wins.
"""
from pathlib import Path

import yaml
from loguru import logger
from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from .config import KNOWLEDGE_DIR, now_utc
from .db import playbook_docs, playbook_history
from .playbooks import Playbook, PlaybookError, PlaybookLibrary, _parse, validate


def file_docs(root: Path | None = None) -> dict[str, dict]:
    out = {}
    for path in sorted(((root or KNOWLEDGE_DIR) / "playbooks").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        out[raw["id"]] = raw
    return out


def check(library: PlaybookLibrary, raw: dict) -> Playbook:
    """Parse and validate one playbook document; raises PlaybookError listing every problem."""
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("nodes"):
        raise PlaybookError("A playbook needs at least `id`, `appliance`, `entry` and `nodes`.")
    try:
        pb = _parse(raw)
    except (KeyError, TypeError, AttributeError) as e:
        raise PlaybookError(f"Malformed playbook: missing or wrong field {e}") from e
    errors = validate(pb, set(library.sources))
    if errors:
        raise PlaybookError("\n".join(errors))
    return pb


def _write(c, raw: dict, version: int, by: str | None, exists: bool) -> None:
    values = {"appliance": raw["appliance"], "status": raw.get("status", "draft-unreviewed"), "version": version,
              "doc": raw, "updated_at": now_utc(), "updated_by": by}
    if exists:
        c.execute(update(playbook_docs).where(playbook_docs.c.id == raw["id"]).values(**values))
    else:
        c.execute(insert(playbook_docs).values(id=raw["id"], **values))
    c.execute(insert(playbook_history).values(playbook_id=raw["id"], version=version, doc=raw,
                                              saved_at=now_utc(), saved_by=by))


def sync(engine: Engine, library: PlaybookLibrary) -> None:
    """At startup: put the stored playbooks into the in-memory library, seeding missing ones from the files."""
    with engine.begin() as c:
        stored = {r.id: r for r in c.execute(select(playbook_docs))}
        for pid, raw in file_docs().items():
            if pid not in stored:
                _write(c, raw, 1, "seed from knowledge/playbooks", exists=False)
                logger.info(f"Playbook {pid} added to the database from its YAML file")
    for pid, r in stored.items():
        try:
            pb = check(library, r.doc)
        except PlaybookError as e:   # never serve a broken playbook: keep the file version
            logger.error(f"Stored playbook {pid} v{r.version} is invalid, using the file version: {e}")
            continue
        for old_id, old in list(library.playbooks.items()):
            if old.appliance == pb.appliance and old_id != pb.id:   # one playbook per appliance
                library.playbooks.pop(old_id)
        library.playbooks[pid] = pb


def versions(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {r.id: r.version for r in c.execute(select(playbook_docs.c.id, playbook_docs.c.version))}


def source(engine: Engine, playbook_id: str) -> tuple[str, int] | None:
    """The stored document as YAML text, and its version."""
    with engine.connect() as c:
        r = c.execute(select(playbook_docs.c.doc, playbook_docs.c.version).where(playbook_docs.c.id == playbook_id)).first()
    if not r:
        return None
    return yaml.safe_dump(r.doc, allow_unicode=True, sort_keys=False, width=110), r.version


def history(engine: Engine, playbook_id: str) -> list[dict]:
    with engine.connect() as c:
        rows = c.execute(select(playbook_history.c.version, playbook_history.c.saved_at, playbook_history.c.saved_by)
                         .where(playbook_history.c.playbook_id == playbook_id)
                         .order_by(playbook_history.c.version.desc()).limit(50))
        return [{"version": r.version, "saved_at": r.saved_at.isoformat(), "saved_by": r.saved_by} for r in rows]


def save(engine: Engine, library: PlaybookLibrary, text: str, *, playbook_id: str, by: str | None,
         base_version: int | None = None) -> int:
    """Validate YAML text and store it as the next version. Returns the new version number."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PlaybookError(f"Not valid YAML: {e}") from e
    pb = check(library, raw)
    if pb.id != playbook_id:
        raise PlaybookError(f"The document's id is '{pb.id}', but you are editing '{playbook_id}'.")
    other = next((p for p in library.playbooks.values() if p.appliance == pb.appliance and p.id != pb.id), None)
    if other:
        raise PlaybookError(f"Appliance '{pb.appliance}' already has playbook '{other.id}'.")
    with engine.begin() as c:
        current = c.execute(select(playbook_docs.c.version).where(playbook_docs.c.id == pb.id)).scalar()
        if base_version is not None and current is not None and current != base_version:
            raise PlaybookError(f"Someone saved version {current} while you were editing version {base_version}. "
                                "Reload to see their changes, then edit again.")
        version = (current or 0) + 1
        _write(c, raw, version, by, exists=current is not None)
    library.playbooks[pb.id] = pb
    logger.info(f"Playbook {pb.id} saved as v{version} by {by or 'unknown'}")
    return version


def import_files(engine: Engine, library: PlaybookLibrary, by: str = "import from knowledge/playbooks") -> list[str]:
    """Store every YAML file as a new version (overrides edits made on /knowledge), and retire stored
    playbooks that no longer have a file (their versions stay in the history)."""
    files = file_docs()
    with engine.begin() as c:
        gone = [r.id for r in c.execute(select(playbook_docs.c.id)) if r.id not in files]
        if gone:
            c.execute(delete(playbook_docs).where(playbook_docs.c.id.in_(gone)))
    for pid in gone:
        library.playbooks.pop(pid, None)
        logger.info(f"Playbook {pid} retired from the database (no YAML file any more)")
    saved = []
    for pid, raw in files.items():
        save(engine, library, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), playbook_id=pid, by=by)
        saved.append(pid)
    return saved


def export_files(engine: Engine, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with engine.connect() as c:
        for r in c.execute(select(playbook_docs.c.id, playbook_docs.c.appliance, playbook_docs.c.doc)):
            path = out_dir / f"{r.appliance}.yaml"
            path.write_text(yaml.safe_dump(r.doc, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
            written.append(path)
    return written
