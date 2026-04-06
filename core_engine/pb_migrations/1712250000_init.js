migrate((db) => {
    const dao = new Dao(db);
    const collection = new Collection({
        "name": "transmissions",
        "type": "base",
        "schema": [
            { "name": "prompt", "type": "text" },
            { "name": "thoughts", "type": "text" },
            { "name": "response", "type": "text" }
        ],
        "listRule": "", "viewRule": "", "createRule": "", "updateRule": ""
    });
    return dao.saveCollection(collection);
})