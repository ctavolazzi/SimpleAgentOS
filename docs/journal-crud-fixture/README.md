# Isolated browser journal CRUD fixture

This is an executable browser contract experiment, not SimpleAgent's recorder implementation. It uses synthetic records in browser localStorage. No real model, PocketBase, production journal, or credentials are involved.

From SimpleAgentOS root:

```sh
python3 -m unittest discover -s tests/browser_journal -v
```

Requires Python Playwright and its Chromium binary. Verified with Playwright 1.60.0. If absent, install in a dedicated test environment:

```sh
python3 -m pip install playwright==1.60.0
python3 -m playwright install chromium
```

The suite fulfills `https://journal.test/` inside Playwright. No server, DNS lookup, or remote connection is needed. Other requests are blocked and fail the test. Each test starts a fresh browser context and isolated storage.

For manual use, serve only the fixture directory on loopback:

```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory docs/journal-crud-fixture
```

Open `http://127.0.0.1:8765`. Manual records remain in that browser origin under `simpleagent.crud-fixture.v1`. Automated tests cannot reach that storage. Mobile testing means a Chromium mobile-sized viewport, not a claim of Safari or physical-device coverage.

## Contracts exercised

| Scenario | Required observation |
|---|---|
| Full CRUD, desktop and mobile | Create through the form, reload/read, update/reload, confirmed delete/reload |
| Record identity | Update preserves ID, creation time, journal ID, and original author |
| Notes and content | Multiline text, Unicode, Markdown, and JSON-looking notes survive |
| Cancellation | Cancel edits and dismiss deletion without modifying storage |
| Invalid input | Blank title, malformed metadata, and oversized record leave storage unchanged |
| Journal switching | Data stays in its original journal; attachment changes persist |
| Agent attribution | A different editor does not become the original author |
| Rehydration | Fresh browser context restores explicit storage snapshot |
| Storage failure | Failed update or deletion reports failure and preserves stored record |
| Corrupt storage | No automatic overwrite; writing stays disabled |
| Content rendering | HTML-shaped text does not create executable elements |
| Stale tab | A previously observed revision cannot overwrite a newer saved revision |

Deletion asks for confirmation and stores the deleted record's complete snapshot in `backups` in the same localStorage write. Tests verify that backup equals the latest saved record. This is a fixture recovery aid, not a device-loss backup.

The revision check prevents the specific sequential stale-tab overwrite tested. localStorage read/check/write is not a transactional compare-and-swap; simultaneous writers can still race. Do not use this fixture to claim production concurrency, crash durability, authentication, agent isolation, append-only semantics, in-flight swap correctness, or PocketBase integration.

## Negative control

```sh
CRUD_MUTATION=no-delete python3 -m unittest discover -s tests/browser_journal -p test_crud.py -k test_full_crud_desktop -v
```

This changes only the HTML served inside that test process: deletion reports success and retains its backup, but fails to remove the record. Expected result: exit 1 at the post-reload absence assertion. Run without the environment variable to restore normal behavior. No source restoration or live record deletion is involved.

Update `index.html` and the behavioral assertions together only when the intended contract changes. The graph/map suite remains independent. Persistent journal lifecycle and production deletion policies are still separate design decisions.
