/// <reference path="../pb_data/types.d.ts" />
/**
 * SimpleAgentOS: Agent Journal scratch collection v0.0.1
 *
 * `agent_journal_test` is a throwaway twin of `agent_journal`, identical in
 * schema so the same client code exercises the same paths. It exists because
 * the live tests in both SimpleAgentOS and NovaSystem used to write into the
 * real journal, which buried the handful of genuine entries under pytest and
 * novatest rows.
 *
 * One deliberate difference: `deleteRule` is "" here, where the real journal
 * leaves it null (admin only). The real memory is append only on purpose.
 * Scratch data has to be able to clean up after itself, so tests delete their
 * own rows in fixture teardown and the collection stays near empty.
 *
 * Nothing in the harness writes here. Only tests, via PB_JOURNAL_COLLECTION
 * (SimpleAgentOS) or NOVA_JOURNAL_COLLECTION (NovaSystem).
 */

migrate((db) => {
    const dao = new Dao(db);

    const scratch = new Collection({
        name: "agent_journal_test",
        type: "base",
        schema: [
            { name: "entry_id",     type: "text",   required: true,  options: { min: 1 } },
            { name: "content_hash", type: "text",   required: false },
            { name: "occurred_at",  type: "date",   required: true },
            { name: "kind",         type: "text",   required: true },
            { name: "actor",        type: "text",   required: false },
            { name: "source",       type: "text",   required: false },
            { name: "project",      type: "text",   required: false },
            { name: "session_id",   type: "text",   required: false },
            { name: "title",        type: "text",   required: false, options: { max: 500 } },
            { name: "body",         type: "text",   required: false },
            { name: "tags",         type: "json",   required: false, options: { maxSize: 20000 } },
            { name: "path_ref",     type: "text",   required: false },
            { name: "importance",   type: "number", required: false },
            { name: "metadata",     type: "json",   required: false, options: { maxSize: 2000000 } },
        ],
        indexes: [
            "CREATE UNIQUE INDEX idx_agent_journal_test_entry ON agent_journal_test (entry_id)",
            "CREATE INDEX idx_agent_journal_test_occurred ON agent_journal_test (occurred_at)",
            "CREATE INDEX idx_agent_journal_test_kind ON agent_journal_test (kind)",
        ],
        listRule: "", viewRule: "", createRule: "", updateRule: "", deleteRule: "",
    });

    return dao.saveCollection(scratch);
}, (db) => {
    const dao = new Dao(db);
    const scratch = dao.findCollectionByNameOrId("agent_journal_test");
    return dao.deleteCollection(scratch);
});
