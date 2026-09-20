(() => {
  const el = id => document.getElementById(id);
  let path = null, revision = null, cues = [], style = {}, dirty = false, saving = false;
  let session = 0;
  const status = text => { el('captionStatus').textContent = text; };
  function preview() {
    const time = el('captionVideo').currentTime;
    const cue = cues.find(cue => time >= Number(cue.start) && time < Number(cue.end));
    el('captionOverlay').textContent = cue?.text || '';
  }
  function previewStyle() {
    const overlay = el('captionOverlay');
    overlay.style.fontFamily = style.font_name || 'Arial';
    overlay.style.fontSize = `${Math.max(12, Number(style.font_size || 38) * 0.47)}px`;
    overlay.style.color = style.text_color || '#FFFFFF';
    const outline = Math.max(0, Number(style.outline || 0)) * 0.65;
    const shadow = Math.max(0, Number(style.shadow || 0)) * 0.8;
    const color = style.outline_color || '#000000';
    overlay.style.webkitTextStroke = `${outline}px ${color}`;
    overlay.style.textShadow = `${shadow}px ${shadow}px ${Math.max(1, shadow)}px ${color}`;
    overlay.style.top = style.position === 'top' ? '10%' : (style.position === 'middle' ? '45%' : 'auto');
    overlay.style.bottom = style.position === 'bottom' ? `${Math.max(6, Math.min(40, Number(style.margin || 150) / 9.6))}%` : 'auto';
  }
  function styleFields() {
    el('captionStyle').querySelectorAll('[data-style]').forEach(input => {
      const value = style[input.dataset.style];
      if (value !== undefined) input.value = value;
    });
    previewStyle();
  }
  function rows() {
    el('captionRows').innerHTML = cues.map((cue, i) => `<article class="caption-row" data-cue="${i}">
      <div class="caption-row-head"><button type="button" data-seek="${i}" class="frame-button">Play line ${i + 1}</button><button type="button" data-remove="${i}" class="text-btn" aria-label="Remove caption ${i + 1}">Remove</button></div>
      <div class="caption-times"><label>Start (seconds)<input type="number" min="0" step="0.01" data-field="start" value="${esc(cue.start)}" aria-label="Caption ${i + 1} start"></label><label>End (seconds)<input type="number" min="0" step="0.01" data-field="end" value="${esc(cue.end)}" aria-label="Caption ${i + 1} end"></label></div>
      <label>Caption ${i + 1}<textarea maxlength="500" rows="2" data-field="text">${esc(cue.text)}</textarea></label>
    </article>`).join('');
    preview();
  }
  function changed() {
    dirty = true;
    el('saveCaptions').disabled = false;
    status('Unsaved caption changes.');
    preview();
  }
  async function open(videoPath) {
    if (saving) return;
    const request = ++session;
    path = videoPath; dirty = false; cues = []; style = {}; revision = null;
    el('captionVideo').removeAttribute('src'); el('captionVideo').load();
    el('captionRows').replaceChildren(); el('captionOverlay').textContent = '';
    el('captionWorkspace').inert = true; el('saveCaptions').disabled = true;
    el('captionDialog').showModal(); status('Loading captions…');
    try {
      const response = await fetch('/api/captions?path=' + encodeURIComponent(path), {cache: 'no-store'});
      const data = await response.json();
      if (request !== session) return;
      if (!response.ok) throw new Error(data.error || 'Could not load captions.');
      revision = data.revision; cues = data.cues; style = data.style || {};
      el('captionVideo').src = '/media?path=' + encodeURIComponent(data.preview_path) + '&_=' + Date.now();
      el('captionWorkspace').inert = false;
      styleFields(); rows(); status('Edit the caption text, timing, and visual style below.');
    } catch (error) { if (request === session) status(error.message); }
  }
  function close() {
    if (saving) return status('Finishing the render. Please wait before closing.');
    if (dirty && !confirm('Discard your unsaved caption changes?')) return;
    session++; path = null;
    el('captionVideo').pause(); el('captionVideo').removeAttribute('src'); el('captionVideo').load();
    el('captionDialog').close();
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-caption-path]');
    if (button && !button.disabled) open(button.dataset.captionPath);
  });
  el('closeCaptions').addEventListener('click', close);
  el('captionDialog').addEventListener('cancel', event => { event.preventDefault(); close(); });
  el('captionVideo').addEventListener('timeupdate', preview);
  el('captionRows').addEventListener('input', event => {
    const row = event.target.closest('[data-cue]');
    if (!row || !event.target.dataset.field) return;
    const field = event.target.dataset.field;
    cues[Number(row.dataset.cue)][field] = field === 'text' ? event.target.value : (event.target.value === '' ? null : Number(event.target.value));
    changed();
  });
  el('captionStyle').addEventListener('input', event => {
    const field = event.target.dataset.style;
    if (!field) return;
    style[field] = ['font_size', 'outline', 'shadow', 'margin'].includes(field)
      ? Number(event.target.value) : event.target.value;
    previewStyle(); changed();
  });
  el('captionRows').addEventListener('click', event => {
    const seek = event.target.closest('[data-seek]');
    if (seek) {
      const time = Number(cues[Number(seek.dataset.seek)].start);
      if (!Number.isFinite(time) || time < 0) return status('Enter a valid start time first.');
      el('captionVideo').currentTime = time;
      el('captionVideo').play().catch(error => status(error.message));
    }
    const remove = event.target.closest('[data-remove]');
    if (remove) { cues.splice(Number(remove.dataset.remove), 1); rows(); changed(); }
  });
  el('addCaption').addEventListener('click', () => {
    const video = el('captionVideo');
    if (!Number.isFinite(video.duration)) return status('Wait for the video to load first.');
    const start = Math.round(video.currentTime * 100) / 100;
    const next = cues.find(cue => Number(cue.start) > start);
    if (cues.some(cue => start >= Number(cue.start) && start < Number(cue.end))) return status('There is already a caption here. Edit it, or move to a gap.');
    const end = Math.min(start + 2, next ? Number(next.start) : video.duration, video.duration);
    if (end <= start) return status('Move the playhead before the end of the video.');
    cues.push({start, end: Math.floor(end * 100) / 100, text: ''}); cues.sort((a,b) => a.start - b.start);
    rows(); changed();
    const index = cues.findIndex(cue => cue.start === start);
    el('captionRows').querySelector(`[data-cue="${index}"] textarea`).focus();
  });
  el('saveCaptions').addEventListener('click', async () => {
    if (!path || saving) return;
    saving = true; el('captionVideo').pause(); el('captionWorkspace').inert = true;
    el('saveCaptions').disabled = true; status('Rendering captions. This can take a few minutes…');
    try {
      const data = await postJson('/api/captions', {video_path: path, revision, cues, style});
      cues = data.cues; style = data.style || style; revision = data.revision; dirty = false;
      styleFields(); rows(); status('Saved. The local Short and future subtitle uploads now use these captions and style.');
      await loadState();
    } catch (error) { status(error.message); }
    finally { saving = false; el('captionWorkspace').inert = false; el('saveCaptions').disabled = !dirty; }
  });
})();
