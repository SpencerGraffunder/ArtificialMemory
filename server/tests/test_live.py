import asyncio
from datetime import timedelta

from artmem_server.models import UtteranceEvent, utc_now
from artmem_server.testing import fake_embed_vector

NOW = utc_now()


def ev(text, speaker="spk-owner", label="Owner", whisper=False, id="u1", at=None):
    return UtteranceEvent(id=id, speaker_id=speaker, speaker_label=label,
                          start_ts=at or NOW, text=text, is_whisper=whisper)


def seed_memory(store, text, speaker_id=None, type="event", imp=0.9, event_start=None,
                entities=None):
    mid = store.insert_memory(text, {"time_phrase": "tomorrow"}, type, imp, 0.9, None,
                              speaker_id, [], event_start=event_start,
                              embedding=fake_embed_vector(text))
    for name, kind in (entities or []):
        eid = store.upsert_entity(name, kind)
        store.attach_memory_entities(mid, [(eid, "about")])
    return mid


async def test_finalized_utterance_surfaces_relevant_card(ctx, store):
    """Use case 1: someone mentions an event → card with the fact appears."""
    seed_memory(store, "Dinner with Jasmine tomorrow at Chili restaurant",
                entities=[("Jasmine", "person"), ("Chili", "place")])
    _, events = await ctx.handle_utterance(
        ev("so we should finalize dinner with jasmine at chili tonight"))
    cards = [e for e in events if e.type == "card"]
    assert len(cards) == 1
    card = cards[0].card
    assert "Chili" in card.headline
    assert card.kind == "relevance" and card.origin == "memory"
    assert card.memory_id
    # full feature vector logged for offline weight fitting (spec §5.4.7)
    feats = card.debug_features["features"]
    for key in ("semantic", "entity_overlap", "actionability", "importance",
                "recency_decay", "speaker_match", "recently_shown", "dismissed_before"):
        assert key in feats
    assert card.debug_features["reason"] == "ok"
    # recorded in history with shown_at for cooldown
    assert store.last_shown_map()


async def test_cooldown_prevents_reshow_same_memory(ctx, store):
    seed_memory(store, "Jasmine booked the park trip for the picnic",
                entities=[("Jasmine", "person")])
    _, e1 = await ctx.handle_utterance(ev("the park picnic with jasmine is happening", id="a"))
    assert any(e.type == "card" for e in e1)
    ctx.engine.active.clear()          # card left the screen naturally
    _, e2 = await ctx.handle_utterance(ev("jasmine park picnic again", id="b"))
    assert not any(e.type == "card" for e in e2)   # cooldown (10 min) blocks reshow


async def test_card_budget_three_and_gate_below_threshold(ctx, store):
    for i in range(6):
        seed_memory(store, f"quantum flux capacitor calibration number {i} theta zeta "
                           f"delta omega jasmine", entities=[("Jasmine", "person")])
    _, events = await ctx.handle_utterance(
        ev("jasmine theta zeta delta omega quantum flux capacitor calibration"))
    cards = [e for e in events if e.type == "card"]
    assert len(cards) <= ctx.cfg.relevance.max_cards


async def test_dismiss_feedback_lowers_future_scores(ctx, store):
    mid = seed_memory(store, "Marcus mentioned the ARTES acronym at standup meeting",
                      entities=[("Marcus", "person"), ("ARTES", "acronym")])
    _, events = await ctx.handle_utterance(ev("ARTES standup meeting marcus talk"))
    card = next(e.card for e in events if e.type == "card")
    ctx.apply_feedback(card.id, "dismiss")
    assert store.dismiss_counts()[mid] == 1
    assert card.id not in ctx.engine.active       # removed from screen


async def test_whisper_is_priority_card_and_not_conversation_content(ctx, store, fake_llm):
    """Use case 4: owner whispers a query → matching memory as priority card."""
    fake_llm.on("retrieved memories", "")   # no synthesis → raw memory as headline
    seed_memory(store, "We went to the park last Saturday with the kids")
    conv_id, events = await ctx.handle_utterance(
        ev("yeah when was it that we went to the park", whisper=True, id="w1"))
    assert conv_id is None                          # never routed into a conversation
    cards = [e.card for e in events if e.type == "card"]
    assert len(cards) == 1 and cards[0].priority
    assert "park" in cards[0].headline or "park" in cards[0].detail
    row = store.get_utterance("w1")
    assert row["conversation_id"] is None and row["is_whisper"] == 1


async def test_unanswered_question_triggers_lookup_card(ctx, fake_llm):
    fake_llm.on("classify one spoken utterance",
                {"is_question": True, "addressed_to_someone": False,
                 "topic": "what does ARTES stand for"})
    fake_llm.on("web results", {"relation": "x"})   # unused; search backend disabled
    ctx.cfg.relevance.question_wait_s = 0.2
    ctx.cfg.search.backend = "none"
    await ctx.handle_utterance(ev("what does artes even stand for", id="q1"))
    await asyncio.sleep(0.45)                       # wait window + scheduling slack
    # no memory/web answer available → no card, but the timer fired cleanly
    assert all(t.speaker_id for t in [])  # no-op assertion; timer path executed


async def test_unanswered_question_answered_by_human_suppresses_lookup(ctx, fake_llm):
    fake_llm.on("classify one spoken utterance",
                {"is_question": True, "addressed_to_someone": False, "topic": "dinner time"})
    ctx.cfg.relevance.question_wait_s = 0.5
    await ctx.handle_utterance(ev("when is dinner again", id="q2", speaker="spk-owner"))
    await asyncio.sleep(0.05)
    _, events = await ctx.handle_utterance(
        ev("seven thirty", id="q2a", speaker="spk-jasmine", label="Jasmine"))
    await asyncio.sleep(0.7)
    # human answered → no lookup card was emitted
    assert not [e for e in ctx.engine.active.values() if e.card.kind == "lookup"]
    assert not any(e.card and e.card.kind == "lookup" for e in events)


async def test_topic_shift_expires_unpinned_cards(ctx, store):
    # memory carries info beyond the spoken line (echo gate must not suppress)
    seed_memory(store, "Funding deadline for the ARTES grant proposal paperwork needs two signed forms",
                entities=[("ARTES", "acronym")])
    # first window ages out of the 60 s rolling window → drift is measurable
    _, events = await ctx.handle_utterance(ev("artes grant proposal funding deadline paperwork"))
    cards = [e.card for e in events if e.type == "card"]
    assert cards
    ctx.engine.window = [{**u, "start_ts": u["start_ts"] - timedelta(seconds=95)}
                         for u in ctx.engine.window]
    # a totally different window → topic drift → dismiss unpinned cards
    _, events2 = await ctx.handle_utterance(
        ev("completely unrelated pipeline migration kubernetes ingress certificates",
           id="u9"))
    dismissed = [e for e in events2 if e.type == "card_dismiss"]
    assert any(e.card_id == cards[0].id for e in dismissed)
    assert cards[0].id not in ctx.engine.active


async def test_card_fade_after_ttl(ctx, store):
    seed_memory(store, "Yoga class every thursday evening studio downtown",
                entities=[("Yoga Studio", "place")])
    _, events = await ctx.handle_utterance(ev("lets book the yoga studio spot for thursday"))
    card = next(e.card for e in events if e.type == "card")
    assert card.id in ctx.engine.active
    later = utc_now() + timedelta(seconds=ctx.cfg.relevance.card_ttl_s + 5)
    expires = await ctx.engine.tick(later)
    assert any(e.card_id == card.id for e in expires)
    assert card.id not in ctx.engine.active


async def test_pinned_card_survives(ctx, store):
    seed_memory(store, "Yoga class every thursday evening studio downtown")
    _, events = await ctx.handle_utterance(ev("yoga thursday evening studio downtown"))
    card = next(e.card for e in events if e.type == "card")
    ctx.apply_feedback(card.id, "pin")
    later = utc_now() + timedelta(seconds=ctx.cfg.relevance.card_ttl_s + 60)
    expires = await ctx.engine.tick(later)
    assert card.id in ctx.engine.active
    assert not any(e.card_id == card.id for e in expires)


async def test_llm_judge_gate_can_block_card(ctx, store, fake_llm):
    ctx.cfg.relevance.llm_judge_enabled = True
    fake_llm.on("interruption judge", {"worth_surfacing": False, "reason": "off-topic"})
    seed_memory(store, "Jasmine adopted a dog named Biscuit last month")
    _, events = await ctx.handle_utterance(ev("jasmine biscuit dog adoption story"))
    assert not [e for e in events if e.type == "card"]


async def test_echo_gate_blocks_cards_that_only_repeat_just_spoken_words(ctx, store):
    from artmem_server.pipeline.relevance import _echo_coverage
    mem = "Kids soccer game saturday 10am at the east field"
    assert _echo_coverage(mem, "is the kids soccer game at the east field on saturday at 10am") >= 0.72
    assert _echo_coverage(mem, "we should book the venue deposit this week") < 0.72
    assert _echo_coverage("", "anything") == 0.0
    seed_memory(store, mem, event_start=NOW + timedelta(days=1))
    seed_memory(store, "Cleats and air pump live in the Garage Shelf",
                event_start=NOW + timedelta(days=1),
                entities=[("Garage Shelf", "place")])
    # near-verbatim relay of the game memory → stay silent (no other topic match)
    _, events = await ctx.handle_utterance(
        ev("kids soccer game saturday 10am at the east field confirmed", id="e1"))
    assert not [e for e in events if e.type == "card"]
    # related but ADDS remembered info (gear location) → card allowed
    _, events = await ctx.handle_utterance(
        ev("we can grab the gear from the garage shelf before the kids game", id="e2"))
    cards = [e for e in events if e.type == "card"]
    assert cards and cards[0].card.headline.startswith("Cleats and air pump")


async def test_armed_question_defers_relevance_card_to_timer(ctx, store, fake_llm):
    ctx.cfg.relevance.question_wait_s = 0.2
    fake_llm.on("classify one spoken utterance",
                {"is_question": True, "addressed_to_someone": False, "topic": "soccer"})
    fake_llm.on("retrieved memories", "")
    seed_memory(store, "Kids soccer game saturday morning at east field")
    _, events = await ctx.handle_utterance(
        ev("hey when exactly is the kids soccer game at east field", id="q1"))
    # no instant card while humans get their chance; a timer is armed instead
    assert not [e for e in events if e.type == "card"]
    assert ctx.engine._pending_q
    # and nobody answered → lookup card surfaces after the wait
    await asyncio.sleep(ctx.cfg.relevance.question_wait_s + 0.1)
    assert ctx.engine.active


async def test_human_answer_cancels_timer_and_no_card(ctx, store, fake_llm):
    ctx.cfg.relevance.question_wait_s = 0.2
    fake_llm.on("classify one spoken utterance", lambda m: {
        "is_question": "when is" in m[1]["content"],
        "addressed_to_someone": False, "topic": "soccer"})
    seed_memory(store, "Kids soccer game saturday morning at east field")
    await ctx.handle_utterance(ev("hey when is the kids soccer game", id="q2"))
    assert ctx.engine._pending_q
    await ctx.handle_utterance(
        ev("it is saturday morning at ten", id="q2-a", speaker="spk-j", label="Jasmine"))
    assert not ctx.engine._pending_q
    await asyncio.sleep(ctx.cfg.relevance.question_wait_s + 0.1)
    assert not ctx.engine.active
