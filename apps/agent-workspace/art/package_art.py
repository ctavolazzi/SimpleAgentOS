"""Package reviewed exports into an immutable version directory."""
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'fieldwork-v2'
assets = ROOT / 'web/assets'
manifest = json.loads((ROOT / 'art/manifest.json').read_text())
for item in manifest['assets']:
    name = Path(item['path']).name
    source = assets / name
    target = assets / VERSION / name
    data = source.read_bytes()
    if target.exists() and target.read_bytes() != data:
        raise SystemExit('Existing art version differs. Create a new version; do not replace saved-world art.')
    target.parent.mkdir(exist_ok=True)
    if not target.exists():
        shutil.copyfile(source, target)
    item['path'] = VERSION + '/' + name
    item['sha256'] = hashlib.sha256(data).hexdigest()
(ROOT / 'art/manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print('Reviewed assets packaged as', VERSION)
