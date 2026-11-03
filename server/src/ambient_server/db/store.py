"""L1 store: all SQLite access in one place. No LLM, no embeddings computed here —
callers pass vectors in; this module owns rows, FTS sync, vector index, provenance."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from ..config import AppConfig
from ..models import MemoryOut, UtteranceEvent, iso, parse_iso, utc_now
from .connection import Database
from .vector_index import make_memory_index, make_utterance_index


def new_id() -> str:
    return uuid4().hex


@dataclass
class SearchHit:
    row: Any  # sqlite3.Row from memories
    semantic: float = 0.0        # cosine from vector index (0 if unavailable)
    keyword: float = 0.0         # bm25 normalized within candidate set
    entity_hit: bool = False
    temporal_hit: bool = False
    score: float = 0.0           # attached by callers (query profile, etc.)


def _fts_match_text(text: str) -> str:
    toks = re.findall(r"\w+", text, flags=re.UNICODE)
    if not toks:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in toks[:32])


class Store:
    def __init__(self, db: Database, cfg: AppConfig, embed_dim: int):
        self.db = db
        self.cfg = cfg
        self.memory_vectors = make_memory_index(db, max(embed_dim, 1))
        self.utterance_vectors = make_utterance_index(db, max(embed_dim, 1))

    # ------------------------------------------------------------- speakers
    def get_speaker(self, speaker_id: str):
        return self.db.query_one("SELECT * FROM speakers WHERE id=?", (speaker_id,))

    def upsert_speaker_from_utterance(
        self, speaker_id: str, label: str, now: datetime, is_owner_label: bool = False
    ) -> None:
        existing = self.get_speaker(speaker_id)
        if existing is None:
            self.db.execute(
                "INSERT INTO speakers (id,label,name,is_owner,status,first_seen,last_seen,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    speaker_id, label,
                    label if label not in ("Owner",) and not label.startswith("Speaker-") else None,
                    1 if (is_owner_label or label == "Owner") else 0,
                    "named" if label != "Owner" and not label.startswith("Speaker-") else "provisional",
                    iso(now), iso(now), iso(now), iso(now),
                ),
            )
        else:
            self.db.execute(
                "UPDATE speakers SET label=?, last_seen=?, updated_at=? "
                "WHERE id=? AND (label IS NULL OR label<>?)",
                (label, iso(now), iso(now), speaker_id, label),
            )

    def list_speakers(self) -> list[dict]:
        rows = self.db.query(
            """SELECT s.*, COUNT(u.id) AS utterance_count
               FROM speakers s LEFT JOIN utterances u ON u.speaker_id = s.id
               WHERE s.merged_into IS NULL
               GROUP BY s.id ORDER BY s.last_seen DESC"""
        )
        return [dict(r) for r in rows]

    def rename_speaker(self, speaker_id: str, name: str) -> bool:
        cur = self.db.execute(
            "UPDATE speakers SET name=?, label=?, status='named', updated_at=? WHERE id=?",
            (name, name, iso(utc_now()), speaker_id),
        )
        if cur.rowcount:
            # memories/speakers can show the new name; re-resolve via query path
            self.kv_bump("speakers_version")
        return cur.rowcount > 0

    def set_owner(self, speaker_id: str) -> None:
        self.db.execute("UPDATE speakers SET is_owner=0 WHERE is_owner=1")
        self.db.execute(
            "UPDATE speakers SET is_owner=1, updated_at=? WHERE id=?", (iso(utc_now()), speaker_id)
        )

    def merge_speakers(self, from_id: str, into_id: str) -> bool:
        if from_id == into_id:
            return False
        with self.db.transaction() as conn:
            conn.execute("UPDATE utterances SET speaker_id=? WHERE speaker_id=?", (into_id, from_id))
            conn.execute("UPDATE memories SET speaker_id=? WHERE speaker_id=?", (into_id, from_id))
            conn.execute("UPDATE clarifications SET related_speaker_id=? WHERE related_speaker_id=?",
                         (into_id, from_id))
            src = conn.execute("SELECT * FROM speakers WHERE id=?", (from_id,)).fetchone()
            if src is None:
                return False
            conn.execute(
                "UPDATE speakers SET status='merged', merged_into=?, updated_at=? WHERE id=?",
                (into_id, iso(utc_now()), from_id),
            )
            tgt = conn.execute("SELECT * FROM speakers WHERE id=?", (into_id,)).fetchone()
            if tgt:
                first = min(x for x in (src["first_seen"], tgt["first_seen"]) if x)
                last = max(x for x in (src["last_seen"], tgt["last_seen"]) if x)
                conn.execute(
                    "UPDATE speakers SET first_seen=?, last_seen=? WHERE id=?", (first, last, into_id)
                )
        return True

    def delete_speaker(self, speaker_id: str) -> bool:
        """Cascade: memories attributed to the speaker are deleted (with vectors/FTS);
        utterances are kept with speaker nulled (transcript is the record)."""
        mem_ids = [r["id"] for r in self.db.query(
            "SELECT id FROM memories WHERE speaker_id=?", (speaker_id,))]
        for mid in mem_ids:
            self.delete_memory(mid)
        self.db.execute("UPDATE utterances SET speaker_id=NULL WHERE speaker_id=?", (speaker_id,))
        self.db.execute("UPDATE clarifications SET related_speaker_id=NULL "
                        "WHERE related_speaker_id=?", (speaker_id,))
        # merged twins point at this row via FK; they carry no data — remove them
        self.db.execute("DELETE FROM speakers WHERE merged_into=?", (speaker_id,))
        cur = self.db.execute("DELETE FROM speakers WHERE id=?", (speaker_id,))
        return cur.rowcount > 0

    def speaker_by_label(self, label: str):
        return self.db.query_one(
            "SELECT * FROM speakers WHERE merged_into IS NULL AND (label=? OR name=? OR label LIKE ?)",
            (label, label, label.lower()),
        )

    # ------------------------------------------------------------- utterances / conversations
    def create_conversation(self, started_at: datetime, conversation_id: str | None = None) -> str:
        cid = conversation_id or new_id()
        self.db.execute(
            "INSERT OR IGNORE INTO conversations (id, started_at, last_utterance_at, created_at) "
            "VALUES (?,?,?,?)",
            (cid, iso(started_at), iso(started_at), iso(utc_now())),
        )
        return cid

    def get_conversation(self, conversation_id: str):
        return self.db.query_one("SELECT * FROM conversations WHERE id=?", (conversation_id,))

    def open_conversations(self):
        return self.db.query("SELECT * FROM conversations WHERE closed_at IS NULL")

    def close_conversation(self, conversation_id: str, reason: str, closed_at: datetime) -> None:
        self.db.execute(
            "UPDATE conversations SET closed_at=?, close_reason=? WHERE id=? AND closed_at IS NULL",
            (iso(closed_at), reason, conversation_id),
        )

    def sweep_due_closes(self, now: datetime) -> list[tuple[str, str]]:
        """Conversations past the silence gap → close. Returns (id, reason)."""
        cutoff = now - timedelta(seconds=self.cfg.conversations.silence_gap_s)
        rows = self.db.query(
            "SELECT id FROM conversations WHERE closed_at IS NULL AND last_utterance_at < ?",
            (iso(cutoff),),
        )
        out = []
        for r in rows:
            self.close_conversation(r["id"], "silence", now)
            out.append((r["id"], "silence"))
        return out

    def insert_utterance(
        self,
        ev: UtteranceEvent,
        conversation_id: str,
        utterance_vec: list[float] | None = None,
    ) -> str:
        """Idempotent by client id. Returns 'inserted' | 'duplicate'."""
        now = utc_now()
        self.upsert_speaker_from_utterance(ev.speaker_id, ev.speaker_label, now,
                                           is_owner_label=ev.speaker_label.lower() == "owner")
        loc_json = ev.location.model_dump_json() if ev.location else None
        with self.db.transaction() as conn:
            dup = conn.execute("SELECT 1 FROM utterances WHERE id=?", (ev.id,)).fetchone()
            if dup:
                return "duplicate"
            conn.execute(
                "INSERT INTO utterances (id,conversation_id,revision,start_ts,end_ts,speaker_id,"
                "speaker_label,speaker_confidence,text,asr_confidence,is_whisper,location_json,"
                "device,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ev.id, conversation_id, ev.revision, iso(ev.start_ts),
                    iso(ev.end_ts) if ev.end_ts else None,
                    ev.speaker_id, ev.speaker_label, ev.speaker_confidence, ev.text,
                    ev.asr_confidence, 1 if ev.is_whisper else 0, loc_json, ev.device, iso(now),
                ),
            )
            conn.execute("INSERT INTO utterances_fts (utterance_id, text) VALUES (?,?)",
                         (ev.id, ev.text))
            conn.execute(
                "UPDATE conversations SET last_utterance_at=?, utterance_count=utterance_count+1 "
                "WHERE id=?",
                (iso(max(ev.end_ts or ev.start_ts, ev.start_ts)), conversation_id),
            )
        if utterance_vec is not None:
            self.utterance_vectors.upsert(ev.id, utterance_vec)
        return "inserted"

    def apply_revision(
        self, utterance_id: str, revision: int, text: str,
        speaker_id: str | None, speaker_label: str | None,
    ) -> bool:
        """Offline-refinement revisions supersede earlier rows (spec §3.2.4)."""
        now = utc_now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT revision, text FROM utterances WHERE id=?",
                               (utterance_id,)).fetchone()
            if row is None or revision <= row["revision"] or row["text"] == text:
                return False
            conn.execute(
                "UPDATE utterances SET revision=?, text=?, "
                "speaker_id=COALESCE(?,speaker_id), speaker_label=COALESCE(?,speaker_label) "
                "WHERE id=?",
                (revision, text, speaker_id, speaker_label, utterance_id),
            )
            conn.execute("DELETE FROM utterances_fts WHERE utterance_id=?", (utterance_id,))
            conn.execute("INSERT INTO utterances_fts (utterance_id, text) VALUES (?,?)",
                         (utterance_id, text))
            conn.execute(
                "INSERT OR REPLACE INTO utterance_revisions "
                "(utterance_id,revision,text,speaker_id,speaker_label,updated_at) VALUES (?,?,?,?,?,?)",
                (utterance_id, revision, text, speaker_id, speaker_label, iso(now)),
            )
        return True

    def get_utterance(self, utterance_id: str):
        return self.db.query_one("SELECT * FROM utterances WHERE id=?", (utterance_id,))

    def utterances_by_ids(self, ids: list[str]):
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.db.query(
            f"SELECT * FROM utterances WHERE id IN ({marks}) ORDER BY start_ts", tuple(ids)
        )

    def conversation_utterances(self, conversation_id: str):
        return self.db.query(
            "SELECT * FROM utterances WHERE conversation_id=? ORDER BY start_ts, received_at",
            (conversation_id,),
        )

    def recent_utterances(self, since: datetime, exclude_whisper: bool = True):
        sql = "SELECT * FROM utterances WHERE start_ts >= ?"
        if exclude_whisper:
            sql += " AND is_whisper = 0"
        sql += " ORDER BY start_ts"
        return self.db.query(sql, (iso(since),))

    def conversations_list(self, limit: int = 50, offset: int = 0):
        return self.db.query(
            "SELECT * FROM conversations ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def mark_extracted(self, conversation_id: str, last_utt_id: str | None, status: str) -> None:
        self.db.execute(
            "UPDATE conversations SET extraction_status=?, last_extracted_utterance_id=? WHERE id=?",
            (status, last_utt_id, conversation_id),
        )

    def unextracted_utterances(self, conversation_id: str, after_utt_id: str | None = None):
        rows = self.conversation_utterances(conversation_id)
        if after_utt_id:
            ids = [r["id"] for r in rows]
            if after_utt_id in ids:
                rows = rows[ids.index(after_utt_id) + 1 :]
        return rows

    # ------------------------------------------------------------- entities
    def upsert_entity(
        self, name: str, kind: str, aliases: list[str] | None = None,
        unresolved: bool = False, entity_id: str | None = None,
    ) -> str:
        now = iso(utc_now())
        if entity_id:
            existing = self.db.query_one("SELECT * FROM entities WHERE id=?", (entity_id,))
            if existing:
                merged = sorted(set(json.loads(existing["aliases"]) + (aliases or [])))
                self.db.execute(
                    "UPDATE entities SET aliases=?, unresolved=MIN(unresolved,?), updated_at=? WHERE id=?",
                    (json.dumps(merged), 1 if unresolved else 0, now, entity_id),
                )
                return entity_id
        row = self.db.query_one("SELECT * FROM entities WHERE kind=? AND name=?", (kind, name))
        if row:
            merged = sorted(set(json.loads(row["aliases"]) + (aliases or [])))
            self.db.execute(
                "UPDATE entities SET aliases=?, updated_at=? WHERE id=?",
                (json.dumps(merged), now, row["id"]),
            )
            return row["id"]
        eid = entity_id or new_id()
        self.db.execute(
            "INSERT INTO entities (id,kind,name,aliases,unresolved,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (eid, kind, name, json.dumps(sorted(set(aliases or []))), 1 if unresolved else 0, now, now),
        )
        return eid

    def resolve_entity_by_alias(self, name: str):
        target = name.strip().casefold()
        for r in self.db.query("SELECT * FROM entities"):
            if target == r["name"].casefold():
                return r
            for a in json.loads(r["aliases"]):
                if target == a.casefold():
                    return r
        return None

    def entity_candidates(self, kind: str | None = None):
        rows = self.db.query(
            "SELECT * FROM entities" + (" WHERE kind=?" if kind else ""),
            (kind,) if kind else (),
        )
        return [dict(r) for r in rows]

    def get_entity(self, entity_id: str):
        return self.db.query_one("SELECT * FROM entities WHERE id=?", (entity_id,))

    def resolve_entity_name(self, name: str):
        """Exact/alias hit on normalized name (used by extraction; fuzzy lives in core.normalize)."""
        from ..core.normalize import similarity

        best, best_score = None, 0.0
        for r in self.entity_candidates():
            s = max(
                [similarity(name, r["name"])] + [similarity(name, a) for a in json.loads(r["aliases"])]
            )
            if s > best_score:
                best, best_score = r, s
        return best if best_score >= 0.999 else None

    def attach_memory_entities(self, memory_id: str, links: list[tuple[str, str]]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO memory_entities (memory_id, entity_id, role) VALUES (?,?,?)",
            [(memory_id, eid, role) for eid, role in links],
        )

    def entities_for_memory(self, memory_id: str) -> list[dict]:
        rows = self.db.query(
            "SELECT e.id, e.kind, e.name, me.role FROM memory_entities me "
            "JOIN entities e ON e.id = me.entity_id WHERE me.memory_id=?",
            (memory_id,),
        )
        return [dict(r) for r in rows]

    def entity_ids_for_names(self, names: set[str]) -> set[str]:
        if not names:
            return set()
        from ..core.normalize import normalize_name

        targets = {normalize_name(n) for n in names if n}
        out: set[str] = set()
        for r in self.entity_candidates():
            forms = {normalize_name(r["name"]), *[normalize_name(a) for a in json.loads(r["aliases"])]}
            if targets & forms:
                out.add(r["id"])
        return out

    def matched_entity_forms(self, text: str) -> tuple[set[str], set[str]]:
        """Case-insensitive scan of free text for known entity names/aliases (ASR output
        is not reliably capitalized). Returns (entity_ids, canonical_names)."""
        from ..core.normalize import normalize_name

        norm = " " + normalize_name(text) + " "
        ids: set[str] = set()
        names: set[str] = set()
        for r in self.entity_candidates():
            forms = [normalize_name(r["name"])] + [normalize_name(a) for a in json.loads(r["aliases"])]
            for f in forms:
                if len(f) >= 3 and re.search(r"\b" + re.escape(f) + r"\b", norm):
                    ids.add(r["id"])
                    names.add(r["name"])
                    break
        return ids, names

    def entity_edges_for(self, entity_ids: set[str]) -> list[dict]:
        if not entity_ids:
            return []
        marks = ",".join("?" * len(entity_ids))
        rows = self.db.query(
            f"SELECT * FROM entity_edges WHERE src IN ({marks}) OR dst IN ({marks})",
            tuple(entity_ids) * 2,
        )
        return [dict(r) for r in rows]

    def log_entity_edge(self, src: str, dst: str, kind: str, ts: datetime) -> None:
        now = iso(ts)
        self.db.execute(
            "INSERT INTO entity_edges (src,dst,kind,first_seen,last_seen,weight) "
            "VALUES (?,?,?,?,?,1.0) ON CONFLICT(src,dst,kind) DO UPDATE SET "
            "last_seen=excluded.last_seen, weight=weight+1",
            (src, dst, kind, now, now),
        )

    def set_entity_observation(self, entity_id: str, text: str, count: int) -> None:
        self.db.execute(
            "INSERT INTO entity_observations (entity_id,text,source_memory_count,updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET text=excluded.text, "
            "source_memory_count=excluded.source_memory_count, updated_at=excluded.updated_at",
            (entity_id, text, count, iso(utc_now())),
        )

    def get_entity_observation(self, entity_id: str):
        return self.db.query_one("SELECT * FROM entity_observations WHERE entity_id=?", (entity_id,))

    def entities_needing_observation(self, min_new: int = 1) -> list[dict]:
        rows = self.db.query(
            """SELECT e.id, e.name, e.kind, COUNT(me.memory_id) AS n,
                      COALESCE(o.source_memory_count, -1) AS known
               FROM entities e
               LEFT JOIN memory_entities me ON me.entity_id = e.id
               LEFT JOIN entity_observations o ON o.entity_id = e.id
               GROUP BY e.id HAVING n - known >= ? AND n > 0""",
            (min_new,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- memories
    def insert_memory(
        self,
        text: str,
        structured: dict[str, Any],
        type: str,
        importance: float,
        confidence: float,
        conversation_id: str | None,
        speaker_id: str | None,
        source_utterance_ids: list[str],
        event_start: datetime | None = None,
        event_end: datetime | None = None,
        valid_from: datetime | None = None,
        source: str = "extraction",
        embedding: list[float] | None = None,
        memory_id: str | None = None,
    ) -> str:
        mid = memory_id or new_id()
        now = utc_now()
        vf = valid_from or now
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO memories (id,text,structured_json,type,importance,confidence,"
                "event_start,event_end,valid_from,recorded_at,source_utterance_ids,"
                "speaker_id,conversation_id,status,source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                (
                    mid, text, json.dumps(structured, ensure_ascii=False), type, importance,
                    confidence, iso(event_start) if event_start else None,
                    iso(event_end) if event_end else None, iso(vf), iso(now),
                    json.dumps(source_utterance_ids), speaker_id, conversation_id, source,
                ),
            )
            conn.execute("INSERT INTO memories_fts (memory_id, text) VALUES (?,?)", (mid, text))
        if embedding is not None:
            self.memory_vectors.upsert(mid, embedding)
        return mid

    def get_memory(self, memory_id: str, include_inactive: bool = True):
        row = self.db.query_one("SELECT * FROM memories WHERE id=?", (memory_id,))
        return row

    def memory_out(self, row) -> MemoryOut:
        speaker = (
            self.get_speaker(row["speaker_id"]) if row["speaker_id"] else None
        )
        return MemoryOut(
            id=row["id"], text=row["text"], type=row["type"], importance=row["importance"],
            confidence=row["confidence"], event_start=row["event_start"], event_end=row["event_end"],
            valid_from=row["valid_from"], valid_to=row["valid_to"], recorded_at=row["recorded_at"],
            status=row["status"], source=row["source"], speaker_id=row["speaker_id"],
            speaker_name=(speaker["name"] or speaker["label"]) if speaker else None,
            conversation_id=row["conversation_id"],
            source_utterance_ids=json.loads(row["source_utterance_ids"]),
            entities=self.entities_for_memory(row["id"]),
            structured=json.loads(row["structured_json"]),
        )

    def list_supersedes(self, new_id_: str):
        """Outbound supersession is stored on the OLD row (superseded_by -> new)."""
        return self.db.query("SELECT id, text FROM memories WHERE superseded_by=?", (new_id_,))

    def supersede(self, old_id: str, new_id_: str, valid_to: datetime | None = None) -> None:
        """Never overwrite: close the old fact, link to the new one (spec §5.3)."""
        self.db.execute(
            "UPDATE memories SET status='superseded', superseded_by=?, valid_to=? "
            "WHERE id=? AND status='active'",
            (new_id_, iso(valid_to or utc_now()), old_id),
        )

    def retract(self, memory_id: str) -> None:
        self.db.execute(
            "UPDATE memories SET status='retracted', valid_to=? WHERE id=?",
            (iso(utc_now()), memory_id),
        )

    def delete_memory(self, memory_id: str) -> bool:
        self.memory_vectors.delete(memory_id)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM memories_fts WHERE memory_id=?", (memory_id,))
            conn.execute("DELETE FROM memory_entities WHERE memory_id=?", (memory_id,))
            conn.execute("UPDATE memories SET superseded_by=NULL WHERE superseded_by=?",
                         (memory_id,))
            conn.execute("UPDATE cards_shown SET memory_id=NULL WHERE memory_id=?",
                         (memory_id,))
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return cur.rowcount > 0

    def active_memories_for_entity(self, entity_id: str, limit: int = 200):
        return self.db.query(
            "SELECT m.* FROM memories m JOIN memory_entities me ON me.memory_id=m.id "
            "WHERE me.entity_id=? AND m.status='active' ORDER BY m.recorded_at DESC LIMIT ?",
            (entity_id, limit),
        )

    # ------------------------------------------------------------- hybrid search
    def search(
        self,
        query_text: str = "",
        query_vec: list[float] | None = None,
        entity_ids: set[str] | None = None,
        time_range: tuple[datetime, datetime] | None = None,
        speaker_id: str | None = None,
        status: str | None = "active",
        limit: int = 20,
    ) -> list[SearchHit]:
        """Union of vector / FTS5 / entity / temporal channels; per-channel signals attached.
        (Spec §5.4 step 2; final scoring happens in pipeline.relevance.)"""
        hits: dict[str, SearchHit] = {}
        k = max(limit * 2, 10)

        if query_vec is not None:
            for mid, sim in self.memory_vectors.search(query_vec, k).items():
                hits.setdefault(mid, SearchHit(row=None)).semantic = max(0.0, sim)

        match = _fts_match_text(query_text)
        if match:
            rows = self.db.query(
                "SELECT f.memory_id AS id, bm25(memories_fts) AS rank FROM memories_fts f "
                "JOIN memories m ON m.id = f.memory_id "
                + ("WHERE memories_fts MATCH ? AND m.status=? " if status else "WHERE memories_fts MATCH ? ")
                + "ORDER BY rank LIMIT ?",
                (match, status, k) if status else (match, k),
            )
            scores = {r["id"]: -float(r["rank"]) for r in rows}  # bm25: lower=better
            if scores:
                lo, hi = min(scores.values()), max(scores.values())
                rng = hi - lo
                for mid, s in scores.items():
                    h = hits.setdefault(mid, SearchHit(row=None))
                    h.keyword = (s - lo) / rng if rng > 1e-9 else 1.0

        if entity_ids:
            marks = ",".join("?" * len(entity_ids))
            sql = (
                f"SELECT DISTINCT me.memory_id AS id FROM memory_entities me "
                f"JOIN memories m ON m.id = me.memory_id WHERE me.entity_id IN ({marks})"
            )
            sql += " AND m.status=?" if status else ""
            params = (*entity_ids, *((status,) if status else ()), k)
            rows = self.db.query(sql + " LIMIT ?", params)
            for r in rows:
                hits.setdefault(r["id"], SearchHit(row=None)).entity_hit = True

        if time_range:
            a, b = time_range
            sql = "SELECT id FROM memories WHERE event_start IS NOT NULL AND event_start>=? AND event_start<=?"
            params: tuple = (iso(a), iso(b))
            if status:
                sql += " AND status=?"
                params += (status,)
            for r in self.db.query(sql + " LIMIT ?", (*params, k)):
                hits.setdefault(r["id"], SearchHit(row=None)).temporal_hit = True

        if not hits:
            return []
        ids = list(hits.keys())
        marks = ",".join("?" * len(ids))
        sql = f"SELECT * FROM memories WHERE id IN ({marks})"
        params = tuple(ids)
        if status:
            sql += " AND status=?"
            params += (status,)
        rows = {r["id"]: r for r in self.db.query(sql, params)}
        out = []
        for mid, h in hits.items():
            if mid in rows:
                h.row = rows[mid]
                out.append(h)
        if speaker_id:
            out = [h for h in out if h.row["speaker_id"] == speaker_id]
        out.sort(key=lambda h: (h.semantic, h.keyword), reverse=True)
        return out[:limit]

    # ------------------------------------------------------------- cards / feedback
    def record_card_shown(self, card_id: str, memory_id: str | None, kind: str, score: float,
                          features: dict, context_utterance_id: str | None, now: datetime) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO cards_shown (id,memory_id,kind,score,features_json,"
            "context_utterance_id,shown_at) VALUES (?,?,?,?,?,?,?)",
            (card_id, memory_id, kind, score, json.dumps(features, ensure_ascii=False),
             context_utterance_id, iso(now)),
        )

    def record_feedback(self, card_id: str, kind: str, at: datetime) -> str | None:
        row = self.db.query_one("SELECT memory_id FROM cards_shown WHERE id=?", (card_id,))
        memory_id = row["memory_id"] if row else None
        self.db.execute(
            "INSERT INTO feedback (card_id,memory_id,kind,created_at) VALUES (?,?,?,?)",
            (card_id, memory_id, kind, iso(at)),
        )
        if kind == "dismiss":
            self.db.execute("UPDATE cards_shown SET dismissed_at=? WHERE id=?", (iso(at), card_id))
        elif kind == "pin":
            self.db.execute("UPDATE cards_shown SET pinned_at=? WHERE id=?", (iso(at), card_id))
        elif kind == "unpin":
            self.db.execute("UPDATE cards_shown SET pinned_at=NULL WHERE id=?", (card_id,))
        return memory_id

    def last_shown_map(self) -> dict[str, datetime]:
        rows = self.db.query(
            "SELECT memory_id, MAX(shown_at) AS t FROM cards_shown "
            "WHERE memory_id IS NOT NULL GROUP BY memory_id"
        )
        return {r["memory_id"]: parse_iso(r["t"]) for r in rows}

    def dismiss_counts(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT memory_id, COUNT(*) AS n FROM feedback WHERE kind='dismiss' AND memory_id IS NOT NULL "
            "GROUP BY memory_id"
        )
        return {r["memory_id"]: r["n"] for r in rows}

    def pinned_memory_ids(self) -> set[str]:
        rows = self.db.query(
            "SELECT memory_id FROM cards_shown WHERE pinned_at IS NOT NULL AND memory_id IS NOT NULL"
        )
        return {r["memory_id"] for r in rows}

    def feedback_log(self, limit: int = 1000):
        return self.db.query(
            "SELECT f.*, c.score, c.features_json, c.kind AS card_kind FROM feedback f "
            "JOIN cards_shown c ON c.id = f.card_id ORDER BY f.created_at DESC LIMIT ?", (limit,)
        )

    # ------------------------------------------------------------- clarifications
    def insert_clarification(
        self, kind: str, question: str, context: dict, importance: float, uncertainty: float,
        conversation_id: str | None, related_memory_ids: list[str], related_speaker_id: str | None,
        related_entity_id: str | None, now: datetime, expiry_days: int,
    ) -> str | None:
        from ..core.clarifications import dedupe_key, expected_value

        dkey = dedupe_key(kind, context.get("subject", question))
        for r in self.db.query("SELECT context_json FROM clarifications WHERE status IN ('open','reminded')"):
            if json.loads(r["context_json"]).get("_dedupe") == dkey:
                return None  # already asked
        cid = new_id()
        self.db.execute(
            "INSERT INTO clarifications (id,kind,question,context_json,importance,uncertainty,"
            "expected_value,status,conversation_id,related_memory_ids,related_speaker_id,"
            "related_entity_id,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid, kind, question,
                json.dumps({**context, "_dedupe": dkey}, ensure_ascii=False),
                importance, uncertainty, expected_value(importance, uncertainty), "open",
                conversation_id, json.dumps(related_memory_ids), related_speaker_id,
                related_entity_id, iso(now), iso(now + timedelta(days=expiry_days)),
            ),
        )
        return cid

    def list_clarifications(self, statuses: tuple[str, ...] = ("open",)) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        rows = self.db.query(
            f"SELECT * FROM clarifications WHERE status IN ({marks}) "
            f"ORDER BY expected_value DESC, created_at DESC",
            statuses,
        )
        return [self._clar_dict(r) for r in rows]

    def list_all_clarifications(self) -> list[dict]:
        return [self._clar_dict(r) for r in self.db.query(
            "SELECT * FROM clarifications ORDER BY created_at DESC")]

    def _clar_dict(self, r) -> dict:
        d = dict(r)
        d["context"] = json.loads(d.pop("context_json"))
        d["related_memory_ids"] = json.loads(d["related_memory_ids"])
        return d

    def get_clarification(self, cid: str):
        return self.db.query_one("SELECT * FROM clarifications WHERE id=?", (cid,))

    def answer_clarification(self, cid: str, answer: str, source: str) -> bool:
        cur = self.db.execute(
            "UPDATE clarifications SET status='answered', answered_at=?, answer_text=?, "
            "answer_source=? WHERE id=? AND status IN ('open','reminded')",
            (iso(utc_now()), answer, source, cid),
        )
        return cur.rowcount > 0

    def set_clarification_status(self, cid: str, status: str) -> None:
        self.db.execute("UPDATE clarifications SET status=? WHERE id=?", (status, cid))

    def mark_reminded(self, cid: str) -> None:
        self.db.execute("UPDATE clarifications SET status='reminded', reminded=1 WHERE id=?", (cid,))

    def mark_digest_sent(self, ids: list[str], now: datetime) -> None:
        marks = ",".join("?" * len(ids))
        self.db.execute(
            f"UPDATE clarifications SET digest_sent_at=? WHERE id IN ({marks})",
            (iso(now), *ids),
        )

    # ------------------------------------------------------------- vocabulary
    def list_vocabulary(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM vocabulary ORDER BY term")]

    def upsert_vocabulary(
        self, term: str, kind: str = "name", definition: str | None = None,
        entity_id: str | None = None, source: str = "manual", boost: float = 5.0,
    ) -> None:
        now = iso(utc_now())
        self.db.execute(
            "INSERT INTO vocabulary (term,kind,definition,entity_id,boost,source,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(term) DO UPDATE SET kind=excluded.kind, "
            "definition=COALESCE(excluded.definition, vocabulary.definition), "
            "entity_id=COALESCE(excluded.entity_id, vocabulary.entity_id), "
            "boost=excluded.boost, source=excluded.source, updated_at=excluded.updated_at",
            (term.lower(), kind, definition, entity_id, boost, source, now, now),
        )

    # ------------------------------------------------------------- kv / stats / export
    def kv_get(self, key: str) -> str | None:
        r = self.db.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else None

    def kv_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def kv_bump(self, key: str) -> int:
        n = int(self.kv_get(key) or "0") + 1
        self.kv_set(key, str(n))
        return n

    def stats(self) -> dict:
        def count(table: str, where: str = "") -> int:
            return self.db.query_one(f"SELECT COUNT(*) AS n FROM {table} {where}")["n"]

        return {
            "speakers": count("speakers", "WHERE merged_into IS NULL"),
            "conversations": count("conversations"),
            "open_conversations": count("conversations", "WHERE closed_at IS NULL"),
            "utterances": count("utterances"),
            "memories": count("memories", "WHERE status='active'"),
            "entities": count("entities"),
            "open_clarifications": count("clarifications", "WHERE status IN ('open','reminded')"),
            "cards_shown": count("cards_shown"),
        }

    def export_all(self) -> dict:
        def dump(table: str) -> list[dict]:
            return [dict(r) for r in self.db.query(f"SELECT * FROM {table}")]

        out = {
            "exported_at": iso(utc_now()),
            "version": 1,
        }
        for t in ("speakers", "conversations", "utterances", "utterance_revisions", "entities",
                  "memories", "memory_entities", "entity_edges", "entity_observations",
                  "clarifications", "cards_shown", "feedback", "vocabulary"):
            out[t] = dump(t)
        return out
