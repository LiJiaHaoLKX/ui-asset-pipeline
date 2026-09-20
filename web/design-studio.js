(() => {
  const el = id => document.getElementById(id);
  const fields = {product: 'studioProduct', audience: 'studioAudience', platform: 'studioPlatform', canvas: 'studioCanvas', style: 'studioStyle', constraints: 'studioConstraints'};
  let data = null, dirty = false, working = false, timer;
  const current = () => data?.versions.find(v => v.id === data.current);
  const status = (text, error = false) => { el('studioStatus').textContent = text; el('studioStatus').classList.toggle('studio-error', error); };
  async function request(path = '', body) {
    const response = await fetch('/api/design-studio' + path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '操作失败');
    return result;
  }
  function render(fill = false) {
    if (!data) return;
    if (fill && !dirty) {
      for (const [key, id] of Object.entries(fields)) el(id).value = data.brief[key] || (key === 'platform' ? '小程序' : key === 'canvas' ? '752x1344' : '');
      el('studioModel').value = data.previewModel;
    }
    const busy = Boolean(working || data.busy);
    el('designStudio').setAttribute('aria-busy', String(busy));
    el('studioInputs').disabled = busy;
    for (const id of ['studioPreview', 'studioAdjust', 'studioAdopt']) el(id).disabled = busy;
    el('studioReferences').replaceChildren();
    for (const ref of data.references) {
      const card = document.createElement('figure');
      const image = document.createElement('img'); image.src = ref.url; image.alt = ref.name;
      const caption = document.createElement('figcaption'); caption.textContent = ref.name;
      const button = document.createElement('button'); button.type = 'button'; button.className = 'text-button'; button.textContent = '移除'; button.disabled = busy;
      button.onclick = () => action(async () => { data = await request('/remove', {id: ref.id}); render(); status('参考图已移除，请重新生成规范'); });
      card.append(image, caption, button); el('studioReferences').append(card);
    }
    const version = current();
    el('studioProposal').hidden = !version;
    if (!version) return;
    el('studioVersion').textContent = `02 · 规范草稿 v${version.number}`;
    el('studioAdopted').textContent = data.adopted === version.id ? '已确立' : '待确认';
    el('studioSummary').textContent = version.summary;
    el('studioSpec').textContent = JSON.stringify(version.spec, null, 2);
    el('studioPrompt').textContent = version.previewPrompt;
    el('studioPreviewImage').hidden = !version.previewUrl;
    if (version.previewUrl) el('studioPreviewImage').src = version.previewUrl;
    else el('studioPreviewImage').removeAttribute('src');
    el('studioPreviewEmpty').hidden = Boolean(version.previewUrl);
    el('studioAdopt').disabled = busy || dirty || !version.previewId || version.inputRevision !== data.revision || data.adopted === version.id;
    el('studioPreview').disabled = busy || dirty || version.inputRevision !== data.revision;
    el('studioHistory').replaceChildren();
    for (const item of [...data.versions].reverse()) {
      const row = document.createElement('details');
      const heading = document.createElement('summary'); heading.textContent = `v${item.number}${item.id === data.adopted ? ' · 已确立' : ''} · ${item.feedback || '初始生成'}`;
      const text = document.createElement('pre'); text.textContent = JSON.stringify(item.spec, null, 2);
      row.append(heading, text);
      if (item.previewUrl) { const link = document.createElement('a'); link.href = item.previewUrl; link.target = '_blank'; link.textContent = '查看该版本预览'; row.append(link); }
      el('studioHistory').append(row);
    }
  }
  async function action(fn) {
    if (working) return;
    working = true; render();
    try { await fn(); } catch (error) { status(error.message, true); }
    finally { working = false; render(); }
  }
  async function save() {
    const brief = Object.fromEntries(Object.entries(fields).map(([key, id]) => [key, el(id).value]));
    if (!data || dirty) data = await request('/brief', {brief, previewModel: el('studioModel').value});
    dirty = false;
  }
  async function poll(jobId) {
    for (;;) {
      const response = await fetch('/api/jobs/' + encodeURIComponent(jobId));
      const job = await response.json();
      if (!response.ok) throw new Error('任务连接已中断，请刷新检查已保存的结果');
      if (job.status === 'failed') throw new Error(job.message);
      if (job.status === 'completed') return;
      await new Promise(resolve => setTimeout(resolve, 1800));
    }
  }
  async function run(kind, feedback = '') {
    if (kind === 'generate' && !el('studioProduct').value.trim()) return status('请先填写产品用途', true);
    if (kind === 'preview' && (dirty || current()?.inputRevision !== data.revision)) return status('问答或参考图已修改，请先重新生成规范，再预览', true);
    if (!confirm(kind === 'generate' ? '将把问答及参考图发送给文本模型生成设计规范，是否继续？' : `将使用 ${data.previewModel} 生成一张样式预览，会产生 API 费用，是否继续？`)) return;
    await action(async () => {
      if (kind === 'generate') await save();
      status(kind === 'generate' ? 'AI 正在整理专业设计规范…' : '正在生成样式预览，请稍候…');
      const result = await request('/' + kind, {confirmed: true, feedback, version: current()?.id});
      try { await poll(result.jobId); status(kind === 'generate' ? '规范已生成，请生成预览查看效果' : '预览已生成，可提出调整意见或确立规范'); }
      finally { data = await request(); render(); }
    });
  }
  for (const id of [...Object.values(fields), 'studioModel']) el(id).addEventListener('input', () => { dirty = true; el('studioAdopt').disabled = true; el('studioPreview').disabled = true; });
  el('studioSave').onclick = () => action(async () => { await save(); render(); status('问答已保存'); });
  el('studioGenerate').onclick = () => run('generate');
  el('studioPreview').onclick = () => run('preview');
  el('studioAdjust').onclick = () => {
    const feedback = el('studioFeedback').value.trim();
    if (!feedback) return status('请先描述希望调整的内容', true);
    run('generate', feedback);
  };
  el('studioAdopt').onclick = () => {
    if (!confirm('确立当前规范后，后续页面生成及新建 Codex 任务会使用它。已有页面与任务保持原样。确认？')) return;
    action(async () => { await request('/adopt', {version: current().id}); data = await request(); el('projectDesignSpec').value = JSON.stringify(current().spec, null, 2); status('已确立为项目规范，后续页面共用此规范'); });
  };
  el('studioUpload').onchange = event => {
    const files = [...event.target.files];
    action(async () => {
      if (files.length + data.references.length > 5) throw new Error('最多添加 5 张参考图');
      for (const file of files) {
        if (file.size > 4 * 1024 * 1024) throw new Error(`${file.name} 超过 4 MB`);
        const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
        data = await request('/upload', {name: file.name, data: encoded});
      }
      status('参考图已上传，生成规范时会参与分析');
    }).finally(() => { event.target.value = ''; render(); });
  };
  window.loadDesignStudio = async () => {
    try {
      data = await request(); render(true);
      clearTimeout(timer);
      if (!working && data.task) status(data.task.message, data.task.status === 'failed');
      if (data.busy && !working) { status('项目设计规范任务正在执行…'); timer = setTimeout(window.loadDesignStudio, 2000); }
    } catch (error) { status(error.message, true); }
  };
})();
