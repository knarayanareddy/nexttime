from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nexttime_bee_adapter import (
    BeeAdapterError,
    BeeCliAdapter,
    BeeCliConfig,
    BeeTranscript,
    LocalSpoolBeeAdapter,
    redact_bridge_log,
)
from nexttime_bee_bridge import (
    BeeBridgeLeaseError,
    BeeIngestionBridge,
    CaptureMetadata,
    run_cli,
)
from nexttime_domain import (
    CaptureMode,
    ConsentGrant,
    RawSpeakerSegment,
    Rubric,
    Situation,
)
from nexttime_minimization import DeletionPipelineWorker, InMemoryArtifactStore

LEARNER_ID = "01000000000000000000000002"
CONSENT_ID = "01000000000000000000000004"


def _write_spool_file(spool_dir: Path, name: str, payload: dict[str, object]) -> None:
    (spool_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def test_redact_bridge_log_strips_secrets_emails_phones_and_utterances() -> None:
    raw = (
        "Authorization: Bearer super-secret-token contact test@example.com +31 6 12345678\n"
        'payload={"text": "Mijn geheime zin", "speaker": "other", "reference": "bee-1"}'
    )
    redacted = redact_bridge_log(raw)
    assert "super-secret-token" not in redacted
    assert "test@example.com" not in redacted
    assert "12345678" not in redacted
    assert "Mijn geheime zin" not in redacted
    assert "bee-1" in redacted
    assert redact_bridge_log("x" * 300, max_length=32).endswith("...[truncated]")


def test_local_spool_adapter_discovers_paginates_and_acknowledges(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing"
    _write_spool_file(
        tmp_path,
        "item1.json",
        {
            "reference": "bee-spool-001",
            "recorded_at": "2026-10-24T09:00:00Z",
            "device_type": "bee_device",
            "segments": [
                {
                    "segment_id": "seg-1",
                    "speaker_label": "self",
                    "text": "Ik bel voor een reparatieafspraak.",
                    "start_ms": 0,
                    "end_ms": 2400,
                    "asr_confidence": 0.91,
                },
                {
                    "segment_id": "seg-2",
                    "speaker_label": "other",
                    "text": "Andere spreker tekst mag nooit blijven.",
                    "start_ms": 2600,
                    "end_ms": 4200,
                    "asr_confidence": 0.95,
                },
            ],
        },
    )
    _write_spool_file(
        tmp_path,
        "item2.json",
        {
            "reference": "bee-spool-002",
            "recorded_at": "2026-10-24T09:05:00Z",
            "device_type": "apple_watch_bee",
            "segments": [
                {
                    "segment_id": "seg-1",
                    "speaker_label": "self",
                    "text": "Kan het vrijdag?",
                    "start_ms": 0,
                    "end_ms": 1800,
                    "asr_confidence": 0.93,
                }
            ],
        },
    )

    async def _exercise() -> None:
        offline = LocalSpoolBeeAdapter(missing_dir)
        assert (await offline.health()).status == "offline"
        assert (await offline.list_recordings(None)).items == ()

        adapter = LocalSpoolBeeAdapter(tmp_path, page_size=1)
        assert (await adapter.health()).status == "healthy"
        page1 = await adapter.list_recordings(None)
        assert len(page1.items) == 1
        assert page1.items[0].reference == "bee-spool-001"
        assert page1.items[0].provenance == "fixture"

        page2 = await adapter.list_recordings(page1.next_cursor)
        assert len(page2.items) == 1
        assert page2.items[0].reference == "bee-spool-002"
        assert (await adapter.list_recordings("zzz-end")).items == ()

        transcript = await adapter.get_transcript("bee-spool-001")
        assert len(transcript.segments) == 2

        with pytest.raises(BeeAdapterError):
            await adapter.get_transcript("nonexistent")

        await adapter.acknowledge("bee-spool-001", "bee-spool-001")
        assert "bee-spool-001" in adapter.acknowledged_references
        assert adapter.last_cursor == "bee-spool-001"

        (tmp_path / "broken.json").write_text("{not-json", encoding="utf-8")
        with pytest.raises(BeeAdapterError):
            await adapter.list_recordings(None)

    asyncio.run(_exercise())


def test_bee_cli_adapter_subprocess_json_health_timeout_and_errors(tmp_path: Path) -> None:
    helper_script = tmp_path / "fake_bee_cli.py"
    helper_script.write_text(
        "import json, sys, time\n"
        "mode = sys.argv[1]\n"
        "if mode == 'health':\n"
        "    print(json.dumps({'ok': True, 'detail': 'fixture-cli-ready'}))\n"
        "elif mode == 'list':\n"
        "    item = {'reference': 'cli-ref-1', 'recorded_at': '2026-10-24T09:00:00Z'}\n"
        "    print(json.dumps({'items': [item], 'next_cursor': 'cli-ref-1'}))\n"
        "elif mode == 'get':\n"
        "    ref = sys.argv[-1]\n"
        "    print(json.dumps({\n"
        "        'reference': ref,\n"
        "        'recorded_at': '2026-10-24T09:00:00Z',\n"
        "        'segments': [{\n"
        "            'segment_id': 's1', 'speaker_label': 'self',\n"
        "            'text': 'Goedemiddag ik wil een afspraak.',\n"
        "            'start_ms': 0, 'end_ms': 2200, 'asr_confidence': 0.9\n"
        "        }]\n"
        "    }))\n"
        "elif mode == 'ack':\n"
        "    print(json.dumps({'ok': True}))\n"
        "elif mode == 'badjson':\n"
        "    print('not-json')\n"
        "elif mode == 'big':\n"
        "    print('x' * 5000)\n"
        "elif mode == 'slow':\n"
        "    time.sleep(2.0)\n"
        "elif mode == 'fail':\n"
        "    sys.stderr.write('Bearer leaked-token\\n')\n"
        "    sys.exit(2)\n",
        encoding="utf-8",
    )

    config = BeeCliConfig(
        executable_path=sys.executable,
        health_args=(str(helper_script), "health"),
        list_args=(str(helper_script), "list"),
        transcript_args=(str(helper_script), "get"),
        acknowledge_args=(str(helper_script), "ack"),
        provenance="fixture",
    )

    async def _run() -> None:
        unconf = BeeCliAdapter(None)
        assert (await unconf.health()).status == "unconfigured"
        with pytest.raises(BeeAdapterError):
            await unconf.list_recordings(None)

        adapter = BeeCliAdapter(config)
        h = await adapter.health()
        assert h.status == "healthy"
        assert h.live_bee_verified is False

        page = await adapter.list_recordings("cur-0")
        assert page.items[0].reference == "cli-ref-1"
        tx = await adapter.get_transcript("cli-ref-1")
        assert tx.reference == "cli-ref-1"
        await adapter.acknowledge("cli-ref-1", "cli-ref-1")
        assert adapter.acknowledged_cursors["cli-ref-1"] == "cli-ref-1"

        for bad_mode, expected_code, timeout_s, max_b in (
            ("fail", "cli_nonzero_exit", 5.0, 4096),
            ("badjson", "invalid_cli_json", 5.0, 4096),
            ("big", "cli_output_exceeded_bound", 5.0, 1024),
            ("slow", "cli_timeout", 0.1, 4096),
        ):
            bad_cfg = BeeCliConfig(
                executable_path=sys.executable,
                health_args=(str(helper_script), bad_mode),
                list_args=(str(helper_script), bad_mode),
                transcript_args=(str(helper_script), "get"),
                timeout_seconds=timeout_s,
                max_output_bytes=max_b,
            )
            bad_adapter = BeeCliAdapter(bad_cfg)
            assert (await bad_adapter.health()).status == "offline"
            with pytest.raises(BeeAdapterError) as exc_info:
                await bad_adapter.list_recordings(None)
            assert exc_info.value.code == expected_code
            assert "leaked-token" not in str(exc_info.value)

    asyncio.run(_run())


def test_bridge_consent_gates_dedupe_lease_revocation_and_fail_closed_cleanup(
    tmp_path: Path, rubric: Rubric
) -> None:
    now = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    _write_spool_file(
        tmp_path,
        "a.json",
        {
            "reference": "bee-ref-a",
            "recorded_at": "2026-10-24T09:30:00Z",
            "segments": [
                {
                    "segment_id": "s1",
                    "speaker_label": "self",
                    "text": "Ik bel om een afspraak te maken voor de reparatie.",
                    "start_ms": 0,
                    "end_ms": 2900,
                    "asr_confidence": 0.92,
                },
                {
                    "segment_id": "s2",
                    "speaker_label": "other",
                    "text": "Geheime informatie van baliemedewerker.",
                    "start_ms": 3100,
                    "end_ms": 4800,
                    "asr_confidence": 0.96,
                },
            ],
        },
    )

    async def _exercise() -> None:
        store = InMemoryArtifactStore()
        worker = DeletionPipelineWorker(store)
        adapter = LocalSpoolBeeAdapter(tmp_path)
        bridge = BeeIngestionBridge(
            adapter=adapter,
            store=store,
            deletion_worker=worker,
            rubric=rubric,
        )

        # Lease contention check
        bridge._acquire_lease(learner_id=LEARNER_ID, worker_id="w1", now=now)  # noqa: SLF001
        with pytest.raises(BeeBridgeLeaseError):
            bridge._acquire_lease(  # noqa: SLF001
                learner_id=LEARNER_ID,
                worker_id="w2",
                now=now + timedelta(seconds=5),
            )
        bridge._release_lease(learner_id=LEARNER_ID, cursor=None, synced_at=now)  # noqa: SLF001

        meta = CaptureMetadata(
            learner_id=LEARNER_ID,
            consent_id=CONSENT_ID,
            capture_mode=CaptureMode.CONSENTED_CONVERSATION,
            situation=Situation.REPAIR,
            learner_goal="Plan de reparatieafspraak.",
        )
        bridge.register_capture_metadata("bee-ref-a", meta)

        # 1. Without consent -> rejected and NOT acknowledged on source!
        no_consent_report = await bridge.sync_once(learner_id=LEARNER_ID, now=now)
        assert no_consent_report.outcomes[0].status == "rejected_missing_consent"
        assert adapter.acknowledged_references == frozenset()

        # 2. Grant consent -> ingested, transient purged immediately, source acknowledged!
        store.save_consent(
            ConsentGrant.create(
                consent_id=CONSENT_ID,
                learner_id=LEARNER_ID,
                capture_mode=CaptureMode.CONSENTED_CONVERSATION,
                policy_version="consent_v1",
                all_participants_consented=True,
                attested_at=now - timedelta(minutes=10),
            )
        )
        bridge.register_capture_metadata("bee-ref-a", meta)
        ok_report = await bridge.sync_once(learner_id=LEARNER_ID, now=now)
        assert ok_report.ingested_count == 1
        attempt_id = ok_report.outcomes[0].attempt_id
        assert attempt_id is not None
        assert "bee-ref-a" in adapter.acknowledged_references
        assert not store.transient_sources
        serialized = json.dumps(store.attempts[attempt_id])
        assert "Geheime informatie" not in serialized

        # 3. Duplicate delivery of same reference -> idempotent duplicate, no 2nd attempt!
        tx = BeeTranscript(
            reference="bee-ref-a",
            recorded_at=datetime(2026, 10, 24, 9, 30, tzinfo=UTC),
            device_type="bee_device",
            provenance="fixture",
            segments=(
                RawSpeakerSegment(
                    segment_id="s1",
                    raw_speaker_label="self",
                    text="Ik bel om een afspraak te maken voor de reparatie.",
                    start_ms=0,
                    end_ms=2900,
                    asr_confidence=0.92,
                ),
                RawSpeakerSegment(
                    segment_id="s2",
                    raw_speaker_label="other",
                    text="Geheime informatie van baliemedewerker.",
                    start_ms=3100,
                    end_ms=4800,
                    asr_confidence=0.96,
                ),
            ),
        )
        dup_outcome, dup_prop = await bridge.ingest_transcript(tx, meta, ingested_at=now)
        assert dup_outcome.status == "duplicate"
        assert dup_prop is not None
        assert len(store.attempts) == 1

        # 4. Sensitive content -> fails closed and purges transient immediately!
        sensitive_tx = BeeTranscript(
            reference="bee-ref-sensitive",
            recorded_at=datetime(2026, 10, 24, 9, 45, tzinfo=UTC),
            device_type="bee_device",
            provenance="fixture",
            segments=(
                RawSpeakerSegment(
                    segment_id="s1",
                    raw_speaker_label="self",
                    text="Mijn advocaat belt over de rechtbank.",
                    start_ms=0,
                    end_ms=2400,
                    asr_confidence=0.92,
                ),
            ),
        )
        sens_outcome, _ = await bridge.ingest_transcript(sensitive_tx, meta, ingested_at=now)
        assert sens_outcome.status == "rejected_invalid_transcript"
        assert sens_outcome.error_code == "sensitive_content_excluded"
        assert not store.transient_sources

        # 5. Revoked consent -> stops ingestion and deletes prior derived attempt!
        store.save_consent(
            store.consents[CONSENT_ID].revoke(
                actor_learner_id=LEARNER_ID,
                revoked_at=now + timedelta(minutes=1),
            )
        )
        rev_tx = BeeTranscript(
            reference="bee-ref-after-revoke",
            recorded_at=datetime(2026, 10, 24, 9, 50, tzinfo=UTC),
            device_type="bee_device",
            provenance="fixture",
            segments=tx.segments,
        )
        rev_outcome, _ = await bridge.ingest_transcript(rev_tx, meta, ingested_at=now)
        assert rev_outcome.status == "stopped_consent_revoked"
        assert attempt_id not in store.attempts

    asyncio.run(_exercise())


def test_bee_bridge_cli_entrypoint(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    _write_spool_file(
        spool,
        "sample.json",
        {
            "reference": "cli-spool-01",
            "recorded_at": "2026-10-24T08:30:00Z",
            "segments": [
                {
                    "segment_id": "seg1",
                    "speaker_label": "self",
                    "text": "Ik wil een afspraak bij school.",
                    "start_ms": 0,
                    "end_ms": 2300,
                    "asr_confidence": 0.91,
                }
            ],
        },
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "learner_id": LEARNER_ID,
                "consents": [
                    {
                        "consent_id": CONSENT_ID,
                        "capture_mode": "consented_conversation",
                        "policy_version": "consent_v1",
                        "all_participants_consented": True,
                        "attested_at": "2026-10-24T08:00:00Z",
                    }
                ],
                "captures": [
                    {
                        "reference": "cli-spool-01",
                        "consent_id": CONSENT_ID,
                        "capture_mode": "consented_conversation",
                        "situation": "school_admin_appointment",
                        "learner_goal": "Maak een afspraak op school.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    exit_code, summary = run_cli(["--spool-dir", str(spool), "--manifest", str(manifest)])
    assert exit_code == 0
    assert summary["ingested_count"] == 1
    assert len(summary["attempt_ids"]) == 1
