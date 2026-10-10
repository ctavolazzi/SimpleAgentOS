"""Offline graph controls and browser interaction smoke check; no server needed."""
import copy
import importlib.util
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('atlas', HERE / 'map.py')
atlas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(atlas)
data = json.loads((HERE / 'graph.json').read_text())

# Negative control: the validator must catch a real broken relationship.
broken = copy.deepcopy(data)
broken['edges'][0]['target'] = 'missing-node'
try:
    atlas.validate(broken)
except AssertionError as exc:
    assert 'Dangling edge' in str(exc)
else:
    raise AssertionError('Validator accepted a dangling edge')
atlas.validate(data)
route = atlas.walk(data, 'journal', 2, 'out')
assert 'sync-race' in route[1]['nodes']
assert 'outbox' in route[2]['nodes']
assert len([n for layer in route for n in layer['nodes']]) == len({n for layer in route for n in layer['nodes']})

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': 1440, 'height': 1100}, offline=True)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(atlas.DEFAULT_OUTPUT.as_uri())
    assert page.locator('#nodes [data-node-id]').count() == 9
    expect(page.locator('#inspector h2')).to_have_text('SimpleAgent recorder')
    page.get_by_role('button', name='Evidence & gaps', exact=True).click()
    page.locator('[data-node-id="sync-race"]').click()
    page.wait_for_function("location.hash.includes('node=sync-race')")
    expect(page.locator('#inspector h2')).to_have_text('Lost append during sync')
    page.reload()
    expect(page.locator('#inspector h2')).to_have_text('Lost append during sync')
    page.get_by_role('button', name='→ Durable outbox', exact=False).click()
    expect(page.locator('#inspector h2')).to_have_text('Durable outbox')
    page.go_back()
    page.wait_for_function("document.querySelector('#inspector h2').textContent==='Lost append during sync'")
    page.locator('#search').fill('no-such-node-12345')
    assert page.locator('#results').inner_text().startswith('No matches.')
    page.locator('#search').fill('')
    page.locator('#filter').select_option('reproduced')
    assert page.locator('#results button').count() == 3
    page.locator('#filter').select_option('all')
    page.locator('[data-node-id="brain"]').focus()
    page.keyboard.press('Enter')
    expect(page.locator('#inspector h2')).to_have_text('Brain.call()')
    page.get_by_role('button', name='Proposed exchange', exact=True).click()
    expect(page.locator('#nodes [data-node-id]')).to_have_count(6)
    embedded = json.loads(page.locator('#graph-data').text_content())
    assert embedded == data, 'Generated HTML is stale'
    with page.expect_download() as download:
        page.get_by_role('button', name='Download graph JSON').click()
    assert download.value.suggested_filename == 'simpleagent-graph.json'
    page.get_by_role('button', name='Code landscape', exact=True).click()
    page.locator('[data-node-id="recorder"]').click()
    page.screenshot(path=str(HERE / 'preview-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Page overflows mobile viewport'
    page.locator('#search').fill('sync-race')
    page.locator('[data-index-node="sync-race"]').click()
    expect(page.locator('#inspector h2')).to_have_text('Lost append during sync')
    page.screenshot(path=str(HERE / 'preview-mobile.png'), full_page=True)
    assert not errors, errors
    browser.close()
print('Verified: negative control, graph traversal, offline HTML, views, deep links, history, search, status filters, keyboard selection, JSON export, mobile overflow, and zero browser errors.')
