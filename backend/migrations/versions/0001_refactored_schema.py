"""Initial schema for the modular runtime. Legacy databases must be reset."""

import sqlalchemy as sa
from alembic import op

revision = "0001_refactored"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_runtime_heartbeats",
        sa.Column("worker_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chromium_path", sa.Text(), nullable=True),
        sa.Column("chromium_version", sa.String(length=120), nullable=True),
        sa.Column("playwright_version", sa.String(length=80), nullable=True),
        sa.Column("launch_succeeded", sa.Boolean(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        op.f("ix_browser_runtime_heartbeats_checked_at"),
        "browser_runtime_heartbeats",
        ["checked_at"],
        unique=False,
    )
    op.create_table(
        "capabilities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_capabilities_type"), "capabilities", ["type"], unique=False)
    op.create_table(
        "failure_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("fingerprint", sa.String(length=80), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=True),
        sa.Column("plan_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("source_node_run_id", sa.String(length=36), nullable=True),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("occurrence", sa.Integer(), nullable=False),
        sa.Column("recovered", sa.Boolean(), nullable=False),
        sa.Column("baseline_reproduced", sa.Boolean(), nullable=False),
        sa.Column("evidence_kind", sa.String(length=40), nullable=False),
        sa.Column("reproduction_policy_json", sa.JSON(), nullable=False),
        sa.Column("evidence_ids_json", sa.JSON(), nullable=False),
        sa.Column("last_run_id", sa.String(length=36), nullable=True),
        sa.Column("last_checkpoint_id", sa.String(length=36), nullable=True),
        sa.Column("locks_json", sa.JSON(), nullable=False),
        sa.Column("contract_summary_json", sa.JSON(), nullable=False),
        sa.Column("recovered_outcome_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(workflow_version_id IS NOT NULL) != (plan_snapshot_id IS NOT NULL)",
            name="ck_failure_observation_source",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_failure_observation_fingerprint", "failure_observations", ["fingerprint"], unique=True
    )
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("transport", sa.String(length=20), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "runtime_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("probe_json", sa.JSON(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('model', 'agent')", name="ck_runtime_profile_kind"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_runtime_profiles_kind"), "runtime_profiles", ["kind"], unique=False)
    op.create_index(
        "uq_runtime_profile_default",
        "runtime_profiles",
        ["kind"],
        unique=True,
        sqlite_where=sa.text("is_default = 1"),
    )
    op.create_table(
        "secret_references",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("external_key", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "task_checkpoints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_kind", sa.String(length=24), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("source_digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_checkpoint_source", "task_checkpoints", ["source_kind", "source_id"], unique=False
    )
    op.create_index(
        op.f("ix_task_checkpoints_status"), "task_checkpoints", ["status"], unique=False
    )
    op.create_table(
        "workflow_disclosure_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("agent_run_id", sa.String(length=120), nullable=True),
        sa.Column("disclosed_json", sa.JSON(), nullable=False),
        sa.Column("call_budget_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workflow_disclosure_sessions_agent_run_id"),
        "workflow_disclosure_sessions",
        ["agent_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_disclosure_sessions_conversation_id"),
        "workflow_disclosure_sessions",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_disclosure_sessions_expires_at"),
        "workflow_disclosure_sessions",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_disclosure_sessions_status"),
        "workflow_disclosure_sessions",
        ["status"],
        unique=False,
    )
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("owner_id", sa.String(length=120), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_json", sa.JSON(), nullable=False),
        sa.Column("draft_digest", sa.String(length=80), nullable=True),
        sa.Column("draft_base_version_id", sa.String(length=36), nullable=True),
        sa.Column("draft_metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workflows_name"), "workflows", ["name"], unique=False)
    op.create_table(
        "capability_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("capability_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("package_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["capability_id"], ["capabilities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_capability_versions_capability_id"),
        "capability_versions",
        ["capability_id"],
        unique=False,
    )
    op.create_index(
        "uq_capability_version", "capability_versions", ["capability_id", "version"], unique=True
    )
    op.create_table(
        "mcp_server_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mcp_server_versions_server_id"), "mcp_server_versions", ["server_id"], unique=False
    )
    op.create_index(
        "uq_mcp_server_version", "mcp_server_versions", ["server_id", "version"], unique=True
    )
    op.create_table(
        "replay_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("baseline_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("patched_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("diff_json", sa.JSON(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("fixture_digest", sa.String(length=80), nullable=True),
        sa.Column("base_version_id", sa.String(length=36), nullable=True),
        sa.Column("patch_of_observation_id", sa.String(length=36), nullable=True),
        sa.Column("base_definition_digest", sa.String(length=80), nullable=True),
        sa.Column("patch_digest", sa.String(length=80), nullable=True),
        sa.Column("patched_definition_digest", sa.String(length=80), nullable=True),
        sa.Column("proposal_id", sa.String(length=36), nullable=True),
        sa.Column("human_patch_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["observation_id"], ["failure_observations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_replay_evidence_observation_id"),
        "replay_evidence",
        ["observation_id"],
        unique=False,
    )
    op.create_table(
        "workflow_disclosure_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("request_summary_json", sa.JSON(), nullable=False),
        sa.Column("response_digest", sa.String(length=80), nullable=False),
        sa.Column("disclosure_level", sa.String(length=8), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["workflow_disclosure_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_disclosure_call_session",
        "workflow_disclosure_calls",
        ["session_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("validation_json", sa.JSON(), nullable=False),
        sa.Column("published_from_draft_revision", sa.Integer(), nullable=True),
        sa.Column("published_from_draft_digest", sa.String(length=80), nullable=True),
        sa.Column("revision_lineage_json", sa.JSON(), nullable=False),
        sa.Column("presentation_policy_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workflow_versions_digest"), "workflow_versions", ["digest"], unique=False
    )
    op.create_index(
        "uq_workflow_version", "workflow_versions", ["workflow_id", "version"], unique=True
    )
    op.create_table(
        "components",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("default_version_id", sa.String(length=36), nullable=False),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("execution_class", sa.String(length=20), nullable=False),
        sa.Column("seed_key", sa.String(length=120), nullable=True),
        sa.Column("managed_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["default_version_id"],
            ["workflow_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seed_key"),
    )
    op.create_index(op.f("ix_components_name"), "components", ["name"], unique=False)
    op.create_table(
        "mcp_tool_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("server_version_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema_json", sa.JSON(), nullable=False),
        sa.Column("output_schema_json", sa.JSON(), nullable=True),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("contract_digest", sa.String(length=80), nullable=False),
        sa.Column("side_effect", sa.String(length=30), nullable=False),
        sa.Column("idempotent", sa.Boolean(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("effect_source", sa.String(length=20), nullable=False),
        sa.Column("effect_review_status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_version_id"], ["mcp_server_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mcp_tool_snapshots_server_version_id"),
        "mcp_tool_snapshots",
        ["server_version_id"],
        unique=False,
    )
    op.create_index(
        "uq_mcp_tool_name", "mcp_tool_snapshots", ["server_version_id", "name"], unique=True
    )
    op.create_table(
        "revision_proposals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("base_version_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("fixture_digest", sa.String(length=80), nullable=True),
        sa.Column("baseline_digest", sa.String(length=80), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("proposal_digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["base_version_id"], ["workflow_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["observation_id"], ["failure_observations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_revision_proposals_base_version_id"),
        "revision_proposals",
        ["base_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_revision_proposals_observation_id"),
        "revision_proposals",
        ["observation_id"],
        unique=False,
    )
    op.create_index(
        "uq_revision_proposal_content",
        "revision_proposals",
        ["observation_id", "baseline_digest", "proposal_digest"],
        unique=True,
    )
    op.create_table(
        "workflow_capability_manifests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_digest", sa.String(length=80), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"], ["workflow_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workflow_capability_manifests_digest"),
        "workflow_capability_manifests",
        ["digest"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_capability_status", "workflow_capability_manifests", ["status"], unique=False
    )
    op.create_index(
        "uq_workflow_capability_version",
        "workflow_capability_manifests",
        ["workflow_version_id"],
        unique=True,
    )
    op.create_table(
        "workflow_fragment_manifests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("fragment_ref", sa.String(length=240), nullable=False),
        sa.Column("source_workflow_digest", sa.String(length=80), nullable=False),
        sa.Column("closure_digest", sa.String(length=80), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"], ["workflow_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workflow_fragment_manifests_digest"),
        "workflow_fragment_manifests",
        ["digest"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_fragment_version",
        "workflow_fragment_manifests",
        ["workflow_version_id"],
        unique=False,
    )
    op.create_index(
        "uq_workflow_fragment_ref", "workflow_fragment_manifests", ["fragment_ref"], unique=True
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("bound_component_id", sa.String(length=36), nullable=True),
        sa.Column("agent_profile_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_mode", sa.String(length=16), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["bound_component_id"],
            ["components.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversations_activity", "conversations", ["last_message_at", "id"], unique=False
    )
    op.create_table(
        "human_patches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("parent_proposal_id", sa.String(length=36), nullable=False),
        sa.Column("editor_id", sa.String(length=120), nullable=False),
        sa.Column("patch_json", sa.JSON(), nullable=False),
        sa.Column("patch_digest", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_proposal_id"], ["revision_proposals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_human_patches_parent_proposal_id"),
        "human_patches",
        ["parent_proposal_id"],
        unique=False,
    )
    op.create_index(
        "uq_human_patch_content",
        "human_patches",
        ["parent_proposal_id", "patch_digest"],
        unique=True,
    )
    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("user_message_id", sa.String(length=36), nullable=True),
        sa.Column("assistant_message_id", sa.String(length=36), nullable=True),
        sa.Column("parent_turn_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_mode_snapshot", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_turn_id"], ["conversation_turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_turns_conversation",
        "conversation_turns",
        ["conversation_id", "accepted_at", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_turns_conversation_id"),
        "conversation_turns",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_turns_status"), "conversation_turns", ["status"], unique=False
    )
    op.create_table(
        "pending_selections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("disclosure_session_id", sa.String(length=36), nullable=False),
        sa.Column("original_task", sa.Text(), nullable=False),
        sa.Column("selection_json", sa.JSON(), nullable=False),
        sa.Column("bindings_json", sa.JSON(), nullable=False),
        sa.Column("missing_inputs_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["disclosure_session_id"], ["workflow_disclosure_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pending_selections_conversation_id"),
        "pending_selections",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pending_selections_disclosure_session_id"),
        "pending_selections",
        ["disclosure_session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pending_selections_expires_at"), "pending_selections", ["expires_at"], unique=False
    )
    op.create_index(
        op.f("ix_pending_selections_status"), "pending_selections", ["status"], unique=False
    )
    op.create_table(
        "conversation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("run_type", sa.String(length=24), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("trajectory_json", sa.JSON(), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_event_seq", sa.Integer(), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_runs_claim",
        "conversation_runs",
        ["status", "lease_expires_at", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_runs_conversation_id"),
        "conversation_runs",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_runs_turn_id"), "conversation_runs", ["turn_id"], unique=False
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_messages_conversation_id"), "messages", ["conversation_id"], unique=False
    )
    op.create_index(op.f("ix_messages_turn_id"), "messages", ["turn_id"], unique=False)
    op.create_table(
        "conversation_run_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_run_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_run_id"], ["conversation_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_conversation_run_events_conversation_run_id"),
        "conversation_run_events",
        ["conversation_run_id"],
        unique=False,
    )
    op.create_index(
        "uq_conversation_run_seq",
        "conversation_run_events",
        ["conversation_run_id", "seq"],
        unique=True,
    )
    op.create_table(
        "distillations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("draft_workflow_id", sa.String(length=36), nullable=True),
        sa.Column("analysis_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_run_id"], ["conversation_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["draft_workflow_id"],
            ["workflows.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_distillations_conversation_run_id"),
        "distillations",
        ["conversation_run_id"],
        unique=False,
    )
    op.create_table(
        "plan_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("source_refs_json", sa.JSON(), nullable=False),
        sa.Column("locks_json", sa.JSON(), nullable=False),
        sa.Column("source_map_json", sa.JSON(), nullable=False),
        sa.Column("created_by_conversation_run_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("plan_signature", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_conversation_run_id"], ["conversation_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plan_signature", "plan_snapshots", ["plan_signature"], unique=False)
    op.create_index(op.f("ix_plan_snapshots_digest"), "plan_snapshots", ["digest"], unique=False)
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=True),
        sa.Column("plan_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("component_id", sa.String(length=36), nullable=True),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("owner_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("runtime_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("next_event_seq", sa.Integer(), nullable=False),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(workflow_version_id IS NOT NULL) != (plan_snapshot_id IS NOT NULL)",
            name="ck_run_execution_source",
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
        ),
        sa.ForeignKeyConstraint(
            ["plan_snapshot_id"],
            ["plan_snapshots.id"],
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runs_component_created", "runs", ["component_id", "created_at", "id"], unique=False
    )
    op.create_index(op.f("ix_runs_status"), "runs", ["status"], unique=False)
    op.create_index(op.f("ix_runs_turn_id"), "runs", ["turn_id"], unique=False)
    op.create_index(
        "ix_runs_workflow_version_created",
        "runs",
        ["workflow_version_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("conversation_run_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(run_id IS NOT NULL) != (conversation_run_id IS NOT NULL)", name="ck_artifact_source"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_run_id"], ["conversation_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_artifacts_conversation_run_id"), "artifacts", ["conversation_run_id"], unique=False
    )
    op.create_index(op.f("ix_artifacts_run_id"), "artifacts", ["run_id"], unique=False)
    op.create_index(
        "uq_conversation_artifact_name",
        "artifacts",
        ["conversation_run_id", "kind", "name"],
        unique=True,
    )
    op.create_table(
        "execution_checkpoints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("failed_node_id", sa.String(length=64), nullable=False),
        sa.Column("failed_node_run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=True),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(length=80), nullable=True),
        sa.Column("recovery_node_run_id", sa.String(length=36), nullable=True),
        sa.Column("approval_status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_checkpoints_run_id", "execution_checkpoints", ["run_id"], unique=False
    )
    op.create_index(
        "uq_checkpoint_failed_attempt",
        "execution_checkpoints",
        ["failed_node_run_id", "attempt"],
        unique=True,
    )
    op.create_table(
        "node_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("worker_id", sa.String(length=120), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_generation", sa.Integer(), nullable=True),
        sa.Column("claim_token", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_node_claim", "node_runs", ["status", "available_at", "created_at"], unique=False
    )
    op.create_index(op.f("ix_node_runs_run_id"), "node_runs", ["run_id"], unique=False)
    op.create_index(op.f("ix_node_runs_status"), "node_runs", ["status"], unique=False)
    op.create_index("uq_node_attempt", "node_runs", ["run_id", "node_id", "attempt"], unique=True)
    op.create_table(
        "output_substitutions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("failed_node_run_id", sa.String(length=36), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_output_substitution_node", "output_substitutions", ["run_id", "node_id"], unique=True
    )
    op.create_table(
        "run_presentations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("producer", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("config_digest", sa.String(length=80), nullable=True),
        sa.Column("prompt_template_digest", sa.String(length=80), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_run_presentations_run_id"), "run_presentations", ["run_id"], unique=False
    )
    op.create_index(
        op.f("ix_run_presentations_status"), "run_presentations", ["status"], unique=False
    )
    op.create_index(
        "uq_run_presentation_revision", "run_presentations", ["run_id", "revision"], unique=True
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_run_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=80), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decided_by", sa.String(length=120), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["node_run_id"], ["node_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_approvals_run_id"), "approvals", ["run_id"], unique=False)
    op.create_index(op.f("ix_approvals_status"), "approvals", ["status"], unique=False)
    op.create_table(
        "model_usage",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("conversation_run_id", sa.String(length=36), nullable=True),
        sa.Column("presentation_id", sa.String(length=36), nullable=True),
        sa.Column("call_id", sa.String(length=160), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("protocol", sa.String(length=80), nullable=True),
        sa.Column("runtime_type", sa.String(length=40), nullable=True),
        sa.Column("adapter_version", sa.String(length=80), nullable=True),
        sa.Column("adapter_digest", sa.String(length=80), nullable=True),
        sa.Column("runtime_used", sa.String(length=32), nullable=True),
        sa.Column("runtime_digest", sa.String(length=80), nullable=True),
        sa.Column("fallback_reason", sa.String(length=80), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("usage_complete", sa.Boolean(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_run_id"], ["conversation_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["presentation_id"], ["run_presentations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_usage_turn", "model_usage", ["turn_id", "created_at"], unique=False)
    op.create_index(
        "uq_model_usage_call_attempt", "model_usage", ["call_id", "attempt"], unique=True
    )
    op.create_table(
        "recovery_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("plan_patch_json", sa.JSON(), nullable=True),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["checkpoint_id"], ["execution_checkpoints.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_recovery_attempt", "recovery_attempts", ["checkpoint_id", "attempt"], unique=True
    )
    op.create_table(
        "replay_fixtures",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_run_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("source_digest", sa.String(length=80), nullable=False),
        sa.Column("input_digest", sa.String(length=80), nullable=False),
        sa.Column("output_digest", sa.String(length=80), nullable=False),
        sa.Column("capture_policy", sa.String(length=40), nullable=False),
        sa.Column("evidence_kind", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_replay_fixtures_run_id"), "replay_fixtures", ["run_id"], unique=False)
    op.create_index("uq_replay_fixture_node_run", "replay_fixtures", ["node_run_id"], unique=True)
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_run_id", sa.String(length=36), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["node_run_id"], ["node_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_run_events_run_id"), "run_events", ["run_id"], unique=False)
    op.create_index("uq_run_seq", "run_events", ["run_id", "seq"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_run_seq", table_name="run_events")
    op.drop_index(op.f("ix_run_events_run_id"), table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("uq_replay_fixture_node_run", table_name="replay_fixtures")
    op.drop_index(op.f("ix_replay_fixtures_run_id"), table_name="replay_fixtures")
    op.drop_table("replay_fixtures")
    op.drop_index("uq_recovery_attempt", table_name="recovery_attempts")
    op.drop_table("recovery_attempts")
    op.drop_index("uq_model_usage_call_attempt", table_name="model_usage")
    op.drop_index("ix_model_usage_turn", table_name="model_usage")
    op.drop_table("model_usage")
    op.drop_index(op.f("ix_approvals_status"), table_name="approvals")
    op.drop_index(op.f("ix_approvals_run_id"), table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("uq_run_presentation_revision", table_name="run_presentations")
    op.drop_index(op.f("ix_run_presentations_status"), table_name="run_presentations")
    op.drop_index(op.f("ix_run_presentations_run_id"), table_name="run_presentations")
    op.drop_table("run_presentations")
    op.drop_index("uq_output_substitution_node", table_name="output_substitutions")
    op.drop_table("output_substitutions")
    op.drop_index("uq_node_attempt", table_name="node_runs")
    op.drop_index(op.f("ix_node_runs_status"), table_name="node_runs")
    op.drop_index(op.f("ix_node_runs_run_id"), table_name="node_runs")
    op.drop_index("ix_node_claim", table_name="node_runs")
    op.drop_table("node_runs")
    op.drop_index("uq_checkpoint_failed_attempt", table_name="execution_checkpoints")
    op.drop_index("ix_execution_checkpoints_run_id", table_name="execution_checkpoints")
    op.drop_table("execution_checkpoints")
    op.drop_index("uq_conversation_artifact_name", table_name="artifacts")
    op.drop_index(op.f("ix_artifacts_run_id"), table_name="artifacts")
    op.drop_index(op.f("ix_artifacts_conversation_run_id"), table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_runs_workflow_version_created", table_name="runs")
    op.drop_index(op.f("ix_runs_turn_id"), table_name="runs")
    op.drop_index(op.f("ix_runs_status"), table_name="runs")
    op.drop_index("ix_runs_component_created", table_name="runs")
    op.drop_table("runs")
    op.drop_index(op.f("ix_plan_snapshots_digest"), table_name="plan_snapshots")
    op.drop_index("ix_plan_signature", table_name="plan_snapshots")
    op.drop_table("plan_snapshots")
    op.drop_index(op.f("ix_distillations_conversation_run_id"), table_name="distillations")
    op.drop_table("distillations")
    op.drop_index("uq_conversation_run_seq", table_name="conversation_run_events")
    op.drop_index(
        op.f("ix_conversation_run_events_conversation_run_id"), table_name="conversation_run_events"
    )
    op.drop_table("conversation_run_events")
    op.drop_index(op.f("ix_messages_turn_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_conversation_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_conversation_runs_turn_id"), table_name="conversation_runs")
    op.drop_index(op.f("ix_conversation_runs_conversation_id"), table_name="conversation_runs")
    op.drop_index("ix_conversation_runs_claim", table_name="conversation_runs")
    op.drop_table("conversation_runs")
    op.drop_index(op.f("ix_pending_selections_status"), table_name="pending_selections")
    op.drop_index(op.f("ix_pending_selections_expires_at"), table_name="pending_selections")
    op.drop_index(
        op.f("ix_pending_selections_disclosure_session_id"), table_name="pending_selections"
    )
    op.drop_index(op.f("ix_pending_selections_conversation_id"), table_name="pending_selections")
    op.drop_table("pending_selections")
    op.drop_index(op.f("ix_conversation_turns_status"), table_name="conversation_turns")
    op.drop_index(op.f("ix_conversation_turns_conversation_id"), table_name="conversation_turns")
    op.drop_index("ix_conversation_turns_conversation", table_name="conversation_turns")
    op.drop_table("conversation_turns")
    op.drop_index("uq_human_patch_content", table_name="human_patches")
    op.drop_index(op.f("ix_human_patches_parent_proposal_id"), table_name="human_patches")
    op.drop_table("human_patches")
    op.drop_index("ix_conversations_activity", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("uq_workflow_fragment_ref", table_name="workflow_fragment_manifests")
    op.drop_index("ix_workflow_fragment_version", table_name="workflow_fragment_manifests")
    op.drop_index(
        op.f("ix_workflow_fragment_manifests_digest"), table_name="workflow_fragment_manifests"
    )
    op.drop_table("workflow_fragment_manifests")
    op.drop_index("uq_workflow_capability_version", table_name="workflow_capability_manifests")
    op.drop_index("ix_workflow_capability_status", table_name="workflow_capability_manifests")
    op.drop_index(
        op.f("ix_workflow_capability_manifests_digest"), table_name="workflow_capability_manifests"
    )
    op.drop_table("workflow_capability_manifests")
    op.drop_index("uq_revision_proposal_content", table_name="revision_proposals")
    op.drop_index(op.f("ix_revision_proposals_observation_id"), table_name="revision_proposals")
    op.drop_index(op.f("ix_revision_proposals_base_version_id"), table_name="revision_proposals")
    op.drop_table("revision_proposals")
    op.drop_index("uq_mcp_tool_name", table_name="mcp_tool_snapshots")
    op.drop_index(op.f("ix_mcp_tool_snapshots_server_version_id"), table_name="mcp_tool_snapshots")
    op.drop_table("mcp_tool_snapshots")
    op.drop_index(op.f("ix_components_name"), table_name="components")
    op.drop_table("components")
    op.drop_index("uq_workflow_version", table_name="workflow_versions")
    op.drop_index(op.f("ix_workflow_versions_digest"), table_name="workflow_versions")
    op.drop_table("workflow_versions")
    op.drop_index("ix_disclosure_call_session", table_name="workflow_disclosure_calls")
    op.drop_table("workflow_disclosure_calls")
    op.drop_index(op.f("ix_replay_evidence_observation_id"), table_name="replay_evidence")
    op.drop_table("replay_evidence")
    op.drop_index("uq_mcp_server_version", table_name="mcp_server_versions")
    op.drop_index(op.f("ix_mcp_server_versions_server_id"), table_name="mcp_server_versions")
    op.drop_table("mcp_server_versions")
    op.drop_index("uq_capability_version", table_name="capability_versions")
    op.drop_index(op.f("ix_capability_versions_capability_id"), table_name="capability_versions")
    op.drop_table("capability_versions")
    op.drop_index(op.f("ix_workflows_name"), table_name="workflows")
    op.drop_table("workflows")
    op.drop_index(
        op.f("ix_workflow_disclosure_sessions_status"), table_name="workflow_disclosure_sessions"
    )
    op.drop_index(
        op.f("ix_workflow_disclosure_sessions_expires_at"),
        table_name="workflow_disclosure_sessions",
    )
    op.drop_index(
        op.f("ix_workflow_disclosure_sessions_conversation_id"),
        table_name="workflow_disclosure_sessions",
    )
    op.drop_index(
        op.f("ix_workflow_disclosure_sessions_agent_run_id"),
        table_name="workflow_disclosure_sessions",
    )
    op.drop_table("workflow_disclosure_sessions")
    op.drop_index(op.f("ix_task_checkpoints_status"), table_name="task_checkpoints")
    op.drop_index("ix_task_checkpoint_source", table_name="task_checkpoints")
    op.drop_table("task_checkpoints")
    op.drop_table("secret_references")
    op.drop_index(
        "uq_runtime_profile_default",
        table_name="runtime_profiles",
        sqlite_where=sa.text("is_default = 1"),
    )
    op.drop_index(op.f("ix_runtime_profiles_kind"), table_name="runtime_profiles")
    op.drop_table("runtime_profiles")
    op.drop_table("mcp_servers")
    op.drop_index("uq_failure_observation_fingerprint", table_name="failure_observations")
    op.drop_table("failure_observations")
    op.drop_index(op.f("ix_capabilities_type"), table_name="capabilities")
    op.drop_table("capabilities")
    op.drop_index(
        op.f("ix_browser_runtime_heartbeats_checked_at"), table_name="browser_runtime_heartbeats"
    )
    op.drop_table("browser_runtime_heartbeats")
