from pathlib import Path

from artmem_server.config import load_config


def write_yaml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_defaults_without_file(tmp_path):
    cfg = load_config(config_path=tmp_path / "missing.yaml", env={})
    assert cfg.relevance.gate_threshold == 0.55
    assert cfg.relevance.weights.semantic == 0.40
    assert cfg.conversations.silence_gap_s == 180.0
    assert cfg.clarifications.max_per_digest == 5
    assert cfg.storage.keep_raw_audio is False  # spec §13 default off


def test_yaml_overlay(tmp_path):
    p = write_yaml(tmp_path, """
relevance:
  gate_threshold: 0.7
  weights:
    semantic: 0.5
conversations:
  silence_gap_s: 60
""")
    cfg = load_config(config_path=p, env={})
    assert cfg.relevance.gate_threshold == 0.7
    assert cfg.relevance.weights.semantic == 0.5
    assert cfg.relevance.weights.entity_overlap == 0.20  # untouched default
    assert cfg.conversations.silence_gap_s == 60


def test_env_overrides_yaml(tmp_path):
    p = write_yaml(tmp_path, "relevance:\n  gate_threshold: 0.7\n")
    cfg = load_config(config_path=p, env={"ARTMEM_RELEVANCE__GATE_THRESHOLD": "0.9"})
    assert cfg.relevance.gate_threshold == 0.9


def test_secrets_env_only(tmp_path):
    p = write_yaml(tmp_path, "token: from-yaml\nllm:\n  api_key: yaml-secret\n")
    cfg = load_config(config_path=p, env={})
    assert cfg.token == ""            # yaml cannot set the token
    assert cfg.llm.api_key == ""      # nor the api key
    cfg = load_config(config_path=p, env={"ARTMEM_TOKEN": "t0k", "LLM_API_KEY": "sk-x"})
    assert cfg.token == "t0k"
    assert cfg.llm.api_key == "sk-x"


def test_bool_and_none_coercion(tmp_path):
    cfg = load_config(
        config_path=tmp_path / "m.yaml",
        env={"ARTMEM_STORAGE__KEEP_RAW_AUDIO": "true",
             "ARTMEM_LLM__EMBEDDING_MODEL": ""},
    )
    assert cfg.storage.keep_raw_audio is True


def test_embedding_base_url_fallback():
    cfg = load_config(config_path="none.yaml", env={})
    assert cfg.llm.resolved_embedding_base_url == cfg.llm.base_url
