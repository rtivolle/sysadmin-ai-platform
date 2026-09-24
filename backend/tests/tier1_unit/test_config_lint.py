"""
Tier 1 Unit Test: config_lint_and_diff Validation & Unified Diff Generation.
Validates multi-format syntax parsing (JSON, YAML, systemd unit) and standard diff output.
"""
import os
import json
import pytest

from backend.services.agent_tools.tools import config_lint_and_diff
from backend.tests.fixtures import CONFIG_DIR

def test_lint_json_valid_modification():
    """Verify valid JSON modification generates unified diff."""
    target = os.path.join(CONFIG_DIR, "platform_config.json")
    proposed = """{
  "num_users": 10,
  "traefik_port": 8080,
  "max_parallel_requests": 4,
  "rpm_limit": 120,
  "daily_token_budget": 2000000
}
"""
    res = config_lint_and_diff(target_file=target, proposed_content=proposed)
    assert res["valid"] is True
    assert res["error"] is None
    assert "--- a/" in res["diff"]
    assert "+++ b/" in res["diff"]
    assert "-  \"max_parallel_requests\": 2" in res["diff"]
    assert "+  \"max_parallel_requests\": 4" in res["diff"]

def test_lint_json_malformed_trailing_comma():
    """Verify malformed JSON with trailing comma is rejected."""
    target = os.path.join(CONFIG_DIR, "platform_config.json")
    malformed = """{
  "num_users": 10,
  "traefik_port": 8080,
}
"""
    res = config_lint_and_diff(target_file=target, proposed_content=malformed)
    assert res["valid"] is False
    assert res["error"] is not None
    assert "json syntax error" in res["error"].lower()

def test_lint_json_malformed_unclosed_bracket():
    """Verify malformed JSON with unclosed bracket is rejected."""
    target = os.path.join(CONFIG_DIR, "platform_config.json")
    res = config_lint_and_diff(target_file=target, proposed_content='{"key": "value"')
    assert res["valid"] is False
    assert "json syntax error" in res["error"].lower()

def test_lint_yaml_valid_modification():
    """Verify valid YAML modification generates unified diff."""
    target = os.path.join(CONFIG_DIR, "docker-compose.aux.yml")
    proposed = """version: '3.8'
services:
  victorialogs:
    image: victoriametrics/victoria-logs:v0.25.0
    container_name: victorialogs
    ports:
      - "9428:9428"
    volumes:
      - ./backend/data/victorialogs:/vl-data
    deploy:
      resources:
        limits:
          memory: 4G
  seaweedfs:
    image: chrislusf/seaweedfs:latest
    container_name: seaweedfs
    ports:
      - "8333:8333"
      - "9333:9333"
"""
    res = config_lint_and_diff(target_file=target, proposed_content=proposed)
    assert res["valid"] is True
    assert res["error"] is None
    assert "-          memory: 2G" in res["diff"]
    assert "+          memory: 4G" in res["diff"]

def test_lint_yaml_malformed_tab_character():
    """Verify YAML with forbidden tab indentation is rejected."""
    target = os.path.join(CONFIG_DIR, "litellm_config.yaml")
    malformed_yaml = "model_list:\n\t- model_name: fast-model\n    litellm_params:\n      model: qwen\n"
    res = config_lint_and_diff(target_file=target, proposed_content=malformed_yaml)
    assert res["valid"] is False
    assert res["error"] is not None
    assert "yaml syntax error" in res["error"].lower()

def test_lint_systemd_valid():
    """Verify valid systemd unit with [Unit], [Service], [Install] passes."""
    target = os.path.join(CONFIG_DIR, "dsh-sysadmin.service")
    proposed = """[Unit]
Description=DeepSeek Harness Sysadmin Agent
After=network.target

[Service]
Type=simple
User=sysadmin
ExecStart=/usr/bin/python3 -m backend.services.agent_tools.server
ProtectSystem=strict
ProtectHome=yes
NoNewPrivileges=yes
PrivateTmp=yes
LimitNOFILE=65535
Restart=always

[Install]
WantedBy=multi-user.target
"""
    res = config_lint_and_diff(target_file=target, proposed_content=proposed)
    assert res["valid"] is True
    assert res["error"] is None
    assert "+ProtectSystem=strict" in res["diff"]
    assert "+ProtectHome=yes" in res["diff"]

def test_lint_systemd_missing_service_section():
    """Verify malformed systemd unit missing [Service] section is rejected."""
    target = os.path.join(CONFIG_DIR, "dsh-agent.service")
    broken = """# Missing all sections
Description=Agent
ExecStrt=/usr/bin/node
WantedBy=multi-user.target
"""
    res = config_lint_and_diff(target_file=target, proposed_content=broken)
    assert res["valid"] is False
    assert "systemd unit syntax error" in res["error"].lower()

def test_lint_no_changes_detected():
    """Verify identical proposed content reports '(No changes detected)'."""
    target = os.path.join(CONFIG_DIR, "platform_config.json")
    with open(target, "r") as f:
        content = f.read()
    res = config_lint_and_diff(target_file=target, proposed_content=content)
    assert res["valid"] is True
    assert res["diff"] == "(No changes detected)"
