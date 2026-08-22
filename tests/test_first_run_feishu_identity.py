from __future__ import annotations

from scripts.auto_cut_first_run import build_first_run_guide


def test_first_run_guide_requires_operator_feishu_user_identity() -> None:
    feishu = build_first_run_guide(None)["feishu"]

    assert feishu["document_read_identity"] == "user"
    assert feishu["document_read_identity_policy"] == "strict_user_only"
    commands = feishu["commands"]
    assert "lark-cli config default-as user" in commands
    assert "lark-cli config strict-mode user" in commands
    assert "lark-cli docs +fetch --as user --doc <document_url> --json" in commands
    assert all("<user|bot>" not in command for command in commands)
    assert "Never copy another computer's token" in feishu["authorization_boundary"]
