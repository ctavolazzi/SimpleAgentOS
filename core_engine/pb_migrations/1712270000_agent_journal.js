/// <reference path="../pb_data/types.d.ts" />
/**
 * SimpleAgentOS: Agent Journal (queryable memory) v0.0.1
 *
 * One collection, one row per remembered thing:
 *   agent_journal: append-only journal the OS writes to and queries back.
 *
 * This is deliberately ONE wide collection rather than a normalized schema.
 * Memory recall is "find me the thing that mentioned X", not a join.
 * `kind` + `tags` carry the structure; `body` carries the substance.
 *
 * Access rules are "" (public), same as the other collections in this
 * PocketBase. That means anyone who can reach the port can read/write it,
 * so the server MUST stay bound to 127.0.0.1. Both ways of starting it do
 * that: `pb_journal.py serve` and the launchd plist in core_engine/.
 * Do not expose this instance on a LAN without tightening these rules.
 */

migrate((db) => {
    const dao = new Dao(db);

    const journal = new Collection({
        name: "agent_journal",
        type: "base",
        schema: [
            // Identity + dedupe
            { name: "entry_id",     type: "text",   required: true,  options: { min: 1 } },
            { name: "content_hash", type: "text",   required: false },

            // When
            { name: "occurred_at",  type: "date",   required: true },

            // What kind of memory this is
            // note|finding|decision|question|event|session|error|commit|artifact|reflection
            { name: "kind",         type: "text",   required: true },

            // Who / where it came from
            { name: "actor",        type: "text",   required: false },
            { name: "source",       type: "text",   required: false },
            { name: "project",      type: "text",   required: false },
            { name: "session_id",   type: "text",   required: false },

            // The substance
            { name: "title",        type: "text",   required: false, options: { max: 500 } },
            { name: "body",         type: "text",   required: false },

            // Retrieval handles
            { name: "tags",         type: "json",   required: false, options: { maxSize: 20000 } },
            { name: "path_ref",     type: "text",   required: false },
            { name: "importance",   type: "number", required: false },

            // Anything else the caller wants to keep
            { name: "metadata",     type: "json",   required: false, options: { maxSize: 2000000 } },
        ],
        indexes: [
            "CREATE UNIQUE INDEX idx_agent_journal_entry ON agent_journal (entry_id)",
            "CREATE INDEX idx_agent_journal_occurred ON agent_journal (occurred_at)",
            "CREATE INDEX idx_agent_journal_kind ON agent_journal (kind)",
            "CREATE INDEX idx_agent_journal_project ON agent_journal (project)",
            "CREATE INDEX idx_agent_journal_session ON agent_journal (session_id)",
        ],
        listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: null,
    });

    return dao.saveCollection(journal);
}, (db) => {
    const dao = new Dao(db);
    const journal = dao.findCollectionByNameOrId("agent_journal");
    return dao.deleteCollection(journal);
});
