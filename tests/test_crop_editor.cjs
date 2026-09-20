// Exercise the editor's timeline transitions without a media decoder or server.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', innerHTML: '', disabled: false,
    currentTime: 0, duration: 20, videoWidth: 0, paused: true,
    pause() { this.paused = true; }, classList: {toggle() {}},
  });
  return elements.get(id);
}
let script = fs.readFileSync('dashboard/app.js', 'utf8')
  .replace(/^bindEvents\(\);$/m, '')
  .replace(/^loadState\(\);$/m, '')
  .replace(/^technicalTimer = setInterval.*$/m, '')
  .replace(/^setInterval\(loadState.*$/m, '');
const context = vm.createContext({document: {getElementById: element}, console});
vm.runInContext(script, context);
const run = code => vm.runInContext(code, context);
run('frameKeyframes = normalizeFrameKeyframes([{time:0, center_x:0.5, center_y:0.5, zoom:1}]);');
element('frameSourceVideo').currentTime = 5;
run('framePosition = {centerX:0.7, centerY:0.5, zoom:1.2}; setCurrentKeyframe({quiet:true});');
assert.equal(run('frameKeyframes.length'), 2);
assert.equal(element('undoCropBtn').disabled, false);
run('showFrameAt(12, {selectExact:true});');
assert.equal(run('framePosition.centerX'), 0.7, 'Seek preserves the committed crop');
run('undoCropChange();');
assert.equal(run('frameKeyframes.length'), 1);
assert.equal(run('framePosition.centerX'), 0.5);
element('frameSourceVideo').currentTime = 0;
run('showFrameAt(0, {selectExact:true}); deleteCurrentKeyframe();');
assert.equal(run('frameKeyframes.length'), 1, 'Start point is retained');
assert.equal(element('deleteKeyframeBtn').disabled, true);
element('frameSourceVideo').currentTime = 5;
run('framePosition = {centerX:0.8, centerY:0.5, zoom:1}; setCurrentKeyframe({quiet:true});');
element('frameSourceVideo').currentTime = 5.02;
run('framePosition = {centerX:0.6, centerY:0.5, zoom:1}; setCurrentKeyframe({quiet:true});');
assert.equal(run('frameKeyframes.length'), 2, 'Nearby edits update the point');
assert.equal(run('frameKeyframes[1].time'), 5, 'Editing does not shift the point');
run('jumpCropPoint(-1);');
assert.equal(element('frameSourceVideo').currentTime, 0);
run('jumpCropPoint(1);');
assert.equal(element('frameSourceVideo').currentTime, 5);
run('deleteCurrentKeyframe(); undoCropChange();');
assert.equal(run('frameKeyframes.length'), 2, 'Removal can be undone');
console.log('Crop editor transition checks passed.');
