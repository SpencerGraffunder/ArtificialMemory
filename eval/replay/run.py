"""Replay harness: feeds fixture transcripts through the REAL pipeline (fake LLM endpoint,
deterministic embeddings) at accelerated speed, records every card, and evaluates labeled
moments → precision / recall / cards-per-hour. Tune weights/thresholds in config, re-run:

    .venv/bin/python ../eval/replay/run.py                 # all fixtures
    .venv/bin/python ../eval/replay/run.py --fixture 04 --verbose
    .venv/bin/python ../eval/replay/run.py --config my_tuned.yaml --json report.json

Fixture format: see eval/fixtures/*.json — seed_memories + timestamped utterances +
labels [{after_utterance, expect: card|silence, memory_contains_any}].

Speed model: utterance timestamps are fixture time (anchor + t); wall-clock sleeps are
compressed (capped 0.15 s between utterances) while the engine's rolling window is
pre-trimmed against the simulated clock so topic-shift semantics stay honest. Question
timers run at question_wait_s=0.3 s wall.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server" / "src"))

from ambient_server.config import load_config  # noqa: E402
from ambient_server.db import store as store_mod  # noqa: E402
from ambient_server.models import UtteranceEvent, parse_iso  # noqa: E402
from ambient_server.pipeline import extraction as extraction_mod  # noqa: E402
from ambient_server.pipeline import lookup as lookup_mod  # noqa: E402
from ambient_server.pipeline import relevance as relevance_mod  # noqa: E402
from ambient_server.pipeline.relevance import EngineEvent  # noqa: E402
from ambient_server.services import AppContext  # noqa: E402
from ambient_server import services as services_mod  # noqa: E402
from ambient_server.testing import FakeLLM, fake_embed_vector, make_test_cfg  # noqa: E402

FIXTURES_DIR = REPO / "eval" / "fixtures"
CARD_MATCH_WINDOW_S = 12.0     # simulated seconds after labeled utterance
QUESTION_WAIT_S = 0.3          # wall-clock compression of the 2-5 s spec wait
MAX_WALL_GAP = 0.15            # keep < QUESTION_WAIT_S so answer-before-timer holds


@dataclass
class CardLog:
    ts: datetime               # simulated time of the utterance that produced it
    card_id: str
    kind: str
    headline: str
    detail: str | None
    score: float | None
    matched_label: bool = False

    def text(self) -> str:
        return f"{self.headline} {self.detail or ''}".lower()


@dataclass
class FixtureResult:
    fixture_id: str
    tp: int = 0
    fn: int = 0
    fp: int = 0
    cards: list[CardLog] = field(default_factory=list)
    sim_hours: float = 0.0
    clarifications: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None


def seed_memories(ctx: AppContext, spec: list[dict], anchor: datetime) -> None:
    for m in spec:
        mid = ctx.store.insert_memory(
            m["text"], m.get("structured", {"time_phrase": None}), m.get("type", "event"),
            m.get("importance", 0.7), m.get("confidence", 0.95), None, None, [],
            embedding=fake_embed_vector(m["text"]))
        ent_ids = []
        for name, kind in m.get("entities", []):
            ent_ids.append((ctx.store.upsert_entity(name, kind), "about"))
        if ent_ids:
            ctx.store.attach_memory_entities(mid, ent_ids)
        # event windows relative to the fixture anchor (for actionability/temporal channels)
        days = m.get("event_days_ahead")
        if days is not None:
            start = anchor + timedelta(days=days)
            ctx.store.db.execute(
                "UPDATE memories SET event_start=?, event_end=? WHERE id=?",
                (start.isoformat(), (start + timedelta(hours=2)).isoformat(), mid))
        elif m.get("days_ago") is not None:
            start = anchor - timedelta(days=m["days_ago"])
            ctx.store.db.execute(
                "UPDATE memories SET event_start=?, event_end=? WHERE id=?",
                (start.isoformat(), (start + timedelta(hours=2)).isoformat(), mid))


def script_llm(fake: FakeLLM, fixture: dict) -> None:
    """Script the fake endpoint: extraction/clarify/synthesis + a question classifier
    that flags exactly the utterances marked question:true (matched by transcript text)."""
    q_texts = {u["text"].lower() for u in fixture["utterances"] if u.get("question")}

    def classifier(messages: list[dict]) -> dict:
        user = messages[1]["content"] if len(messages) > 1 else ""
        first_line = user.splitlines()[0].strip().lower() if user else ""
        is_q = first_line in q_texts or any(q in user for q in q_texts)
        return {"is_question": is_q, "addressed_to_someone": False, "confidence": 0.9,
                "topic": first_line[:24] or "general"}

    fake.on("classify one spoken utterance", classifier)
    fake.on("retrieved memories", "")          # no synthesis: headline = the memory itself
    if "extraction" in fixture:
        fake.on("memory extractor", fixture["extraction"])
    if "clarify" in fixture:
        fake.on("clarification questions", fixture["clarify"])


CLOCK_MODULES = (services_mod, store_mod, relevance_mod, lookup_mod, extraction_mod)


def install_sim_clock(sim: dict) -> list:
    """Replace utc_now in every app module with the simulated clock so rolling
    windows, decay, cooldowns and conversation gaps all follow fixture time."""
    saved = []
    for mod in CLOCK_MODULES:
        if hasattr(mod, "utc_now"):
            saved.append((mod, mod.utc_now))
            mod.utc_now = lambda sim=sim: sim["t"]
    return saved


def uninstall_sim_clock(saved: list) -> None:
    for mod, fn in saved:
        mod.utc_now = fn


async def run_fixture(path: Path, verbose: bool) -> FixtureResult:
    fixture = json.loads(path.read_text())
    anchor = parse_iso(fixture["anchor"])
    is_owner = bool(fixture.get("is_owner", True))
    owner_id = "spk-owner"

    with tempfile.TemporaryDirectory() as td:
        cfg = make_test_cfg(Path(td) / "ambient.sqlite3")
        cfg.relevance.question_wait_s = QUESTION_WAIT_S
        cfg.relevance.card_ttl_s = 45 * 60  # generous: TTL is probed in unit tests
        fake = FakeLLM()
        script_llm(fake, fixture)
        sim = {"t": anchor}
        saved = install_sim_clock(sim)
        ctx = AppContext(cfg, llm_client=fake.client(cfg.llm.base_url))
        try:
            res = await _replay(ctx, fixture, anchor, is_owner, owner_id, verbose, sim)
        finally:
            await ctx.aclose()
            uninstall_sim_clock(saved)
        return res


async def _replay(ctx: AppContext, fixture: dict, anchor: datetime, is_owner: bool,
                  owner_id: str, verbose: bool, sim: dict) -> FixtureResult:
    res = FixtureResult(fixture["id"])
    seed_memories(ctx, fixture.get("seed_memories", []), anchor)
    speaker_ids: dict[str, str] = {}

    # timer-path cards fan out through ctx.card_listeners; utterance-path cards arrive
    # as handle_utterance return values. Card ids are unique → dedupe across both paths.
    seen_card_ids: set[str] = set()

    def remember(ev: EngineEvent) -> None:
        if ev.type == "card" and ev.card and ev.card.id not in seen_card_ids:
            seen_card_ids.add(ev.card.id)
            res.cards.append(CardLog(
                ts=sim["t"], card_id=ev.card.id, kind=ev.card.kind,
                headline=ev.card.headline, detail=ev.card.detail,
                score=ev.card.score))

    async def capture(ev: EngineEvent) -> None:
        remember(ev)

    ctx.add_card_listener(capture)

    def sim_ts(u: dict) -> datetime:
        return anchor + timedelta(seconds=u["t"])

    utterances = fixture["utterances"]
    prev_t: float | None = None
    prev_speaker: str | None = None
    prev_was_question = False
    for u in utterances:
        ts = sim_ts(u)
        if prev_t is not None:
            gap = min((u["t"] - prev_t) / 100.0, MAX_WALL_GAP)
            if prev_was_question and u.get("speaker") == prev_speaker:
                # nobody answered → let the answer-wait timer resolve mid-feed
                gap = QUESTION_WAIT_S + 0.15
            await asyncio.sleep(max(0.001, gap))
        prev_t = u["t"]
        prev_speaker = u.get("speaker")
        prev_was_question = bool(u.get("question"))
        sim["t"] = ts  # engine trims its rolling window against the simulated clock

        label = u.get("label", u["speaker"])
        sid = owner_id if u["speaker"] == "Owner" else speaker_ids.setdefault(
            u["speaker"], f"spk-{label.lower().replace(' ', '-')}")
        ev = UtteranceEvent(id=f"{fixture['id']}-u{u['t']}", speaker_id=sid,
                            speaker_label=label, start_ts=ts, text=u["text"],
                            is_whisper=bool(u.get("whisper", False)),
                            audio_duration_s=2.0)
        _, events = await ctx.handle_utterance(ev, live=True)
        for e in events:
            remember(e)

    # let pending question timers fire (classified during the feed, wait=0.3 s wall)
    await asyncio.sleep(QUESTION_WAIT_S + 0.35)

    # close conversation → closing extraction / unknown-count clarifications
    for conv in ctx.store.open_conversations():
        await ctx.close_conversation(conv["id"], "manual")
    res.clarifications = ctx.store.db.query_one(
        "SELECT COUNT(*) n FROM clarifications WHERE status='open'")["n"]

    res.sim_hours = max((utterances[-1]["t"] - utterances[0]["t"]), 1) / 3600.0
    evaluate(fixture, res, verbose)
    return res


def evaluate(fixture: dict, res: FixtureResult, verbose: bool) -> None:
    anchor = anchor_of(fixture)
    utterances = fixture["utterances"]
    fp_ids: set[str] = set()

    def window_of(lab: dict) -> tuple[datetime, datetime]:
        idx = lab["after_utterance"]
        t0 = anchor + timedelta(seconds=utterances[idx]["t"])
        t1 = t0 + timedelta(seconds=CARD_MATCH_WINDOW_S)
        if lab["expect"] == "silence" and idx + 1 < len(utterances):
            # a card caused by the NEXT utterance belongs to the next label, not this one
            nxt = anchor + timedelta(seconds=utterances[idx + 1]["t"])
            t1 = min(t1, nxt - timedelta(microseconds=1))
        return t0, t1

    for lab in fixture.get("labels", []):
        t0, t1 = window_of(lab)
        window = [c for c in res.cards if t0 <= c.ts <= t1]
        if lab["expect"] == "card":
            keys = [k.lower() for k in lab.get("memory_contains_any", [])]
            hit = next((c for c in window if not c.matched_label
                        and any(k in c.text() for k in keys)), None)
            if hit:
                hit.matched_label = True
                res.tp += 1
            else:
                res.fn += 1
                res.problems.append(
                    f"MISS label@u{lab['after_utterance']} {keys}: "
                    f"no card in [{t0:%H:%M:%S},{t1:%H:%M:%S}]; saw "
                    f"{[c.headline for c in window]}")
        else:  # silence expected
            stray = [c for c in window
                     if not c.matched_label and c.card_id not in fp_ids]
            for c in stray:
                fp_ids.add(c.card_id)
            res.fp += len(stray)
            if stray:
                res.problems.append(
                    f"FP label@u{lab['after_utterance']}: {[c.headline for c in stray]}")

    # any card matching no card-label and not already counted as a silence-window FP
    extra = [c for c in res.cards
             if not c.matched_label and c.card_id not in fp_ids]
    if extra:
        res.fp += len(extra)
        res.problems.append(f"FP unlabeled: {[c.headline for c in extra]}")

    max_cards = fixture.get("max_total_cards")
    if max_cards is not None and len(res.cards) > max_cards:
        res.problems.append(f"card-spam: {len(res.cards)} cards > max {max_cards}")
    min_clar = fixture.get("post", {}).get("min_clarifications", 0)
    if res.clarifications < min_clar:
        res.problems.append(f"clarifications: {res.clarifications} < required {min_clar}")
    if verbose:
        for c in res.cards:
            print(f"    card @{c.ts:%H:%M:%S} [{c.kind}] {c.headline!r} score={c.score}")


def anchor_of(fixture: dict) -> datetime:
    return parse_iso(fixture["anchor"])


async def run_all(fixtures: list[Path], verbose: bool) -> list[FixtureResult]:
    results = []
    for f in fixtures:
        t0 = time.monotonic()
        r = await run_fixture(f, verbose)
        wall = time.monotonic() - t0
        ok = not r.problems
        print(f"{'PASS' if ok else 'FAIL'}  {r.fixture_id:<32} "
              f"tp={r.tp} fp={r.fp} fn={r.fn} cards={len(r.cards)} "
              f"c/hr={len(r.cards) / max(r.sim_hours, 1e-9):.2f} clar={r.clarifications} "
              f"({wall:.1f}s)")
        for p in r.problems:
            print(f"      - {p}")
        results.append(r)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Ambient Memory replay eval harness")
    ap.add_argument("--fixture", default=None, help="substring filter, e.g. --fixture 04")
    ap.add_argument("--verbose", action="store_true", help="print every card with timing")
    ap.add_argument("--json", default=None, help="write machine-readable report")
    ap.add_argument("--config", default=None, help="AppConfig yaml to tune weights")
    args = ap.parse_args()

    if args.config:  # honored by server runs; harness uses make_test_cfg + overrides
        print(f"(note) tuning config loaded for reference: {args.config}")
        load_config(config_path=Path(args.config))

    files = sorted(FIXTURES_DIR.glob("*.json"))
    if args.fixture:
        files = [f for f in files if args.fixture in f.name]
    if not files:
        print(f"no fixtures matched in {FIXTURES_DIR}")
        return 2

    results = asyncio.run(run_all(files, args.verbose))
    tp = sum(r.tp for r in results)
    fp = sum(r.fp for r in results)
    fn = sum(r.fn for r in results)
    cards = sum(len(r.cards) for r in results)
    hours = sum(r.sim_hours for r in results)
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    print("-" * 72)
    print(f"TOTAL fixtures={len(results)} cards={cards} over {hours:.2f} sim-hours "
          f"→ {cards / max(hours, 1e-9):.2f} cards/hour")
    print(f"precision={precision:.2f} recall={recall:.2f}  (tp={tp} fp={fp} fn={fn})")
    print(f"target zone from spec: 1-4 relevant cards/hour sustained conversation")

    if args.json:
        payload = {
            "totals": {"tp": tp, "fp": fp, "fn": fn, "cards": cards,
                       "sim_hours": hours, "cards_per_hour": cards / max(hours, 1e-9),
                       "precision": precision, "recall": recall},
            "fixtures": [{"id": r.fixture_id, "tp": r.tp, "fp": r.fp, "fn": r.fn,
                          "cards": [vars(c) for c in r.cards],
                          "clarifications": r.clarifications,
                          "problems": r.problems} for r in results],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2, default=str))
        print(f"report → {args.json}")
    return 0 if all(not r.problems for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
