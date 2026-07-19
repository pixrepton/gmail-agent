from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker-compose.vps.yml"
DOCKERFILE = REPO_ROOT / "docker" / "gmail-audit.Dockerfile"


def test_vps_compose_has_isolated_full_pytest_service() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "gmail-agent-worker-test:" in compose
    assert 'profiles: ["test"]' in compose or 'profiles: [ "test" ]' in compose
    feed_unit = REPO_ROOT / "deploy/systemd/topinstal-daszek-feed-push.service"
    assert feed_unit.is_file()
    assert "push_daszek_operational_feed_prod.sh" in feed_unit.read_text(encoding="utf-8")
    assert "image: gmail-agent-runtime-test:local" in compose
    assert "INSTALL_DOCLING: ${GMAIL_AGENT_TEST_INSTALL_DOCLING:-0}" in compose
    assert "INSTALL_PHP: ${GMAIL_AGENT_TEST_INSTALL_PHP:-1}" in compose
    assert "python -m pytest tools/gmail_audit/tests -q" in compose

    test_service = compose.split("gmail-agent-worker-test:", 1)[1]
    production_service = compose.split("gmail-agent-worker:", 1)[1].split("gmail-agent-worker-test:", 1)[0]
    assert "./tools/gmail_audit/.env:/app/tools/gmail_audit/.env:ro" not in test_service
    assert "GMAIL_AGENT_ENV_FILE: /app/tools/gmail_audit/.env" not in test_service
    assert "INSTALL_PHP" not in production_service


def test_gmail_audit_dockerfile_supports_php_cli_for_test_image() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG INSTALL_PHP=0" in dockerfile
    assert "php-cli" in dockerfile
