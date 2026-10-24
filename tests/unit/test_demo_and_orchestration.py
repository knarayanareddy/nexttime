from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from nexttime_api import create_app

from scripts.record_demo import (
    DEFAULT_OUTPUT_VIDEO,
    WEBM_EBML_MAGIC,
    build_arg_parser,
    find_free_tcp_port,
    wait_for_http_ready,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_v1_health_endpoint_matches_docker_compose_healthcheck() -> None:
    client = TestClient(create_app())
    res = client.get("/v1/health")
    assert res.status_code == 200
    assert res.json() == {
        "status": "ok",
        "service": "nexttime-api",
        "version": "0.1.0",
    }


def test_docker_compose_defines_redis_api_web_and_v1_healthcheck() -> None:
    compose_path = REPO_ROOT / "docker-compose.yml"
    assert compose_path.is_file(), "docker-compose.yml must exist at repository root"

    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = doc["services"]
    assert {"redis", "api", "web"}.issubset(set(services.keys()))

    assert "6379:6379" in services["redis"]["ports"]
    assert "--appendonly" in services["redis"]["command"]
    assert "no" in services["redis"]["command"]

    assert "8000:8000" in services["api"]["ports"]
    assert services["api"]["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "-f",
        "http://localhost:8000/v1/health",
    ]
    assert (REPO_ROOT / services["api"]["build"]["dockerfile"]).is_file()

    assert "3000:3000" in services["web"]["ports"]
    assert (REPO_ROOT / services["web"]["build"]["dockerfile"]).is_file()

    nginx_conf = (REPO_ROOT / "apps" / "web" / "nginx.conf").read_text(encoding="utf-8")
    assert "proxy_pass http://api:8000/v1/;" in nginx_conf
    assert "Upgrade $http_upgrade" in nginx_conf
    assert "/nexttime/" in nginx_conf


def test_github_pages_workflow_and_standalone_web_rehearsal_bundle() -> None:
    vite_cfg = (REPO_ROOT / "apps" / "web" / "vite.config.ts").read_text(encoding="utf-8")
    assert 'base: "/nexttime/"' in vite_cfg

    scenarios_ts = (REPO_ROOT / "apps" / "web" / "src" / "data" / "scenarios.ts").read_text(
        encoding="utf-8"
    )
    assert "examples/real-world-attempt.example.json" in scenarios_ts
    assert "shift_clarification" in scenarios_ts
    assert "repair_appointment" in scenarios_ts

    # Verify linked demo video in apps/web/public/media/nexttime-demo.webm + header view switcher
    linked_video = REPO_ROOT / "apps" / "web" / "public" / "media" / "nexttime-demo.webm"
    assert linked_video.is_symlink(), "apps/web/public/media/nexttime-demo.webm must be a symlink"
    assert linked_video.resolve() == DEFAULT_OUTPUT_VIDEO.resolve()

    linked_mp4 = REPO_ROOT / "apps" / "web" / "public" / "media" / "nexttime-demo.mp4"
    assert linked_mp4.is_symlink(), "apps/web/public/media/nexttime-demo.mp4 must be a symlink"

    captions_vtt = REPO_ROOT / "apps" / "web" / "public" / "media" / "nexttime-demo.vtt"
    assert captions_vtt.is_file()

    app_tsx = (REPO_ROOT / "apps" / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "📹 Watch 90s Walkthrough" in app_tsx
    assert "💬 Interactive Dutch Rehearsal" in app_tsx
    assert "media/nexttime-demo.webm" in app_tsx
    assert "media/nexttime-demo.mp4" in app_tsx
    assert "<video" in app_tsx

    dist_video = REPO_ROOT / "apps" / "web" / "dist" / "media" / "nexttime-demo.webm"
    if dist_video.exists():
        assert dist_video.is_file() and not dist_video.is_symlink()
        with dist_video.open("rb") as fh:
            assert fh.read(4) == WEBM_EBML_MAGIC

    dist_mp4 = REPO_ROOT / "apps" / "web" / "dist" / "media" / "nexttime-demo.mp4"
    if dist_mp4.exists():
        assert dist_mp4.is_file() and not dist_mp4.is_symlink()
        assert dist_mp4.stat().st_size > 50_000

    workflow_path = REPO_ROOT / ".github" / "workflows" / "pages.yml"
    assert workflow_path.is_file()
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    jobs = workflow["jobs"]
    assert "build" in jobs and "deploy" in jobs
    deploy_steps = jobs["deploy"]["steps"]
    assert any("actions/deploy-pages" in str(step.get("uses", "")) for step in deploy_steps)


def test_recorded_demo_webm_and_recorder_helpers() -> None:
    assert DEFAULT_OUTPUT_VIDEO.is_file(), "docs/media/nexttime-demo.webm must exist"
    size_bytes = DEFAULT_OUTPUT_VIDEO.stat().st_size
    assert size_bytes > 50_000, "Recorded WebM video appears unexpectedly small/empty"
    with DEFAULT_OUTPUT_VIDEO.open("rb") as fh:
        assert fh.read(4) == WEBM_EBML_MAGIC

    parsed = build_arg_parser().parse_args(["--width", "1280", "--height", "800"])
    assert parsed.output == DEFAULT_OUTPUT_VIDEO
    assert parsed.width == 1280
    assert parsed.height == 800

    port = find_free_tcp_port()
    assert 1024 <= port <= 65535

    with pytest.raises(ValueError, match="Refusing non-local"):
        wait_for_http_ready("https://example.com/health")

    class _OkHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    with socketserver.TCPServer(("127.0.0.1", 0), _OkHandler) as httpd:
        srv_port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.handle_request, daemon=True)
        thread.start()
        wait_for_http_ready(f"http://127.0.0.1:{srv_port}/ok", timeout_sec=5.0)
        thread.join(timeout=2.0)
