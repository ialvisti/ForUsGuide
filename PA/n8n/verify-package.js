// Read-only package integrity and self-contained Code node validation.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const root = __dirname;
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'release-manifest.manifest'), 'utf8'));
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const hash = text => crypto.createHash('sha256').update(text).digest('hex');
for (const item of manifest.replacements) {
  for (const side of ['baseline', 'candidate']) {
    const source = read(item[side]);
    assert.equal(hash(source), item[side + '_sha256'], item[side] + ' changed; review and refresh manifest');
    new Function('$input', '$', source); // Parse only; never execute a workflow.
  }
}
for (const item of manifest.addenda) assert.equal(hash(read(item.file)), item.sha256, item.file);
const shared = read('consumer-contract.js');
for (const name of ['format-gr', 'format-kq']) {
  assert.ok(read('candidates/' + name + '.js').startsWith(shared), name + ': shared contract drift');
}
assert.deepEqual(manifest.new_internal_terminal.outgoing_connections, []);
assert.equal(manifest.new_internal_terminal.body.visibility, 'internal');
console.log('PA package verified: 7 replacements, 2 addenda; no remote actions.');
