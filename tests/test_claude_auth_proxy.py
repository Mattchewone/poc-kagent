import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from claude_auth_proxy import translate  # noqa: E402


def test_oauth_token_becomes_bearer_with_beta():
    out, mode = translate({"x-api-key": "sk-ant-oat01-abc", "anthropic-beta": "foo", "host": "x"})
    assert mode == "oauth"
    assert "x-api-key" not in out and "host" not in out
    assert out["authorization"] == "Bearer sk-ant-oat01-abc"
    assert out["anthropic-beta"] == "foo,oauth-2025-04-20"


def test_oauth_beta_not_duplicated():
    out, _ = translate({"x-api-key": "sk-ant-oat01-abc", "anthropic-beta": "oauth-2025-04-20"})
    assert out["anthropic-beta"] == "oauth-2025-04-20"


def test_api_key_passes_through():
    out, mode = translate({"x-api-key": "sk-ant-api03-abc"})
    assert mode == "api-key"
    assert out == {"x-api-key": "sk-ant-api03-abc"}


def test_placeholder_is_reported():
    _, mode = translate({"x-api-key": "kagent-credential-injected"})
    assert mode.startswith("placeholder")
