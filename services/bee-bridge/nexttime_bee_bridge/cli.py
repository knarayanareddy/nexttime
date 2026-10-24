from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexttime_bee_adapter import LocalSpoolBeeAdapter
from nexttime_domain import (
    CaptureMode,
    ConsentGrant,
    SeparationMethod,
    Situation,
    rubric_from_mapping,
)
from nexttime_minimization import DeletionPipelineWorker, InMemoryArtifactStore

from .listener import BeeIngestionBridge, CaptureMetadata


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexttime-bee-bridge",
        description="Local consented Bee transcript ingestion listener (fixture/spool or CLI).",
    )
    parser.add_argument(
        "--spool-dir",
        required=True,
        help="Local directory of Bee transcript JSONs",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Learner consent and capture-mode manifest JSON path",
    )
    parser.add_argument(
        "--rubric",
        default=str(
            Path(__file__).resolve().parents[3] / "packages" / "rubrics" / "rubric_v1.json"
        ),
        help="Path to rubric JSON",
    )
    return parser


async def _execute_cli(args: argparse.Namespace) -> dict[str, Any]:
    manifest_data = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    rubric_data = json.loads(Path(args.rubric).read_text(encoding="utf-8"))
    rubric = rubric_from_mapping(rubric_data)

    store = InMemoryArtifactStore()
    worker = DeletionPipelineWorker(store)
    adapter = LocalSpoolBeeAdapter(Path(args.spool_dir))
    bridge = BeeIngestionBridge(
        adapter=adapter,
        store=store,
        deletion_worker=worker,
        rubric=rubric,
    )

    learner_id = str(manifest_data["learner_id"])
    for raw_consent in manifest_data.get("consents", []):
        grant = ConsentGrant.create(
            consent_id=str(raw_consent["consent_id"]),
            learner_id=learner_id,
            capture_mode=CaptureMode(str(raw_consent["capture_mode"])),
            policy_version=str(raw_consent.get("policy_version", "consent_v1")),
            all_participants_consented=bool(raw_consent["all_participants_consented"]),
            attested_at=datetime.fromisoformat(
                str(raw_consent["attested_at"]).replace("Z", "+00:00")
            ),
        )
        if raw_consent.get("revoked"):
            grant = grant.revoke(
                actor_learner_id=learner_id,
                revoked_at=datetime.fromisoformat(
                    str(raw_consent["revoked_at"]).replace("Z", "+00:00")
                ),
            )
        store.save_consent(grant)

    for raw_item in manifest_data.get("captures", []):
        method_str = raw_item.get("separation_method")
        bridge.register_capture_metadata(
            str(raw_item["reference"]),
            CaptureMetadata(
                learner_id=learner_id,
                consent_id=str(raw_item["consent_id"]),
                capture_mode=CaptureMode(str(raw_item["capture_mode"])),
                situation=Situation(str(raw_item["situation"])),
                learner_goal=str(raw_item["learner_goal"]),
                separation_method=(
                    SeparationMethod(str(method_str)) if method_str is not None else None
                ),
                recurrence_id=raw_item.get("recurrence_id"),
            ),
        )

    report = await bridge.sync_once(learner_id=learner_id, now=datetime.now(UTC))
    return {
        "learner_id": report.learner_id,
        "status": report.status,
        "cursor": report.cursor,
        "ingested_count": report.ingested_count,
        "duplicate_count": report.duplicate_count,
        "rejected_count": report.rejected_count,
        "attempt_ids": [o.attempt_id for o in report.outcomes if o.attempt_id is not None],
    }


def run_cli(argv: Sequence[str] | None = None) -> tuple[int, dict[str, Any]]:
    parser = _build_parser()
    parsed = parser.parse_args(list(argv) if argv is not None else None)
    summary = asyncio.run(_execute_cli(parsed))
    code = 0 if summary["status"] in {"completed", "stopped_consent_revoked"} else 1
    return code, summary


def main() -> None:
    exit_code, summary = run_cli()
    print(json.dumps(summary, indent=2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
