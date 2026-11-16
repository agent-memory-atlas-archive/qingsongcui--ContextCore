from __future__ import annotations

import hashlib
import inspect
import io
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.base import AgentAdapterError
from app.adapters.claude_code import normalize_claude_code_hook
from app.adapters.hooks import parse_hook
from app.database import create_schema, db, get_app_setting, seed_data
from app.integrations import cc_hook
from app.main import app
from app.services import content_service, conversation_service, handoff_service, project_service, working_memory_service
from app.services.derivation_service import ensure_schema as ensure_derivation_schema
from app.services.host_registry import get_observations


def _count_slot_files(home: Path) -> list[Path]:
    notif_dir = home / ".contextcore" / "notifications"
    if not notif_dir.exists():
        return []
    return sorted(p for p in notif_dir.iterdir() if p.is_file() and not p.name.startswith("."))


def _clear_hook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "CURSOR_VERSION",
        "CURSOR_PROJECT_DIR",
        "CURSOR_TRANSCRIPT_PATH",
        "CURSOR_USER_EMAIL",
        "CURSOR_CODE_REMOTE",
        "CURSOR_AGENT",
        "GROK_HOOK_EVENT",
        "GROK_HOOK_NAME",
        "GROK_SESSION_ID",
        "GROK_WORKSPACE_ROOT",
        "GROK_HOME",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


class TestClientHookHostAttribution:
    def test_blocks_cursor_and_grok_variants_while_preserving_claude_code(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        _clear_hook_env(monkeypatch)
        monkeypatch.setattr(cc_hook, "get_token_securely", lambda host: f"tok-{host}")

        claude_proj = home / ".claude" / "projects" / "-Users-dev-cursor-demo"
        claude_proj.mkdir(parents=True, exist_ok=True)
        legit_claude_file = claude_proj / "cc-uuid-1.jsonl"
        legit_claude_file.write_text('{"role": "user", "content": "hi"}\n', encoding="utf-8")

        cursor_file = home / ".cursor" / "projects" / "demo" / "agent-transcripts" / "cur-1" / "cur-1.jsonl"
        cursor_file.parent.mkdir(parents=True, exist_ok=True)
        cursor_file.write_text("{}\n", encoding="utf-8")

        grok_file = home / ".grok" / "sessions" / "proj" / "019d-grok-1" / "updates.jsonl"
        grok_file.parent.mkdir(parents=True, exist_ok=True)
        grok_file.write_text("{}\n", encoding="utf-8")

        # 1. Cursor transcript path -> blocked even with clean env
        assert cc_hook.enqueue_notification(
            "claude-code", "stop", {"session_id": "cur-1", "transcript_path": str(cursor_file)}
        ) is False
        assert _count_slot_files(home) == []

        # 2. Grok transcript path -> blocked even with clean env
        assert cc_hook.enqueue_notification(
            "claude-code", "stop", {"session_id": "019d-grok-1", "transcript_path": str(grok_file)}
        ) is False
        assert _count_slot_files(home) == []

        # 3. Cursor Turn-1 payload with null transcript_path -> blocked via payload markers
        assert cc_hook.enqueue_notification(
            "claude-code",
            "prompt",
            {
                "conversation_id": "cur-turn1",
                "generation_id": "gen-1",
                "cursor_version": "2.6.11",
                "hook_event_name": "beforeSubmitPrompt",
                "transcript_path": None,
                "prompt": "Cursor turn 1",
            },
        ) is False
        assert _count_slot_files(home) == []

        # 4. Cursor Turn-1 hook env var without payload markers
        monkeypatch.setenv("CURSOR_VERSION", "2.6.11")
        assert cc_hook.enqueue_notification("claude-code", "session-start", {"session_id": "cur-env-1"}) is False
        monkeypatch.delenv("CURSOR_VERSION", raising=False)

        # 5. Grok hook env var without transcript_path (older Grok build)
        monkeypatch.setenv("GROK_HOOK_EVENT", "UserPromptSubmit")
        assert cc_hook.enqueue_notification(
            "claude-code", "prompt", {"session_id": "019d-old-grok", "prompt": "from grok"}
        ) is False
        monkeypatch.delenv("GROK_HOOK_EVENT", raising=False)

        # 6. Arbitrary outside-.claude jsonl or non-jsonl rejected for claude-code
        outside_file = tmp_path / "outside.jsonl"
        outside_file.write_text("{}\n", encoding="utf-8")
        assert cc_hook.enqueue_notification(
            "claude-code", "stop", {"session_id": "out-1", "transcript_path": str(outside_file)}
        ) is False
        non_jsonl = claude_proj / "readme.txt"
        non_jsonl.write_text("hi\n", encoding="utf-8")
        assert cc_hook.enqueue_notification(
            "claude-code", "stop", {"session_id": "txt-1", "transcript_path": str(non_jsonl)}
        ) is False

        # 7. False-positive guard: ambient CURSOR_AGENT / GROK_HOME without runner vars MUST NOT block Claude Code
        monkeypatch.setenv("CURSOR_AGENT", "1")
        monkeypatch.setenv("GROK_HOME", str(home / ".grok"))
        assert cc_hook.enqueue_notification(
            "claude-code",
            "prompt",
            {"session_id": "cc-uuid-1", "transcript_path": str(legit_claude_file), "prompt": "real claude"},
        ) is True
        assert len(_count_slot_files(home)) == 1

        # 8. Legitimate claude-code hook with empty transcript_path
        assert cc_hook.enqueue_notification(
            "claude-code", "session-start", {"session_id": "cc-start-only"}
        ) is True
        assert len(_count_slot_files(home)) == 2

        # 9. Legitimate Cursor own hook -> allowed
        assert cc_hook.enqueue_notification(
            "cursor", "stop", {"conversation_id": "cur-1", "transcript_path": str(cursor_file)}
        ) is True
        assert len(_count_slot_files(home)) == 3

        # 10. Custom CLAUDE_CONFIG_DIR respected
        custom_claude = tmp_path / "custom-claude"
        custom_file = custom_claude / "projects" / "p1" / "cc-2.jsonl"
        custom_file.parent.mkdir(parents=True, exist_ok=True)
        custom_file.write_text("{}\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_claude))
        assert cc_hook.enqueue_notification(
            "claude-code", "stop", {"session_id": "cc-2", "transcript_path": str(custom_file)}
        ) is True
        assert len(_count_slot_files(home)) == 4

    def test_main_skips_enqueue_and_returns_valid_json_when_foreign(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        _clear_hook_env(monkeypatch)
        monkeypatch.setenv("GROK_HOOK_EVENT", "SessionStart")

        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "argv", ["cc_hook.py", "claude-code", "session-start"])
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id": "g-1", "prompt": "hello"}'))
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        assert cc_hook.main() == 0
        parsed = json.loads(stdout_buf.getvalue())
        assert isinstance(parsed, dict)
        assert _count_slot_files(home) == []

        # Preserve frozen hook entry invariant required by installer.py
        main_src = inspect.getsource(cc_hook.main)
        assert "enqueue_notification(" in main_src
        assert "post_to_contextcore(" not in main_src

    def test_drain_notifications_drops_stale_foreign_slots_without_posting(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        _clear_hook_env(monkeypatch)
        monkeypatch.setattr(cc_hook, "get_token_securely", lambda host: f"tok-{host}")

        notif_dir = cc_hook._notification_dir(create=True)
        cred_hash = hashlib.sha256(b"tok-claude-code").hexdigest()
        now_s = time.time()

        legit_claude_file = home / ".claude" / "projects" / "demo" / "cc-ok.jsonl"
        legit_claude_file.parent.mkdir(parents=True, exist_ok=True)
        legit_claude_file.write_text("{}\n", encoding="utf-8")

        slot_specs = [
            (
                "slot-bad-cursor.json",
                {
                    "session_id": "c-stale",
                    "transcript_path": str(home / ".cursor" / "projects" / "x" / "agent-transcripts" / "c-stale" / "c-stale.jsonl"),
                    "prompt": "Cursor prompt",
                },
            ),
            (
                "slot-bad-grok.json",
                {
                    "session_id": "updates",
                    "transcript_path": str(home / ".grok" / "sessions" / "p" / "g-stale" / "updates.jsonl"),
                },
            ),
            (
                "slot-good-claude.json",
                {
                    "session_id": "cc-ok",
                    "transcript_path": str(legit_claude_file),
                    "prompt": "Legit Claude prompt",
                },
            ),
        ]
        for fname, slot_payload in slot_specs:
            slot_path = notif_dir / fname
            slot_path.write_text(
                json.dumps({
                    "agent_id": "claude-code",
                    "event": "prompt",
                    "base_url": "http://127.0.0.1:8420",
                    "created_at": now_s,
                    "credential_hash": cred_hash,
                    "payload": slot_payload,
                }),
                encoding="utf-8",
            )
            slot_path.chmod(0o600)

        posted: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(
            cc_hook,
            "post_to_contextcore",
            lambda host, event, payload, **kwargs: posted.append((host, event, payload)) or {"ok": True},
        )
        cc_hook.drain_notifications("http://127.0.0.1:8420")

        assert len(posted) == 1
        assert posted[0][0] == "claude-code"
        assert posted[0][2]["session_id"] == "cc-ok"
        assert _count_slot_files(home) == []

    def test_post_to_contextcore_rejects_foreign_host_before_network(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        _clear_hook_env(monkeypatch)

        monkeypatch.setattr(
            cc_hook.urllib.request,
            "urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("network must not be called")),
        )
        res = cc_hook.post_to_contextcore(
            "claude-code",
            "stop",
            {"session_id": "g1", "transcript_path": str(home / ".grok" / "sessions" / "p" / "g1" / "updates.jsonl")},
        )
        assert res is None


class TestServerHookHostAttribution:
    def test_parse_and_normalize_reject_foreign_markers_and_keep_valid_paths(self):
        with pytest.raises(AgentAdapterError):
            parse_hook(
                "claude-code",
                {
                    "session_id": "cur-1",
                    "transcript_path": "/Users/dev/.cursor/projects/p/agent-transcripts/cur-1/cur-1.jsonl",
                },
            )
        with pytest.raises(AgentAdapterError):
            parse_hook(
                "claude-code",
                {
                    "session_id": "updates",
                    "transcript_path": "/Users/dev/.grok/sessions/p/019d/updates.jsonl",
                },
            )
        with pytest.raises(AgentAdapterError):
            parse_hook(
                "claude-code",
                {
                    "session_id": "cur-t1",
                    "cursor_version": "2.6.11",
                    "generation_id": "g-1",
                    "hook_event_name": "beforeSubmitPrompt",
                },
            )
        with pytest.raises(AgentAdapterError):
            parse_hook(
                "claude-code",
                {"session_id": "grok-env", "prompt": "hi"},
                env={"GROK_HOOK_EVENT": "UserPromptSubmit"},
            )
        with pytest.raises(AgentAdapterError):
            normalize_claude_code_hook(
                {
                    "session_id": "cur-camel",
                    "transcriptPath": "/Users/dev/.cursor/projects/p/agent-transcripts/cur-camel/cur-camel.jsonl",
                }
            )

        with pytest.raises(AgentAdapterError):
            normalize_claude_code_hook(
                {
                    "session_id": "fake-claude-dir",
                    "transcript_path": "/tmp/outside/.claude/projects/fake.jsonl",
                }
            )

        # Workspace slug containing 'cursor' under actual .claude root remains valid
        ok_rec = normalize_claude_code_hook(
            {
                "session_id": "cc-ok",
                "transcript_path": str(Path.home() / ".claude/projects/-Users-dev-cursor-demo/cc-ok.jsonl"),
                "prompt": "real claude",
            }
        )
        assert ok_rec["external_id"] == "claude-code:cc-ok"

    def test_http_endpoint_rejects_foreign_or_outside_root_before_recording_observation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "http.db"))
        home = tmp_path / "home"
        claude_root = home / ".claude" / "projects" / "-Users-dev-cursor-demo"
        claude_root.mkdir(parents=True, exist_ok=True)
        legit_transcript = claude_root / "cc-valid-1.jsonl"
        legit_transcript.write_text("{}\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        _clear_hook_env(monkeypatch)

        create_schema()
        seed_data()
        cc_tok = "tok-claude"
        with db() as conn:
            conn.execute(
                "UPDATE connections SET enabled = 1, paused = 0, paths = ?, token_hash = ? WHERE id = 'claude-code'",
                (json.dumps([str(home / ".claude")]), hashlib.sha256(cc_tok.encode("utf-8")).hexdigest()),
            )

        # Pre-existing cursor sibling session for empty-transcript_path sibling test
        conversation_service.upsert_conversation(
            agent="cursor",
            title="Existing Cursor Chat",
            external_id="cursor:cur-existing-1",
            source_path=str(home / ".cursor" / "projects" / "p" / "agent-transcripts" / "cur-existing-1" / "cur-existing-1.jsonl"),
            messages=[{"role": "user", "content": "Cursor question"}],
        )

        client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
        csrf_token = client.get("/api/session").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token, "Authorization": f"Bearer {cc_tok}"}

        rejected_payloads = [
            {
                "event": "Stop",
                "session_id": "cur-p",
                "transcript_path": str(home / ".cursor" / "projects" / "p" / "agent-transcripts" / "cur-p" / "cur-p.jsonl"),
            },
            {
                "event": "Stop",
                "session_id": "updates",
                "transcript_path": str(home / ".grok" / "sessions" / "p" / "019d" / "updates.jsonl"),
            },
            {
                "session_id": "cur-t1",
                "cursor_version": "2.6.11",
                "generation_id": "gen-1",
                "hook_event_name": "beforeSubmitPrompt",
            },
            {
                "event": "Stop",
                "session_id": "outside-1",
                "transcript_path": str(tmp_path / "elsewhere" / "outside-1.jsonl"),
            },
            {
                "event": "Stop",
                "session_id": "bad-ext",
                "transcript_path": str(claude_root / "notes.txt"),
            },
            {
                "event": "UserPromptSubmit",
                "session_id": "cur-existing-1",
                "prompt": "single-line duplicate from cursor hook",
            },
        ]

        for payload in rejected_payloads:
            resp = client.post("/api/connections/claude-code/hook", headers=headers, json=payload)
            assert resp.status_code == 422, f"Expected 422 for {payload}, got {resp.status_code}: {resp.text}"

        with db() as conn:
            cc_rows = conn.execute("SELECT COUNT(*) FROM conversations WHERE agent = 'claude-code'").fetchone()[0]
            assert cc_rows == 0
        assert get_observations("claude-code") == []

        ok_resp = client.post(
            "/api/connections/claude-code/hook",
            headers=headers,
            json={
                "event": "UserPromptSubmit",
                "session_id": "cc-valid-1",
                "transcript_path": str(legit_transcript),
                "cwd": str(tmp_path),
                "prompt": "real claude prompt",
            },
        )
        assert ok_resp.status_code == 200
        with db() as conn:
            cc_rows = conn.execute("SELECT agent, external_id FROM conversations WHERE agent = 'claude-code'").fetchall()
            assert [(r[0], r[1]) for r in cc_rows] == [("claude-code", "claude-code:cc-valid-1")]
        obs = get_observations("claude-code")
        assert len(obs) == 1 and obs[0]["count"] == 1


class TestMisattributedConversationMigration:
    def test_repairs_cursor_duplicates_preserves_quotes_and_reclassifies_grok_idempotently(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "repair.db"))
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        _clear_hook_env(monkeypatch)

        claude_file = home / ".claude" / "projects" / "-Users-dev-cursor-demo" / "cc-real-1.jsonl"
        claude_file.parent.mkdir(parents=True, exist_ok=True)
        claude_file.write_text("{}\n", encoding="utf-8")

        grok_uuid1 = "01a1d39e-1111-7222-8333-444455556666"
        grok_file1 = home / ".grok" / "sessions" / "proj-a" / grok_uuid1 / "updates.jsonl"
        grok_file1.parent.mkdir(parents=True, exist_ok=True)
        grok_file1.write_text("{}\n", encoding="utf-8")

        grok_uuid2 = "019d9999-aaaa-7bbb-8ccc-ddddeeeeffff"
        grok_file2 = home / ".grok" / "sessions" / "proj-b" / grok_uuid2 / "updates.jsonl"
        grok_file2.parent.mkdir(parents=True, exist_ok=True)
        grok_file2.write_text("{}\n", encoding="utf-8")

        cursor_uuid1 = "11111111-2222-4333-8444-555555555555"
        cursor_uuid2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        cursor_file1 = home / ".cursor" / "projects" / "demo" / "agent-transcripts" / cursor_uuid1 / f"{cursor_uuid1}.jsonl"
        cursor_file1.parent.mkdir(parents=True, exist_ok=True)
        cursor_file1.write_text("{}\n", encoding="utf-8")

        create_schema()
        with db() as conn:
            ensure_derivation_schema(conn)
        seed_data()

        proj = project_service.create_project(name="Demo", description="d", directories=[str(tmp_path)])
        sib1 = conversation_service.upsert_conversation(
            agent="cursor",
            project_id=proj["id"],
            title="Real Cursor Chat 1",
            external_id=f"cursor:{cursor_uuid1}",
            source_path=str(cursor_file1),
            messages=[{"role": "user", "content": "Cursor msg 1"}],
        )
        dup1 = conversation_service.upsert_conversation(
            agent="claude-code",
            project_id=proj["id"],
            title=f"Claude Code {cursor_uuid1}",
            external_id=f"claude-code:{cursor_uuid1}",
            source_path=str(cursor_file1),
            messages=[{"role": "user", "content": "Cursor msg 1"}],
        )
        sib2 = conversation_service.upsert_conversation(
            agent="cursor",
            project_id=proj["id"],
            title="Real Cursor Chat 2",
            external_id=f"cursor:{cursor_uuid2}",
            messages=[{"role": "user", "content": "Cursor msg 2"}],
        )
        dup2 = conversation_service.upsert_conversation(
            agent="claude-code",
            project_id=proj["id"],
            title=f"Claude Code {cursor_uuid2}",
            external_id=f"claude-code:{cursor_uuid2}",
            source_path=None,
            messages=[{"role": "user", "content": "Cursor msg 2"}],
        )
        grok1 = conversation_service.upsert_conversation(
            agent="claude-code",
            project_id=proj["id"],
            title="Claude Code updates",
            external_id="claude-code:updates",
            source_path=str(grok_file1),
            messages=[{"role": "user", "content": "排查 Docker 网络超时问题"}],
        )
        grok2 = conversation_service.upsert_conversation(
            agent="claude-code",
            project_id=proj["id"],
            title=f"Claude Code {grok_uuid2}",
            external_id=f"claude-code:{grok_uuid2}",
            source_path=None,
            messages=[{"role": "user", "content": "Check Grok CLI auth config"}],
        )
        legit1 = conversation_service.upsert_conversation(
            agent="claude-code",
            project_id=proj["id"],
            title="Legit Claude Session",
            external_id="claude-code:cc-real-1",
            source_path=str(claude_file),
            messages=[{"role": "user", "content": "Real Claude"}],
        )
        legit2 = conversation_service.upsert_conversation(
            agent="claude-code",
            project_id=proj["id"],
            title="Claude Code manual-only",
            external_id="claude-code:manual-only",
            source_path=None,
            messages=[{"role": "user", "content": "Manual Claude"}],
        )

        # Attach content_sources, working_tasks, handoffs, and relay_discovery_snapshots
        mem1 = content_service.create_content(
            title="Evidence 1", content="Use ipv4", category="knowledge", project_id=proj["id"], agent="claude-code"
        )
        mem2 = content_service.create_content(
            title="Evidence 2", content="Already linked", category="knowledge", project_id=proj["id"], agent="cursor"
        )
        with db() as conn:
            conn.execute(
                """
                INSERT INTO content_sources (id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at)
                VALUES ('src_dup1', ?, ?, 'conversation', ?, 'claude-code', 0, 'Important quote from dup1', '2026-11-14T10:00:00Z')
                """,
                (mem1["id"], dup1["id"], dup1["title"]),
            )
            conn.execute(
                """
                INSERT INTO content_sources (id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at)
                VALUES
                    ('src_sib2', ?, ?, 'conversation', ?, 'cursor', 0, 'Sibling quote', '2026-11-14T10:00:00Z'),
                    ('src_dup2', ?, ?, 'conversation', ?, 'claude-code', 0, 'Dup2 quote', '2026-11-14T10:00:00Z')
                """,
                (mem2["id"], sib2["id"], sib2["title"], mem2["id"], dup2["id"], dup2["title"]),
            )
            conn.execute(
                """
                INSERT INTO handoffs (id, project_id, source_agent, target_agent, status, card, source_ids, source_revisions, created_at)
                VALUES ('ho_dup1', ?, 'claude-code', 'codex', 'confirmed', '{}', ?, ?, '2026-11-14T10:00:00Z')
                """,
                (proj["id"], json.dumps([dup1["id"]]), json.dumps({dup1["id"]: "rev1"})),
            )
            conn.execute(
                """
                INSERT INTO relay_discovery_snapshots (scope_key, mapping_json, updated_at)
                VALUES ('*::__global__', '{"1": "stale"}', '2026-11-14T10:00:00Z')
                """
            )
            conn.execute("DELETE FROM app_settings WHERE key = 'misattributed_claude_code_hooks_repaired_v1'")

        working_memory_service.upsert_working_task(
            title="Track task",
            project_id=proj["id"],
            agent="claude-code",
            source_conversation_id=dup1["id"],
        )

        # Verify create_schema startup migration executes repair once & sets guard key
        create_schema()
        assert get_app_setting("misattributed_claude_code_hooks_repaired_v1", default=False) is True

        with db() as conn:
            remaining_ids = {
                r[0]: (r[1], r[2], r[3], r[4])
                for r in conn.execute("SELECT id, agent, external_id, title, source_path FROM conversations").fetchall()
            }
            assert dup1["id"] not in remaining_ids
            assert dup2["id"] not in remaining_ids
            assert sib1["id"] in remaining_ids
            assert sib2["id"] in remaining_ids
            assert remaining_ids[grok1["id"]][0] == "grok-build"
            assert remaining_ids[grok1["id"]][1] == f"grok-build:{grok_uuid1}"
            assert remaining_ids[grok1["id"]][2] != "Claude Code updates"
            assert "Docker" in remaining_ids[grok1["id"]][2]

            assert remaining_ids[grok2["id"]][0] == "grok-build"
            assert remaining_ids[grok2["id"]][1] == f"grok-build:{grok_uuid2}"
            assert not remaining_ids[grok2["id"]][2].startswith("Claude Code")
            assert remaining_ids[grok2["id"]][3] == str(grok_file2)

            assert remaining_ids[legit1["id"]][0] == "claude-code"
            assert remaining_ids[legit2["id"]][0] == "claude-code"

            # Trigger deleted_conversation_evidence must NOT have wiped quote on re-homed src_dup1 or src_sib2
            src_dup1_row = conn.execute(
                "SELECT source_id, source_agent, deleted, quote FROM content_sources WHERE id = 'src_dup1'"
            ).fetchone()
            assert tuple(src_dup1_row) == (sib1["id"], "cursor", 0, "Important quote from dup1")

            src_sib2_rows = conn.execute(
                "SELECT id, source_id, deleted, quote FROM content_sources WHERE content_id = ?",
                (mem2["id"],),
            ).fetchall()
            assert [tuple(r) for r in src_sib2_rows] == [("src_sib2", sib2["id"], 0, "Sibling quote")]

            # Working task & handoff re-homed to sibling
            wt_row = conn.execute("SELECT source_conversation_id, last_agent FROM working_tasks").fetchone()
            assert tuple(wt_row) == (sib1["id"], "cursor")
            ho_row = conn.execute("SELECT source_agent, source_ids, source_revisions FROM handoffs WHERE id = 'ho_dup1'").fetchone()
            assert ho_row[0] == "cursor"
            assert json.loads(ho_row[1]) == [sib1["id"]]
            assert sib1["id"] in json.loads(ho_row[2])

            # Stale relay snapshot cleared
            assert conn.execute("SELECT COUNT(*) FROM relay_discovery_snapshots").fetchone()[0] == 0

            # Index updated for grok rows
            idx_row = conn.execute(
                "SELECT title_zh FROM conversation_relay_index WHERE conversation_id = ?",
                (grok1["id"],),
            ).fetchone()
            assert idx_row is not None and "Docker" in idx_row[0]

        # Idempotency check
        second_stats = conversation_service.repair_misattributed_claude_code_conversations(home=home)
        assert second_stats["total_repaired"] == 0
