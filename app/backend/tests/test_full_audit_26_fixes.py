"""Comprehensive regression suite covering the verified 26-item audit fixes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import stat
import sys
import zipfile

from fastapi import HTTPException
import pytest

from app import mcp_server, release_resources, runtime, transport
from app.adapters import host_attribution, opencode
from app.database import create_schema, db, set_app_setting
from app.frontend_release import _digest_tree
from app.integrations import installer, templates
from app.integrations.config_transactions import ConfigurationError, ConfigurationTransactions
from app.integrations.secret_store import SecretUnavailable
from app.security import authenticated_agent
from app.services import (
    conflict_service,
    connection_service,
    content_service,
    conversation_service,
    handoff_page_service,
    handoff_service,
    project_service,
    retrieval_service,
    usage_service,
    working_memory_service,
)
from app.services.management_service import ManagementService


class _FakeVault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.next_id = 0

    def put(self, value: str) -> str:
        self.next_id += 1
        ref = f"keychain:senrelay:{self.next_id:032x}"
        self.values[ref] = value
        return ref

    def get(self, ref: str) -> str:
        if ref not in self.values:
            raise SecretUnavailable("locked")
        return self.values[ref]

    def delete(self, ref: str) -> None:
        self.values.pop(ref, None)


def _init_project_and_task(tmp_path: Path, *, execution_mode: str = "approved") -> tuple[dict, dict, dict, Path]:
    create_schema()
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    proj = project_service.create_project(name="audit-proj", directories=[str(repo)])
    conv = conversation_service.create_conversation(
        agent="cursor",
        title="Audit Conversation",
        project_id=proj["id"],
        cwd=str(repo),
        messages=[
            {"role": "user", "content": "Inspect and prepare module cleanly"},
            {"role": "assistant", "content": "Checked structure and waiting for confirmation."},
        ],
    )
    task = working_memory_service.upsert_working_task(
        title="Implement verified fix",
        project_id=proj["id"],
        cwd=str(repo),
        status="active",
        execution_mode=execution_mode,
        source_conversation_id=conv["id"],
    )
    return proj, conv, task, repo


# --- Step 1 (1.1 - 1.4) ---


@pytest.mark.parametrize("mode", ["stale", "discussion_only"])
def test_1_1_bootstrap_execution_gate_blocks_stale_and_discussion_handoffs(tmp_path: Path, mode: str) -> None:
    proj, conv, task, repo = _init_project_and_task(tmp_path, execution_mode="approved")
    if mode == "discussion_only":
        discuss_task = working_memory_service.upsert_working_task(
            title="Discuss Only Task",
            project_id=proj["id"],
            cwd=str(repo),
            status="active",
            execution_mode="discussion_only",
            source_conversation_id=conv["id"],
        )
        handoff = handoff_service.create_handoff(
            project_id=proj["id"],
            target_agent="cursor",
            task_id=discuss_task["id"],
            conversation_id=conv["id"],
            query="continue work",
        )
        handoff_service.confirm_handoff(handoff["id"], confirmed=True, actor="cursor")
        boot = retrieval_service.bootstrap_working_memory(
            cwd=str(repo),
            project_id=proj["id"],
            task_id=task["id"],
            consumer_id="cursor",
            query="continue",
        )
    else:
        handoff = handoff_service.create_handoff(
            project_id=proj["id"],
            target_agent="cursor",
            task_id=task["id"],
            conversation_id=conv["id"],
            query="continue work",
        )
        handoff_service.confirm_handoff(handoff["id"], confirmed=True, actor="cursor")
        with db() as conn:
            conn.execute("UPDATE conversations SET content_hash = 'stale-hash' WHERE id = ?", (conv["id"],))
        boot = retrieval_service.bootstrap_working_memory(
            cwd=str(repo),
            project_id=proj["id"],
            consumer_id="cursor",
            query="continue",
        )

    cont = handoff_service.lookup_continuation(project_id=proj["id"], query="continue", consumer_id="cursor") or {}
    assert cont.get("status") == mode
    assert cont.get("must_not_execute") is True
    assert boot["selected_task"]["execution_mode"] == "approved"
    assert boot["must_not_execute"] is True


def test_1_2_continuation_contract_forces_discussion_only_on_stale_and_cancelled(tmp_path: Path) -> None:
    proj, conv, task, _ = _init_project_and_task(tmp_path, execution_mode="approved")
    handoff = handoff_service.create_handoff(
        project_id=proj["id"],
        target_agent="cursor",
        task_id=task["id"],
        conversation_id=conv["id"],
        query="resume",
    )
    handoff_service.confirm_handoff(handoff["id"], confirmed=True, actor="cursor")

    # Mutate source conversation hash AFTER confirm -> validate_source_revisions marks stale
    with db() as conn:
        conn.execute("UPDATE conversations SET content_hash = 'diverged-revision' WHERE id = ?", (conv["id"],))
    stale_contract = handoff_service.continuation_contract(handoff_service.get_handoff(handoff["id"]), consumer_id="cursor")
    assert stale_contract["status"] == "stale"
    assert stale_contract["must_not_execute"] is True
    assert stale_contract["execution_mode"] == "discussion_only"

    # Cancelled handoff also forces discussion_only even though task execution_mode is approved
    handoff2 = handoff_service.create_handoff(
        project_id=proj["id"],
        target_agent="cursor",
        task_id=task["id"],
        conversation_id=conv["id"],
        query="resume 2",
    )
    handoff_service.cancel_handoff(handoff2["id"])
    cancelled_contract = handoff_service.continuation_contract(handoff_service.get_handoff(handoff2["id"]), consumer_id="cursor")
    assert cancelled_contract["status"] == "cancelled"
    assert cancelled_contract["must_not_execute"] is True
    assert cancelled_contract["execution_mode"] == "discussion_only"


def test_1_3_mcp_save_memory_does_not_self_authorize_user_correction(tmp_path: Path) -> None:
    proj, _, _, repo = _init_project_and_task(tmp_path)
    original = content_service.create_content(
        title="ORM Choice",
        content="Use SQLite raw driver across backend.",
        project_id=proj["id"],
        category="decision",
        origin_authority="user_explicit",
    )
    tok = authenticated_agent.set("cursor")
    try:
        res = mcp_server.save_memory(
            project_id=proj["id"],
            title="ORM Choice",
            content="改为使用 ORM 封装所有查询",
            category="preference",
            supersedes_id=original["id"],
            origin_authority="user_explicit",
            is_user_correction=True,
            cwd=str(repo),
        )
    finally:
        authenticated_agent.reset(tok)

    assert res["action"] == "conflict_recorded"
    assert content_service.get_content(original["id"])["status"] == "active"
    resolved = conflict_service.resolve_conflict(res["conflict_id"], "replace")
    assert resolved["status"] == "resolved"
    assert content_service.get_content(original["id"])["content"] == "改为使用 ORM 封装所有查询"


def test_1_4_legacy_task_sync_respects_discussion_only_cues(tmp_path: Path) -> None:
    create_schema()
    repo = tmp_path / "repo"
    repo.mkdir()
    proj = project_service.create_project(name="legacy-sync", directories=[str(repo)])
    c_discuss = content_service.create_content(
        title="先别改代码，只讨论拆分方案",
        content="讨论架构边界",
        project_id=proj["id"],
        category="task",
        tags=["open"],
    )
    c_normal = content_service.create_content(
        title="Implement pagination helper",
        content="Add cursor pagination",
        project_id=proj["id"],
        category="task",
        tags=["open"],
    )
    with db() as conn:
        synced = working_memory_service.ensure_legacy_tasks_synced(proj["id"], conn=conn)
        rows = {
            r["title"]: r["execution_mode"]
            for r in conn.execute("SELECT title, execution_mode FROM working_tasks WHERE project_id = ?", (proj["id"],))
        }
    assert synced >= 2
    assert rows[c_discuss["title"]] == "discussion_only"
    assert rows[c_normal["title"]] == "approved"


# --- Step 2 (2.1 - 2.3) ---


def test_2_1_relay_identifier_fragment_scoped_to_current_candidates(tmp_path: Path) -> None:
    create_schema()
    repo = tmp_path / "repo"
    repo.mkdir()
    proj = project_service.create_project(name="scope-proj", directories=[str(repo)])
    c1 = conversation_service.create_conversation(
        agent="cursor",
        title="Alpha Visible Chat",
        project_id=proj["id"],
        updated_at="2026-11-16T10:00:00Z",
    )
    conversation_service.create_conversation(
        agent="cursor",
        title="Zeta Hidden Unique Fragment",
        project_id=proj["id"],
        updated_at="2026-11-16T09:00:00Z",
    )

    candidates = [{"id": c1["id"], "title": c1["title"], "index": 1}]
    assert handoff_service._resolve_relay_conversation_identifier("Zeta Hidden", candidates) is None
    matched = handoff_service._resolve_relay_conversation_identifier("Alpha Visible", candidates)
    assert matched is not None and matched["id"] == c1["id"]

    # Without an active discovery snapshot, a unique global fragment still must NOT silently load
    with pytest.raises(HTTPException) as exc_direct:
        handoff_service.relay_conversation("Zeta Hidden", consumer="omp", sync_first=False)
    assert exc_direct.value.status_code == 404

    # With an active discovery snapshot limited to Alpha Visible Chat, Zeta Hidden is rejected (404)
    handoff_service.discover_relay_conversations(
        consumer="cursor", project_id=proj["id"], limit=1, sync_first=False, link_unlinked=False,
    )
    with pytest.raises(HTTPException) as exc_snap:
        handoff_service.relay_conversation("Zeta Hidden", consumer="cursor", project_id=proj["id"], sync_first=False)
    assert exc_snap.value.status_code == 404

    # Repeated exact title without snapshot still raises 422 ambiguous_conversation before syncing
    conversation_service.create_conversation(agent="cursor", title="Repeated opening", project_id=proj["id"])
    conversation_service.create_conversation(agent="antigravity", title="Repeated opening", project_id=proj["id"])
    with pytest.raises(HTTPException) as exc_dup:
        handoff_service.relay_conversation("Repeated opening", consumer="omp", sync_first=False)
    assert exc_dup.value.status_code == 422
    assert exc_dup.value.detail["code"] == "ambiguous_conversation"


def test_2_2_handoff_page_in_place_edit_and_elided_coverage_accounting(tmp_path: Path, monkeypatch) -> None:
    create_schema()
    set_app_setting("compute_authorized", True)
    set_app_setting("compute_host", "omp")
    with db() as conn:
        from app.database import now_iso
        now = now_iso()
        conn.execute(
            "INSERT INTO connections (id,label,discovered,enabled,paused,paths,capabilities,created_at,updated_at) "
            "VALUES ('omp','OMP',1,1,0,'[]','{}',?,?) "
            "ON CONFLICT(id) DO UPDATE SET enabled=1, paused=0, updated_at=excluded.updated_at",
            (now, now),
        )
    repo = tmp_path / "repo"
    repo.mkdir()
    proj = project_service.create_project(name="hp-proj", directories=[str(repo)])
    msgs = [
        {"role": "user", "content": f"msg-{i}-" + ("x" * 100)}
        for i in range(10)
    ]
    conv = conversation_service.upsert_conversation(
        agent="cursor",
        title="Handoff Page Edits",
        project_id=proj["id"],
        external_id="cursor:hp-edit",
        cwd=str(repo),
        messages=msgs,
    )
    seen_payloads: list[dict] = []

    def fake_distill(_host_id, payload, **_kwargs):
        seen_payloads.append(payload)
        return transport.DistillResult(
            status="success",
            structured={"goal": "g", "done": "d", "do_not_redo": "r", "unverified": "u", "next_step": "n"},
            summary="n",
        )

    monkeypatch.setattr(transport, "run_distill_sync", fake_distill)
    first = handoff_page_service.ensure_handoff_page(conversation_service.get_conversation(conv["id"], include_messages=True))
    assert first["covered_message_count"] == 10

    # Same message count, but message 0 edited -> must feed from index 0, not last 6!
    edited_msgs = list(msgs)
    edited_msgs[0] = {"role": "user", "content": "msg-0-edited-in-place"}
    updated = conversation_service.upsert_conversation(
        agent="cursor",
        title="Handoff Page Edits",
        project_id=proj["id"],
        external_id="cursor:hp-edit",
        cwd=str(repo),
        messages=edited_msgs,
    )
    handoff_page_service.ensure_handoff_page(updated)
    assert len(seen_payloads) == 2
    assert seen_payloads[1]["messages"][0]["index"] == 0
    assert seen_payloads[1]["messages"][0]["content"] == "msg-0-edited-in-place"

    # Oversized message 0 gets middle-elided -> incomplete_indices_out={0}, covered_message_count=0
    huge_msgs = [
        {"role": "user", "content": "A" * 130_000},
        {"role": "assistant", "content": "short reply"},
    ]
    incomplete: set[int] = set()
    _prepared, _ = handoff_page_service._prepare_messages_for_model(huge_msgs, start_index=0, incomplete_indices_out=incomplete)
    assert 0 in incomplete

    conv2 = conversation_service.upsert_conversation(
        agent="cursor",
        title="Huge Message Page",
        project_id=proj["id"],
        external_id="cursor:hp-huge",
        cwd=str(repo),
        messages=huge_msgs,
    )
    huge_page = handoff_page_service.ensure_handoff_page(conversation_service.get_conversation(conv2["id"], include_messages=True))
    assert huge_page["covered_message_count"] == 0


def test_2_3_host_attribution_requires_valid_claude_root(tmp_path: Path) -> None:
    outside = "/tmp/outside/.claude/projects/fake/sess.jsonl"
    assert host_attribution.is_claude_code_transcript_path(outside) is False
    assert host_attribution.detect_foreign_hook_host("claude-code", {"transcript_path": outside}) == "outside-claude-root"

    valid = str(Path.home() / ".claude" / "projects" / "demo" / "sess.jsonl")
    assert host_attribution.is_claude_code_transcript_path(valid) is True
    assert host_attribution.detect_foreign_hook_host("claude-code", {"transcript_path": valid}) is None


# --- Step 3 (3.1 - 3.4) ---


def test_3_1_config_transactions_and_claudecode_activation_preserve_user_tokens(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "user-native-secret-token"}}))

    tx = ConfigurationTransactions(tmp_path / "db.sqlite", tmp_path / "state", [home], _FakeVault())
    with pytest.raises(ConfigurationError):
        tx.create([{
            "path": str(settings_path),
            "kind": "json",
            "pointer": ["env", "ANTHROPIC_AUTH_TOKEN"],
            "owner": "activation:claude-code",
            "operation": "uninstall",
            "takeover_selected_field": True,
        }])
    assert json.loads(settings_path.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "user-native-secret-token"

    manager = ManagementService(tmp_path / "mgmt.sqlite", home, _FakeVault())
    provider = manager.save("provider", {
        "name": "NoKeyRelay",
        "protocol": "anthropic",
        "base_url": "https://relay.example.test",
        "model_map": {"default": "claude-sonnet-4-5"},
    })
    plan = manager.plan({"host_id": "claude-code", "provider_id": provider["id"], "model_id": "default", "activate_provider": True})
    assert not any(
        f.get("operation") == "uninstall" and ["env", "ANTHROPIC_AUTH_TOKEN"] == f.get("pointer")
        for f in plan.get("files", [])
    )


def test_3_2_clean_hook_entry_preserves_siblings_and_hook_files_0600(tmp_path: Path) -> None:
    owned_cmd = "python3 ~/.contextcore/hooks/cc_hook.py claude-code stop"
    foreign_cmd = "echo keep-foreign-sibling"
    entry = {
        "matcher": "",
        "hooks": [
            {"type": "command", "command": owned_cmd},
            {"type": "command", "command": foreign_cmd},
        ],
    }
    cleaned, changed = installer._clean_hook_entry(entry, {owned_cmd})
    assert changed is True
    assert cleaned is not None and cleaned["hooks"] == [{"type": "command", "command": foreign_cmd}]

    # Top-level owned command with surviving nested foreign hooks keeps the nested foreign hooks cleanly
    mixed = {
        "command": owned_cmd,
        "hooks": [{"type": "command", "command": foreign_cmd}],
    }
    cleaned_mixed, changed_mixed = installer._clean_hook_entry(mixed, {owned_cmd})
    assert changed_mixed is True
    assert cleaned_mixed is not None and "command" not in cleaned_mixed
    assert cleaned_mixed["hooks"] == [{"type": "command", "command": foreign_cmd}]

    home = tmp_path / "isolated_hook_home"
    home.mkdir()
    res = installer.apply_host_integration("opencode", token="tok-123", confirmed=True, home=home, components=["hooks"])
    assert res.status == "installed"
    hook_file = Path(res.hooks_path)
    assert hook_file.exists()
    assert stat.S_IMODE(hook_file.stat().st_mode) == 0o600


def test_3_3_base_url_env_strips_credentials_query_and_fragment(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXTCORE_BASE_URL", "http://alice:s3cr3t@127.0.0.1:8420/?token=leak#frag")
    resolved = connection_service.resolve_trusted_base_url()
    assert resolved == "http://127.0.0.1:8420"

    mcp_cfg = templates.render_mcp_config("cursor", base_url=resolved, token="tok")
    hooks_cfg = templates.render_hooks_config("claude-code", hook_runner_path="/tmp/cc_hook.py", base_url=resolved)
    dumped = json.dumps({"mcp": mcp_cfg, "hooks": hooks_cfg})
    for leaked in ("alice", "s3cr3t", "leak", "frag"):
        assert leaked not in dumped


def test_3_4_runtime_cmdline_and_plist_data_dir_guards(tmp_path: Path) -> None:
    assert runtime._is_contextcore_service_cmdline("python -m uvicorn app.main:app --port 8000") is False
    assert runtime._is_contextcore_service_cmdline("/venv/bin/python -m app.cli serve --contextcore") is True

    plist = tmp_path / "com.contextcore.service.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "com.contextcore.service",
        "ProgramArguments": ["/usr/bin/python3", "-m", "app.cli", "serve"],
    }, fmt=plistlib.FMT_XML))
    ok, _ = runtime.verify_plist_ownership(plist, expected_data_dir=tmp_path / "data-dir")
    assert ok is False


# --- Step 4 (4.1 - 4.6) ---


def test_4_1_reconstruct_project_concurrent_calls_never_duplicate_directories(tmp_path: Path) -> None:
    create_schema()
    repo = tmp_path / "shared-repo"
    repo.mkdir()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: project_service.reconstruct_or_resolve_project(str(repo), {"name": "Shared"}), range(4)))
    assert len(set(results)) == 1
    with db() as conn:
        rows = conn.execute("SELECT project_id FROM project_directories WHERE path = ?", (str(repo.resolve()),)).fetchall()
    assert len(rows) == 1


def test_4_2_split_project_migrates_conflicts_and_handoffs(tmp_path: Path) -> None:
    proj, conv, task, _ = _init_project_and_task(tmp_path)
    mem = content_service.create_content(title="Split Mem", content="Detail", project_id=proj["id"])
    conflict = conflict_service.create_conflict(proj["id"], mem["id"], {"title": "Split Mem", "content": "New"}, "conflict")
    handoff = handoff_service.create_handoff(project_id=proj["id"], target_agent="cursor", task_id=task["id"], conversation_id=conv["id"], query="split")

    new_proj = project_service.split_project(proj["id"], "Child Project", conversation_ids=[conv["id"]], content_ids=[mem["id"]])
    with db() as conn:
        c_row = conn.execute("SELECT project_id FROM conflicts WHERE id = ?", (conflict["id"],)).fetchone()
        h_row = conn.execute("SELECT project_id FROM handoffs WHERE id = ?", (handoff["id"],)).fetchone()
    assert c_row["project_id"] == new_proj["id"]
    assert h_row["project_id"] == new_proj["id"]


def test_4_3_resolve_conflict_preserves_existing_quote_and_avoids_ambiguous_match(tmp_path: Path) -> None:
    proj, conv1, _, repo = _init_project_and_task(tmp_path)
    conv2 = conversation_service.create_conversation(
        agent="cursor",
        title="Unique Quote Conversation",
        project_id=proj["id"],
        cwd=str(repo),
        messages=[{"role": "user", "content": "unique-second-source-quote-xyz"}],
    )
    mem = content_service.create_content(
        title="Protocol Decision",
        content="Initial",
        project_id=proj["id"],
        source_id=conv1["id"],
        source_kind="conversation",
        quote="original-conv1-quote",
    )
    c_unique = conflict_service.create_conflict(
        proj["id"],
        mem["id"],
        {"title": "Protocol Decision", "content": "Updated", "quote": "unique-second-source-quote-xyz"},
        "overlap",
    )
    conflict_service.resolve_conflict(c_unique["id"], "merge", merged_title="Protocol Decision", merged_content="Merged")

    with db() as conn:
        sources = [dict(r) for r in conn.execute("SELECT source_id, quote FROM content_sources WHERE content_id = ?", (mem["id"],)).fetchall()]
    assert any(s["source_id"] == conv1["id"] and s["quote"] == "original-conv1-quote" for s in sources)
    assert any(s["source_id"] == conv2["id"] and s["quote"] == "unique-second-source-quote-xyz" for s in sources)

    # Ambiguous substring shared across 2 conversations without explicit source_id must not attach blindly
    conversation_service.create_conversation(
        agent="cursor",
        title="Dup Quote 1",
        project_id=proj["id"],
        messages=[{"role": "user", "content": "shared-ambiguous-phrase"}],
    )
    conversation_service.create_conversation(
        agent="cursor",
        title="Dup Quote 2",
        project_id=proj["id"],
        messages=[{"role": "user", "content": "shared-ambiguous-phrase"}],
    )
    c_ambig = conflict_service.create_conflict(
        proj["id"],
        mem["id"],
        {"title": "Protocol Decision", "content": "Merged 2", "category": "knowledge", "quote": "shared-ambiguous-phrase"},
        "overlap",
    )
    conflict_service.resolve_conflict(c_ambig["id"], "replace")
    with db() as conn:
        after_sources = [dict(r) for r in conn.execute("SELECT source_id, quote FROM content_sources WHERE content_id = ?", (mem["id"],)).fetchall()]
    assert not any(s["quote"] == "shared-ambiguous-phrase" for s in after_sources)


def test_4_5_blackhole_repair_persists_origin_marker_for_retry(tmp_path: Path, monkeypatch) -> None:
    create_schema()
    fake_home = (tmp_path / "user-home").resolve()
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    blackhole = project_service.create_project(name=fake_home.name, description="", directories=[str(fake_home)], manual=False)
    conversation_service.create_conversation(agent="cursor", title="Chat in home", project_id=blackhole["id"], cwd=str(fake_home / "sub"))

    res = project_service.repair_misclassified_projects()
    assert res["archived_blackholes"] >= 1
    with db() as conn:
        row = conn.execute("SELECT description, status FROM projects WHERE id = ?", (blackhole["id"],)).fetchone()
        assert row["status"] == "archived"
        assert str(fake_home) in row["description"]

    # Simulate true mid-repair crash after stamping origin description & detaching directories, before archive step
    bh_interrupted = project_service.create_project(name=fake_home.name, description="", directories=[str(fake_home)], manual=False)
    other_repo = (tmp_path / "other-real-repo").resolve()
    other_repo.mkdir()
    conversation_service.create_conversation(agent="cursor", title="Interrupted Chat", project_id=bh_interrupted["id"], cwd=str(other_repo))

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash")

    with monkeypatch.context() as mp:
        mp.setattr(project_service, "reconstruct_or_resolve_project", boom)
        with pytest.raises(RuntimeError, match="simulated crash"):
            project_service.repair_misclassified_projects()

    with db() as conn:
        mid_row = conn.execute("SELECT description, status FROM projects WHERE id = ?", (bh_interrupted["id"],)).fetchone()
        mid_dirs = conn.execute("SELECT path FROM project_directories WHERE project_id = ?", (bh_interrupted["id"],)).fetchall()
        assert mid_row["status"] == "active"
        assert mid_row["description"].startswith("自动从工作目录还原:")
        assert str(fake_home) in mid_row["description"]
        assert mid_dirs == []

    res2 = project_service.repair_misclassified_projects()
    assert res2["archived_blackholes"] >= 1
    with db() as conn:
        assert conn.execute("SELECT status FROM projects WHERE id = ?", (bh_interrupted["id"],)).fetchone()["status"] == "archived"


def test_4_6_opencode_falls_back_to_legacy_sessions_on_empty_or_corrupt_db(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    root.mkdir()
    # Empty valid opencode.db + legacy JSON session
    db_file = root / "opencode.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT)")
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER)")
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.commit()
    conn.close()

    legacy_dir = root / "storage" / "session" / "proj1"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "ses_legacy_1.json").write_text(json.dumps({
        "id": "ses_legacy_1",
        "title": "Legacy OpenCode Session",
        "directory": str(tmp_path),
    }))
    msg_dir = root / "storage" / "message" / "ses_legacy_1"
    msg_dir.mkdir(parents=True)
    (msg_dir / "msg1.json").write_text(json.dumps({
        "id": "msg1",
        "role": "user",
        "time": {"created": 1700000000000},
    }))
    part_dir = root / "storage" / "part" / "msg1"
    part_dir.mkdir(parents=True)
    (part_dir / "part1.json").write_text(json.dumps({
        "id": "part1",
        "type": "text",
        "text": "hello legacy",
    }))

    sessions = list(opencode.iter_opencode_sessions([str(root)]))
    assert len(sessions) == 1
    assert sessions[0]["external_id"] == "opencode:ses_legacy_1"

    # Corrupt opencode.db still yields legacy sessions before raising PartialHistoryError
    db_file.write_bytes(b"not a valid sqlite database")
    collected: list[dict] = []
    it = opencode.iter_opencode_sessions([str(root)])
    with pytest.raises(opencode.PartialHistoryError):
        for item in it:
            collected.append(item)
    assert len(collected) == 1 and collected[0]["external_id"] == "opencode:ses_legacy_1"


# --- Step 5 backend (5.6) ---


def test_5_6_usage_ingest_422_includes_field_name(tmp_path: Path) -> None:
    create_schema()
    with pytest.raises(HTTPException) as exc_ts:
        usage_service.ingest([{
            "origin": "proxy",
            "observation_id": "obs-bad-ts",
            "status": "succeeded",
            "started_at": "2026-01-01T00:00:00",
        }])
    assert exc_ts.value.status_code == 422
    assert exc_ts.value.detail.startswith("started_at:")

    with pytest.raises(HTTPException) as exc_tok:
        usage_service.ingest([{
            "origin": "proxy",
            "observation_id": "obs-bad-tok",
            "status": "succeeded",
            "started_at": "2026-01-01T00:00:00Z",
            "raw_usage": {"input_tokens": -1},
        }])
    assert exc_tok.value.status_code == 422
    assert exc_tok.value.detail.startswith("input_tokens:")


# --- Step 6 (6.1 - 6.3) ---


def test_6_2_and_6_3_release_resources_and_wheel_exact_member_check(tmp_path: Path) -> None:
    backend_copy = tmp_path / "backend"
    for pkg in ("app", "app/adapters", "app/integrations", "app/integrations/skills", "app/routers", "app/services", "app/services/nested"):
        (backend_copy / pkg).mkdir(parents=True, exist_ok=True)
    for rel in release_resources.RESOURCE_FILES:
        target = backend_copy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"asset:{rel}")
    for pkg in ("app", "app/adapters", "app/integrations", "app/routers", "app/services", "app/services/nested"):
        (backend_copy / pkg / "__init__.py").write_text("")
    (backend_copy / "app/services/nested/deep.py").write_text("VALUE = 1\n")
    (backend_copy / "app/configuration-snapshots").mkdir(parents=True)
    (backend_copy / "app/configuration-snapshots/secret.py").write_text("SECRET = 1\n")

    collected = {p.relative_to(backend_copy).as_posix() for p in release_resources.source_files(backend_copy)}
    assert "app/services/nested/deep.py" in collected
    assert "app/configuration-snapshots/secret.py" not in collected

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts_dir))
    import check_release_readiness

    good_whl = tmp_path / "good.whl"
    with zipfile.ZipFile(good_whl, "w") as zf:
        for path in release_resources.source_files(backend_copy):
            zf.write(path, path.relative_to(backend_copy).as_posix())
    with zipfile.ZipFile(good_whl) as zf:
        check_release_readiness.verify_wheel_members_match_source(zf, backend_copy, release_resources.RESOURCE_FILES)

    # Stale local setuptools build products must never be reintroduced.
    with zipfile.ZipFile(good_whl, "a") as zf:
        zf.writestr("app/services/retired_feature.py", b"# old source")
    with zipfile.ZipFile(good_whl) as zf:
        with pytest.raises(AssertionError, match="Unexpected app files"):
            check_release_readiness.verify_wheel_members_match_source(zf, backend_copy, release_resources.RESOURCE_FILES)

    # Suffix-only spoofed path must fail
    bad_suffix_whl = tmp_path / "bad_suffix.whl"
    with zipfile.ZipFile(bad_suffix_whl, "w") as zf:
        for path in release_resources.source_files(backend_copy):
            rel = path.relative_to(backend_copy).as_posix()
            zf.write(path, f"prefix/{rel}" if rel.endswith("codex_skill.md") else rel)
    with zipfile.ZipFile(bad_suffix_whl) as zf:
        with pytest.raises(AssertionError, match="missing exact"):
            check_release_readiness.verify_wheel_members_match_source(zf, backend_copy, release_resources.RESOURCE_FILES)

    # Tampered bytes in wheel member must fail
    bad_bytes_whl = tmp_path / "bad_bytes.whl"
    with zipfile.ZipFile(bad_bytes_whl, "w") as zf:
        for path in release_resources.source_files(backend_copy):
            rel = path.relative_to(backend_copy).as_posix()
            if rel.endswith("codex_skill.md"):
                zf.writestr(rel, b"tampered skill content")
            else:
                zf.write(path, rel)
    with zipfile.ZipFile(bad_bytes_whl) as zf:
        with pytest.raises(AssertionError, match="bytes differ"):
            check_release_readiness.verify_wheel_members_match_source(zf, backend_copy, release_resources.RESOURCE_FILES)


def test_6_1_validate_bundle_checks_resources_manifest_and_backend_sha(tmp_path: Path) -> None:
    import importlib.util

    entry_path = Path(__file__).resolve().parents[2] / "macos" / "backend_entry.py"
    spec = importlib.util.spec_from_file_location("audit_backend_entry", entry_path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)

    resources = tmp_path / "Contents" / "Resources"
    root = resources / "service" / "_internal"
    (root / "frontend").mkdir(parents=True)
    (root / "frontend" / "index.html").write_text("ui")
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("ok\n")
    manifest = {
        "release_id": "rel-1",
        "api_contract": 2,
        "frontend_sha256": _digest_tree(root / "frontend"),
        "backend_sha256": hashlib.sha256(b"src").hexdigest(),
        "backend_runtime_sha256": entry.compute_backend_runtime_sha256(root),
    }
    (root / "desktop-release.json").write_text(json.dumps(manifest))
    (resources / "desktop-release.json").write_text(json.dumps(manifest))
    assert entry.validate_bundle(root)["release_id"] == "rel-1"

    # Mismatched outer Resources/desktop-release.json
    (resources / "desktop-release.json").write_text(json.dumps({**manifest, "release_id": "other"}))
    with pytest.raises(ValueError, match="mismatch"):
        entry.validate_bundle(root)
    (resources / "desktop-release.json").write_text(json.dumps(manifest))

    # Tampered backend_runtime_sha256
    (root / "desktop-release.json").write_text(json.dumps({**manifest, "backend_runtime_sha256": "0" * 64}))
    with pytest.raises(ValueError, match="backend integrity"):
        entry.validate_bundle(root)

    # Missing/invalid backend_sha256
    (root / "desktop-release.json").write_text(json.dumps({**manifest, "backend_sha256": ""}))
    with pytest.raises(ValueError, match="backend release digest"):
        entry.validate_bundle(root)

