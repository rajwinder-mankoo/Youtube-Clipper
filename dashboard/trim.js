(() => {
  const el = id => document.getElementById(id);
  let path = null;
  let duration = 0;
  let saving = false;
  let previewing = false;

  const format = value => {
    const safe = Math.max(0, Number(value) || 0);
    const minutes = Math.floor(safe / 60);
    return `${String(minutes).padStart(2, '0')}:${(safe % 60).toFixed(2).padStart(5, '0')}`;
  };
  const status = (message, error = false) => {
    el('trimStatus').textContent = message;
    el('trimStatus').classList.toggle('error', error);
  };
  function values() {
    return {start: Number(el('trimStart').value), end: Number(el('trimEnd').value)};
  }
  function renderValues(changed) {
    let {start, end} = values();
    if (changed === 'start' && start > end - 1) start = Math.max(0, end - 1);
    if (changed === 'end' && end < start + 1) end = Math.min(duration, start + 1);
    el('trimStart').value = String(start);
    el('trimEnd').value = String(end);
    el('trimStartValue').textContent = format(start);
    el('trimEndValue').textContent = format(end);
    el('trimDuration').textContent = `${(end - start).toFixed(2)} seconds`;
    el('saveTrimBtn').disabled = saving || end - start < 1 || end - start > 59.001;
  }
  async function open(videoPath) {
    if (saving) return;
    path = videoPath;
    const dialog = el('trimDialog');
    el('trimWorkspace').inert = true;
    el('saveTrimBtn').disabled = true;
    status('Loading the Short timeline…');
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch('/api/trim-info?path=' + encodeURIComponent(path), {cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not open the timeline editor.');
      duration = Number(data.duration);
      for (const id of ['trimStart', 'trimEnd']) el(id).max = String(duration);
      el('trimStart').value = '0';
      el('trimEnd').value = String(Math.min(duration, Number(data.max_duration) || 59));
      el('trimVideo').src = '/media?path=' + encodeURIComponent(data.video_path) + '&_=' + Date.now();
      el('trimWorkspace').inert = false;
      renderValues();
      status('Move the playhead and set the start and end points.');
    } catch (error) {
      status(error.message, true);
    }
  }
  function close() {
    if (saving) return status('Finishing the trim. Please wait before closing.');
    previewing = false;
    path = null;
    const video = el('trimVideo');
    video.pause(); video.removeAttribute('src'); video.load();
    el('trimDialog').close();
  }

  window.openTrimEditor = open;
  el('closeTrimBtn').addEventListener('click', close);
  el('trimDialog').addEventListener('cancel', event => { event.preventDefault(); close(); });
  el('trimStart').addEventListener('input', () => renderValues('start'));
  el('trimEnd').addEventListener('input', () => renderValues('end'));
  el('setTrimStart').addEventListener('click', () => {
    el('trimStart').value = String(el('trimVideo').currentTime || 0); renderValues('start');
  });
  el('setTrimEnd').addEventListener('click', () => {
    el('trimEnd').value = String(el('trimVideo').currentTime || duration); renderValues('end');
  });
  el('previewTrim').addEventListener('click', () => {
    const video = el('trimVideo');
    const {start} = values();
    previewing = true; video.currentTime = start;
    video.play().catch(error => status(error.message, true));
  });
  el('trimVideo').addEventListener('timeupdate', () => {
    if (!previewing) return;
    const {end} = values();
    if (el('trimVideo').currentTime >= end) {
      el('trimVideo').pause(); previewing = false;
    }
  });
  el('saveTrimBtn').addEventListener('click', async () => {
    if (!path || saving) return;
    const {start, end} = values();
    if (!confirm(`Keep ${format(start)} through ${format(end)}? Removed sections can only be recovered by regenerating the Short.`)) return;
    saving = true; previewing = false; el('trimVideo').pause();
    el('trimWorkspace').inert = true; el('saveTrimBtn').disabled = true;
    status('Trimming the Short and synchronizing its editing files…');
    try {
      const data = await postJson('/api/trim', {video_path: path, start, end});
      status(`Saved ${Number(data.duration).toFixed(2)}-second Short.`);
      await loadState();
      saving = false;
      close();
      toast('Short trimmed successfully.');
    } catch (error) {
      status(error.message, true);
    } finally {
      saving = false; el('trimWorkspace').inert = false; renderValues();
    }
  });
})();
