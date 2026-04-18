/// <reference path="../pb_data/types.d.ts" />
/**
 * Daily Note Harness — Section Operations Telemetry v0.1.0
 *
 * Collections:
 *   daily_sessions      — Claude/harness sessions
 *   section_snapshots   — immutable point-in-time section state
 *   section_operations  — event stream of all writes/reads/skips
 *   experiments         — scientist-framed hypothesis tracking
 *   linked_docs         — parent-section → child-doc relationships
 *
 * Privacy: all rules empty (admin-only). Data stays on localhost.
 * See vault: 2026-04-17_Section_Ops_Schema.md for full design.
 */

migrate((db) => {
    const dao = new Dao(db);

    // ── daily_sessions ───────────────────────────────────────────────
    const sessions = new Collection({
        name: "daily_sessions",
        type: "base",
        schema: [
            { name: "session_id",    type: "text",   required: true,  options: { min: 1 } },
            { name: "actor",         type: "text",   required: true },
            { name: "model",         type: "text",   required: false },
            { name: "started_at",    type: "date",   required: true },
            { name: "ended_at",      type: "date",   required: false },
            { name: "project_root",  type: "text",   required: false },
            { name: "git_branch",    type: "text",   required: false },
            { name: "commit_count",  type: "number", required: false },
            { name: "metadata",      type: "json",   required: false, options: { maxSize: 2000000 }},
        ],
        indexes: [
            "CREATE UNIQUE INDEX idx_daily_sessions_sid ON daily_sessions (session_id)",
            "CREATE INDEX idx_daily_sessions_started ON daily_sessions (started_at)",
        ],
        listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: null,
    });
    dao.saveCollection(sessions);

    // ── section_snapshots ────────────────────────────────────────────
    const snapshots = new Collection({
        name: "section_snapshots",
        type: "base",
        schema: [
            { name: "note_date",        type: "text",   required: true },
            { name: "section_name",     type: "text",   required: true },
            { name: "content_hash",     type: "text",   required: true },
            { name: "content_preview",  type: "text",   required: false, options: { max: 500 } },
            { name: "word_count",       type: "number", required: false },
            { name: "filled",           type: "bool",   required: false },
            { name: "captured_at",      type: "date",   required: true },
            { name: "session_id",       type: "text",   required: false },
        ],
        indexes: [
            "CREATE INDEX idx_snap_date ON section_snapshots (note_date)",
            "CREATE INDEX idx_snap_section ON section_snapshots (section_name)",
            "CREATE INDEX idx_snap_hash ON section_snapshots (content_hash)",
        ],
        listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: null,
    });
    dao.saveCollection(snapshots);

    // ── section_operations ───────────────────────────────────────────
    const ops = new Collection({
        name: "section_operations",
        type: "base",
        schema: [
            { name: "op_id",            type: "text",   required: true },
            { name: "session_id",       type: "text",   required: true },
            { name: "note_date",        type: "text",   required: true },
            { name: "section_name",     type: "text",   required: true },
            { name: "operation",        type: "text",   required: true },
            { name: "actor",            type: "text",   required: true },
            { name: "source",           type: "text",   required: true },
            { name: "before_hash",      type: "text",   required: false },
            { name: "after_hash",       type: "text",   required: false },
            { name: "bytes_written",    type: "number", required: false },
            { name: "duration_ms",      type: "number", required: false },
            { name: "result",           type: "text",   required: true },
            { name: "error_message",    type: "text",   required: false },
            { name: "linked_doc_path",  type: "text",   required: false },
            { name: "occurred_at",      type: "date",   required: true },
            { name: "metadata",         type: "json",   required: false, options: { maxSize: 2000000 }},
        ],
        indexes: [
            "CREATE UNIQUE INDEX idx_ops_opid ON section_operations (op_id)",
            "CREATE INDEX idx_ops_date ON section_operations (note_date)",
            "CREATE INDEX idx_ops_section ON section_operations (section_name)",
            "CREATE INDEX idx_ops_occurred ON section_operations (occurred_at)",
            "CREATE INDEX idx_ops_session ON section_operations (session_id)",
        ],
        listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: null,
    });
    dao.saveCollection(ops);

    // ── experiments ──────────────────────────────────────────────────
    const experiments = new Collection({
        name: "experiments",
        type: "base",
        schema: [
            { name: "experiment_id",    type: "text",   required: true },
            { name: "title",            type: "text",   required: true },
            { name: "hypothesis",       type: "text",   required: true },
            { name: "prediction",       type: "text",   required: false },
            { name: "method",           type: "text",   required: false },
            { name: "status",           type: "text",   required: true },
            { name: "result",           type: "text",   required: false },
            { name: "started_at",       type: "date",   required: true },
            { name: "concluded_at",     type: "date",   required: false },
            { name: "linked_note",      type: "text",   required: false },
            { name: "linked_sections",  type: "json",   required: false, options: { maxSize: 2000000 }},
            { name: "tags",             type: "json",   required: false, options: { maxSize: 2000000 }},
        ],
        indexes: [
            "CREATE UNIQUE INDEX idx_exp_eid ON experiments (experiment_id)",
            "CREATE INDEX idx_exp_status ON experiments (status)",
        ],
        listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: null,
    });
    dao.saveCollection(experiments);

    // ── linked_docs ──────────────────────────────────────────────────
    const linked = new Collection({
        name: "linked_docs",
        type: "base",
        schema: [
            { name: "parent_note",      type: "text",   required: true },
            { name: "parent_section",   type: "text",   required: true },
            { name: "child_path",       type: "text",   required: true },
            { name: "relationship",     type: "text",   required: true },
            { name: "created_at",       type: "date",   required: true },
            { name: "summary",          type: "text",   required: false },
        ],
        indexes: [
            "CREATE INDEX idx_linked_parent ON linked_docs (parent_note, parent_section)",
            "CREATE INDEX idx_linked_child ON linked_docs (child_path)",
        ],
        listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: null,
    });
    dao.saveCollection(linked);

}, (db) => {
    // Rollback: drop in reverse order
    const dao = new Dao(db);
    for (const name of ["linked_docs", "experiments", "section_operations", "section_snapshots", "daily_sessions"]) {
        try {
            const c = dao.findCollectionByNameOrId(name);
            dao.deleteCollection(c);
        } catch (e) { /* already gone */ }
    }
});
