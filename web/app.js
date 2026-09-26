const stages = [
  { id: 'overview', label: '项目概览', phase: null, summary: '查看流水线状态与后台任务' },
  { id: 'design', label: '页面需求', phase: 'draft-reference', summary: '描述当前页面并生成图片提示词' },
  { id: 'reference', label: '参考图审核', phase: 'reference-approved', summary: '预览、上传与批准设计参考图' },
  { id: 'marker', label: '区域标注', phase: 'regions-marked', summary: 'AI 建议并人工校准元素类型、动作与层级' },
  { id: 'extract', label: '处理中心', phase: 'extraction-review', summary: '独立执行裁剪、透明化与背景补全' },
  { id: 'assets', label: '元素审核', phase: 'assets-approved', summary: '对比版本并批准图片素材与代码元素' },
  { id: 'code', label: '页面实现', phase: 'page-generated', summary: '编辑页面源码并在固定画布实时预览' },
  { id: 'compare', label: '对比校准', phase: 'comparison-ready', summary: '生成渲染图、重叠图和差异指标' },
];

const phaseOrder = ['draft-reference', 'reference-approved', 'regions-marked', 'crops-created', 'extraction-running', 'extraction-review', 'assets-approved', 'page-generated', 'comparison-ready', 'completed'];
const phaseLabels = {
  'draft-reference': '参考图准备中',
  'reference-approved': '参考图已批准',
  'regions-marked': '标注已保存',
  'crops-created': '裁剪已生成',
  'extraction-running': '素材提取中',
  'extraction-review': '素材待审核',
  'assets-approved': '素材已批准',
  'page-generated': '页面已生成',
  'comparison-ready': '对比待确认',
  completed: '项目已完成',
};

let state = null;
let activeView = location.hash.slice(1) || 'overview';
let currentSourceFile = 'index.html';
let sourceFiles = {};
let sourceRevision = '';
let hideCompletedJobs = false;
let noticeTimer = null;
let loadedPageId = null;
let assetFilter = 'all';
let aiExtractionPending = false;
const assetReviewEdits = new Map();

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
const elementTypeLabels = { 'image-asset': '图片素材', 'code-element': '代码元素', 'complete-composite': '完整复合图片' };
const processingModeLabels = { none: '不生成图片', 'complete-crop': '完整裁剪', 'local-transparent': '本地透明化', 'ai-transparent': 'AI 透明化', 'background-repair': 'AI 背景补全' };

function normalizeTarget(target = {}) {
  const legacy = { ai: 'ai-transparent', 'background-script': 'local-transparent' };
  let processingMode = target.processingMode || legacy[target.extractionMode] || target.extractionMode || 'ai-transparent';
  let elementType = target.elementType || (['complete-crop', 'background-repair'].includes(processingMode) ? 'complete-composite' : 'image-asset');
  if (elementType === 'code-element') processingMode = 'none';
  return { name: '', parent: '', zIndex: null, backgroundTolerance: 34, edgeFeather: 48, reviewStatus: 'confirmed', suggestion: { source: 'manual' }, approved: true, ...target, elementType, processingMode };
}

function modeForType(type, current = '') {
  if (type === 'code-element') return 'none';
  const imageModes = ['local-transparent', 'ai-transparent', 'complete-crop', 'background-repair'];
  if (imageModes.includes(current)) return current;
  return type === 'complete-composite' ? 'complete-crop' : 'local-transparent';
}

function notify(message, error = false) {
  const notice = $('#notice');
  notice.textContent = message;
  notice.className = `notice${error ? ' error' : ''}`;
  notice.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice.hidden = true; }, error ? 7000 : 3500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败：${response.status}`);
  return data;
}

function cacheBust(url) {
  if (!url) return '';
  return `${url}${url.includes('?') ? '&' : '?'}v=${Date.now()}`;
}

function activeFileUrl(relative) {
  return `/files/${state?.project?.fileBase || ''}${relative}`;
}

function phaseIndex(phase) { return phaseOrder.indexOf(phase); }

function renderNavigation() {
  const currentPhase = phaseIndex(state?.phase);
  $('#stageNav').innerHTML = stages.map((item, index) => {
    const complete = item.phase && currentPhase >= phaseIndex(item.phase);
    return `<button class="nav-button${item.id === activeView ? ' active' : ''}" data-view-target="${item.id}"><span class="nav-index">${String(index).padStart(2, '0')}</span><span>${item.label}</span><span class="nav-state">${complete ? '完成' : ''}</span></button>`;
  }).join('');
  $$('[data-view-target]').forEach(button => button.onclick = () => showView(button.dataset.viewTarget));
}

function showView(id) {
  if (id === 'config') id = 'project-settings';
  const projectSettings = id === 'project-settings';
  const stage = projectSettings ? { id, label: '项目设置', summary: '管理全局设计规范、模型和公共处理参数' } : stages.find(item => item.id === id) || stages[0];
  activeView = stage.id;
  location.hash = stage.id;
  $$('.view').forEach(view => view.classList.toggle('active', view.dataset.view === stage.id));
  $('#pageTitle').textContent = stage.label;
  $('#pageSummary').textContent = stage.summary;
  $('.page-switcher').hidden = projectSettings;
  $('#projectSettingsButton').classList.toggle('active', projectSettings);
  $('#phaseBadge').textContent = projectSettings ? '项目级' : phaseLabels[state.phase] || state.phase;
  renderNavigation();
  if (id === 'code' && (!Object.keys(sourceFiles).length || sourceRevision !== state.sourceRevision)) loadSource();
  if (id === 'design') loadDesign();
  if (projectSettings) loadProjectSettings();
}

function renderOverview() {
  $('#regionMetric').textContent = state.regionCount;
  $('#targetMetric').textContent = state.targetCount;
  $('#assetMetric').textContent = state.assets.assets.filter(item => item.approved).length;
  $('#iterationMetric').textContent = state.iterations.length;
  const current = phaseIndex(state.phase);
  $('#progressList').innerHTML = stages.slice(1).map((item, index) => {
    const itemPhase = phaseIndex(item.phase);
    const className = current > itemPhase ? 'complete' : current === itemPhase ? 'current' : '';
    const detail = current > itemPhase ? '已完成' : current === itemPhase ? '当前阶段' : '等待前序阶段';
    return `<button class="progress-row ${className}" data-go="${item.id}"><span class="number">${String(index + 1).padStart(2, '0')}</span><span>${item.label}</span><small>${detail}</small></button>`;
  }).join('');
  $$('[data-go]').forEach(button => button.onclick = () => showView(button.dataset.go));
  renderJobs();
}

function renderJobs() {
  let jobs = state.jobs || [];
  if (hideCompletedJobs) jobs = jobs.filter(job => !['completed'].includes(job.status));
  const list = $('#jobList');
  if (!jobs.length) {
    list.className = 'job-list empty';
    list.textContent = '暂无后台任务';
    return;
  }
  list.className = 'job-list';
  list.innerHTML = jobs.map(job => {
    const d = job.diagnostic;
    const details = d ? `<details class="job-diagnostic"><summary>${escapeHtml(d.origin)} · 查看排查依据</summary><p>${escapeHtml(d.conclusion)}</p><dl>${[
      ['失败阶段', d.stage], ['实际请求地址', d.endpoint || '历史记录未保存'],
      ['HTTP 状态', d.httpStatus ?? '未记录 HTTP 错误响应'], ['耗时', d.elapsedSeconds == null ? '未记录' : `${d.elapsedSeconds} 秒`],
      ['响应头 / 请求 ID', JSON.stringify(d.responseHeaders || {}, null, 2)],
      ['响应正文（节选）', d.responseExcerpt || '无正文或未记录'],
      ['本地记录', d.recordPath || '任务诊断已保留']
    ].map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd><pre>${escapeHtml(String(value))}</pre></dd>`).join('')}</dl></details>` : '';
    return `<div class="job-row"><div><strong>${escapeHtml(job.kind)}</strong><span>${escapeHtml(job.message)}</span>${details}</div><span class="job-state ${job.status}">${escapeHtml(job.status)}</span></div>`;
  }).join('');
}

function setImageModelOptions(models, selected = '') {
  const select = $('#configForm').elements.imageModel;
  const choices = [...new Set((models || []).filter(Boolean))];
  if (selected && !choices.includes(selected)) choices.unshift(selected);
  select.replaceChildren();
  if (!choices.length) {
    select.add(new Option('请先获取模型列表', ''));
    return;
  }
  choices.forEach(model => select.add(new Option(model, model)));
  select.value = selected && choices.includes(selected) ? selected : choices[0];
}

function renderConfig() {
  const form = $('#configForm');
  form.elements.imageBaseUrl.value = state.config.image.baseUrl || '';
  setImageModelOptions([], state.config.image.model || '');
  form.elements.textBaseUrl.value = state.config.text.baseUrl || '';
  form.elements.textModel.value = state.config.text.model || '';
  form.elements.generationSize.value = state.settings.generationSize || '752x1344';
  form.elements.extractionSize.value = state.settings.extractionSize || '1024x1024';
  form.elements.finalSize.value = state.settings.finalSize || '752x1344';
  form.elements.quality.value = state.settings.quality || 'medium';
  form.elements.padding.value = state.settings.padding ?? 32;
  form.elements.threshold.value = state.settings.threshold ?? 20;
  form.elements.codexMaxIterations.value = state.settings.codexMaxIterations ?? 8;
  $('#imageKeyHint').textContent = state.config.image.apiKeyConfigured ? '已配置密钥，留空不会覆盖' : '尚未配置密钥';
  $('#textKeyHint').textContent = state.config.text.apiKeyConfigured ? '已配置密钥，留空不会覆盖' : '尚未配置密钥';
  const ready = state.config.image.apiKeyConfigured && state.config.text.apiKeyConfigured;
  $('#apiIndicator').classList.toggle('ready', ready);
  $('#apiLabel').textContent = ready ? '图片与文本模型已配置' : '模型配置未完成';
}

function renderPageSelector() {
  const selector = $('#pageSelector');
  selector.innerHTML = state.project.pages.map(page => `<option value="${escapeHtml(page.id)}" ${page.id === state.project.activePageId ? 'selected' : ''}>${escapeHtml(page.name)}</option>`).join('');
}

function renderReference() {
  const image = $('#referenceImage');
  const empty = $('#referenceEmpty');
  if (state.reference) {
    image.src = cacheBust(state.reference.url);
    image.hidden = false;
    image.style.display = 'block';
    empty.hidden = true;
    $('#referenceDimensions').textContent = `${state.reference.width} × ${state.reference.height}`;
    $('#referenceStatus').textContent = state.referenceApproved ? '已批准' : '等待批准';
  } else {
    image.removeAttribute('src');
    image.hidden = true;
    image.style.display = 'none';
    empty.hidden = false;
    $('#referenceDimensions').textContent = '';
    $('#referenceStatus').textContent = '未生成';
  }
}

function renderCrops() {
  const gallery = $('#cropGallery');
  const counts = state.processingCounts || {};
  const pending = state.pendingProcessingCounts || {};
  const aiTargets = counts['ai-transparent'] || 0;
  const scriptTargets = counts['local-transparent'] || 0;
  const completeTargets = counts['complete-crop'] || 0;
  const repairTargets = counts['background-repair'] || 0;
  const codeTargets = counts.none || 0;
  const pendingTotal = Object.values(pending).reduce((sum, value) => sum + value, 0);
  const pendingRepair = pending['background-repair'] || 0;
  const extractionRunning = aiExtractionPending || (state.jobs || []).some(job => job.kind === 'extract-assets' && ['queued', 'running'].includes(job.status));
  const extractButton = $('#extractButton');
  $('#extractSummary').textContent = `AI 透明 ${aiTargets} / 本地 ${scriptTargets} / 完整裁剪 ${completeTargets} / 背景补全 ${repairTargets}${pendingRepair ? `（另 ${pendingRepair} 待确认）` : ''} / 代码 ${codeTargets}${pendingTotal ? ` · 全部待确认 ${pendingTotal}` : ''}`;
  extractButton.disabled = !aiTargets || extractionRunning;
  extractButton.classList.toggle('is-loading', extractionRunning);
  extractButton.setAttribute('aria-busy', String(extractionRunning));
  $('strong', extractButton).textContent = extractionRunning ? 'AI 透明化处理中' : '并发 AI 透明化';
  $('span', extractButton).textContent = extractionRunning ? `正在并发处理 ${aiTargets} 个素材` : '按绿色裁剪区并发';
  $('#scriptExtract').disabled = !(scriptTargets + completeTargets + codeTargets);
  $('#repairBackgrounds').disabled = !repairTargets;
  if (!state.crops.length) {
    gallery.className = 'media-grid empty';
    gallery.textContent = '尚未生成裁剪图';
    return;
  }
  gallery.className = 'media-grid';
  gallery.innerHTML = state.crops.map(crop => `<article class="media-item"><img src="${cacheBust(crop.markedUrl)}" alt="${escapeHtml(crop.id)} AI 红框标注图"><div class="media-caption"><span>${escapeHtml(crop.id)}</span><span>AI ${crop.aiTargetCount || 0} / 本地 ${crop.scriptTargetCount || 0} / 完整 ${crop.completeCropCount || 0} / 补全 ${crop.backgroundRepairCount || 0}</span></div></article>`).join('');
}

function markerModeCounts(mode) {
  const targets = marker.regions.flatMap(region => region.approved === false ? [] : region.targets.filter(target => target.approved !== false && target.processingMode === mode));
  return {
    confirmed: targets.filter(target => target.reviewStatus === 'confirmed').length,
    pending: targets.filter(target => target.reviewStatus !== 'confirmed').length,
  };
}

async function saveMarkerDraft() {
  if (!marker.dirty) return;
  await api('/api/regions', { method: 'POST', body: marker.payload() });
  marker.dirty = false;
  await refreshState(true);
}

function pendingRasterTargets() {
  return marker.regions.flatMap(region => region.approved === false ? [] : region.targets.filter(target =>
    target.approved !== false
    && ['image-asset', 'complete-composite'].includes(target.elementType)
    && target.reviewStatus !== 'confirmed'
  ));
}

async function ensureRasterTargetsConfirmed() {
  await saveMarkerDraft();
  const pending = pendingRasterTargets();
  if (!pending.length) return true;
  const names = pending.slice(0, 4).map(target => target.name || target.id || '未命名元素').join('、');
  notify(`还有 ${pending.length} 个图片素材或完整复合图片未人工确认：${names}${pending.length > 4 ? '…' : ''}`, true);
  return false;
}

function renderAssets() {
  const gallery = $('#assetGallery');
  const assets = state.assets.assets || [];
  const codeElements = state.assets.codeElements || [];
  const allItems = [...assets.map(item => ({ ...item, itemKind: 'asset' })), ...codeElements.map(item => ({ ...item, itemKind: 'code', elementType: 'code-element', processingMode: 'none' }))];
  $('#assetSummary').textContent = state.assets.source ? `${assets.length} 张图片，${codeElements.length} 个代码元素` : '';
  if (!allItems.length) {
    gallery.className = 'asset-review-list empty';
    gallery.textContent = '尚无待审核元素';
    return;
  }
  gallery.className = 'asset-review-list';
  gallery.innerHTML = allItems.map((asset, index) => {
    const editKey = `${asset.itemKind}:${asset.id}`;
    const edit = assetReviewEdits.get(editKey) || {};
    const bbox = edit.bbox || asset.placement?.bbox || {};
    const suggested = edit.name ?? (asset.name || `${asset.itemKind === 'code' ? 'code-element' : 'asset'}-${String(index + 1).padStart(3, '0')}`);
    const versions = asset.versions || [];
    const original = versions.find(item => item.id === 'original') || versions[0];
    const processed = versions.find(item => item.id === asset.activeVersion) || versions.at(-1);
    const preview = asset.itemKind === 'code'
      ? `<div class="code-element-preview"><strong>CODE</strong><span>${bbox.width || 0} × ${bbox.height || 0}</span></div>`
      : `<div class="version-compare"><figure class="checker"><img src="${cacheBust(original?.url || asset.url)}" alt="原始裁剪"><figcaption>原始裁剪</figcaption></figure><figure class="checker"><img src="${cacheBust(processed?.url || asset.url)}" alt="处理结果"><figcaption>${escapeHtml(processingModeLabels[asset.processingMode] || '处理结果')}</figcaption></figure></div>`;
    const hidden = assetFilter !== 'all' && asset.elementType !== assetFilter;
    const approved = edit.approved ?? asset.approved;
    const description = edit.description ?? asset.semanticDescription ?? asset.matchedSourceTarget?.purpose ?? '';
    const parent = edit.parent ?? asset.parent ?? '';
    const zIndex = edit.zIndex ?? asset.zIndex ?? '';
    return `<article class="asset-review-item${hidden ? ' filter-hidden' : ''}" data-asset-id="${escapeHtml(asset.id)}" data-item-kind="${asset.itemKind}" data-element-type="${escapeHtml(asset.elementType || 'image-asset')}"><div class="asset-review-preview">${preview}</div><div class="asset-review-fields"><div class="asset-review-heading"><div><strong>${escapeHtml(elementTypeLabels[asset.elementType] || '图片素材')}</strong><span>${escapeHtml(processingModeLabels[asset.processingMode] || asset.extractionMethod || '')}</span></div><label class="check-row"><input class="asset-approved" type="checkbox" ${approved ? 'checked' : ''}><span>批准</span></label></div><label><span>语义名称</span><input class="asset-name" value="${escapeHtml(suggested)}" placeholder="hero-carousel"></label><label><span>用途说明</span><input class="asset-description" value="${escapeHtml(description)}"></label><div class="asset-meta-grid"><label><span>父级</span><input class="asset-parent" value="${escapeHtml(parent)}" placeholder="可留空"></label><label><span>层级</span><input class="asset-z-index" type="number" value="${escapeHtml(zIndex)}" placeholder="0"></label></div><div class="bbox-editor"><span>页面坐标</span>${['x', 'y', 'width', 'height'].map(key => `<label><small>${key}</small><input class="asset-bbox" data-bbox-key="${key}" type="number" value="${bbox[key] ?? ''}"></label>`).join('')}</div><div class="asset-provenance">${escapeHtml(asset.sourceRegion || '')}${asset.sourceTarget ? ` / ${escapeHtml(asset.sourceTarget)}` : ''}${versions.length ? ` · ${versions.length} 个版本` : ''}</div></div></article>`;
  }).join('');
}

function captureAssetReviewEdits() {
  $$('.asset-review-item', $('#assetGallery')).forEach(item => {
    const bbox = {};
    $$('.asset-bbox', item).forEach(input => { if (input.value !== '') bbox[input.dataset.bboxKey] = Number(input.value); });
    assetReviewEdits.set(`${item.dataset.itemKind}:${item.dataset.assetId}`, {
      approved: $('.asset-approved', item).checked,
      name: $('.asset-name', item).value.trim(),
      description: $('.asset-description', item).value.trim(),
      parent: $('.asset-parent', item).value.trim(),
      zIndex: $('.asset-z-index', item).value === '' ? null : Number($('.asset-z-index', item).value),
      bbox: Object.keys(bbox).length === 4 ? bbox : null,
    });
  });
}

function renderComparison() {
  const panel = $('#comparisonPanel');
  const latest = state.iterations[0];
  if (!latest) {
    panel.className = 'comparison empty';
    panel.textContent = '尚无对比结果';
    return;
  }
  const metrics = latest.metrics || {};
  panel.className = 'comparison';
  panel.innerHTML = `<figure><img src="${cacheBust(state.reference?.url)}" alt="参考图"><figcaption>参考图</figcaption></figure><figure><img src="${cacheBust(latest.renderUrl)}" alt="浏览器渲染图"><figcaption>渲染图 ${escapeHtml(latest.id)}</figcaption></figure><figure><img src="${cacheBust(latest.overlayUrl)}" alt="半透明重叠图"><figcaption>重叠图</figcaption></figure><div class="metrics-line"><span>变化像素比例 ${metrics.changedRatio === undefined ? '-' : (metrics.changedRatio * 100).toFixed(2) + '%'}</span><span>平均亮度误差 ${metrics.meanAbsoluteLumaError === undefined ? '-' : Number(metrics.meanAbsoluteLumaError).toFixed(2)}</span><span>阈值 ${metrics.threshold ?? '-'}</span><a href="${latest.diffUrl}" target="_blank">查看差异图</a></div>`;
}

const codexTaskStatusLabels = {
  queued: '等待 Codex 领取',
  running: 'Codex 正在执行',
  'awaiting-human-approval': '等待人工审核',
  completed: '已批准',
  failed: '执行失败',
  cancelled: '已取消',
};

function codexInstruction(task) {
  const action = task.type === 'implementation'
    ? '先调用 get_task_context，完整读取其中的 projectDesignSpec、pageRequirements、aiGeneratedImagePrompt、requiredWorkflow 和 rasterAssetRules。然后必须调用 get_reference_image 查看实际参考图；调用 list_asset_images，并对列表中的每个素材依次调用 get_asset_image 和 record_asset_observation。素材若包含文字、徽标、按钮或装饰，应作为完整位图使用，严禁在 HTML/CSS 中重复实现素材里已有的内容。完成全部视觉检查后才能调用 get_source_files 和 write_source_files。完成页面后调用 submit_for_review。'
    : '先调用 get_task_context 并读取项目规范、页面需求和 AI 图片提示词，再调用 get_reference_image 查看实际参考图；读取最新渲染图、重叠图、差异图和指标，逐轮修改源码并调用 create_comparison。达到要求或最大轮数后调用 submit_for_review。';
  return `使用 ui_asset_pipeline MCP。先调用 claim_task 领取任务 ${task.id}。${action}`;
}

function persistentCodexInstruction() {
  return '使用 ui_asset_pipeline MCP 进入常驻工作模式。立即调用 wait_for_next_task(timeout_seconds=50)，挂起等待浏览器创建任务的本地事件。如果返回任务，调用 claim_task 领取返回的准确 taskId，再严格执行 get_task_context 中的 requiredWorkflow、rasterAssetRules 和 acceptanceCriteria；必须读取 projectDesignSpec、pageRequirements、aiGeneratedImagePrompt，并通过 MCP 实际查看要求的图片。完成任务后调用 submit_for_review，然后立即再次调用 wait_for_next_task。delivery 为 keepalive-timeout 只表示连接保活，不代表没有后续工作；不要回复、不要退出，继续调用 wait_for_next_task。持续处理后续所有浏览器任务。只有当我在这个会话中明确发送“停止常驻模式”或“结束常驻模式”时，才停止等待并结束本次会话。';
}

async function copyPersistentCodexInstruction() {
  try {
    await navigator.clipboard.writeText(persistentCodexInstruction());
    notify('常驻会话指令已复制，只需在新 Codex 会话中发送一次');
  } catch (error) {
    notify(`复制失败：${error.message}`, true);
  }
}

async function copyCodexInstruction(task) {
  try {
    await navigator.clipboard.writeText(codexInstruction(task));
    notify('Codex 指令已复制，可以粘贴到新会话');
  } catch (error) {
    notify(`复制失败：${error.message}`, true);
  }
}

function renderCodexTaskPanel(type) {
  const panel = type === 'implementation' ? $('#implementationTaskPanel') : $('#calibrationTaskPanel');
  const tasks = (state.codexTasks || []).filter(task => task.type === type);
  const task = tasks[0];
  const activeTask = (state.codexTasks || []).find(item => ['queued', 'running', 'awaiting-human-approval'].includes(item.status));
  const createButton = type === 'implementation' ? $('#createImplementationTask') : $('#createCalibrationTask');
  createButton.disabled = Boolean(activeTask);
  if (!task) {
    panel.className = 'codex-task-panel empty-task';
    panel.innerHTML = `<div><strong>尚未创建 Codex ${type === 'implementation' ? '实现' : '校准'}任务</strong><span>创建后，将任务指令复制到你手动新建的 Codex 会话。</span></div>`;
    return;
  }
  const events = (task.events || []).map(event => `<li><time>${escapeHtml(event.createdAt || '')}</time><span>${escapeHtml(event.message || event.event)}</span></li>`).join('');
  const canCopy = ['queued', 'running'].includes(task.status);
  const awaiting = task.status === 'awaiting-human-approval';
  const cancellable = ['queued', 'running', 'awaiting-human-approval'].includes(task.status);
  const detail = task.error || task.summary || (task.type === 'calibration' ? `已完成 ${task.iterationCount} / ${task.maxIterations} 轮` : '等待任务进展');
  panel.className = 'codex-task-panel';
  panel.innerHTML = `
    <div class="task-main">
      <div class="task-heading"><strong>${type === 'implementation' ? '页面实现任务' : '对比校准任务'}</strong><span class="task-status ${escapeHtml(task.status)}">${escapeHtml(codexTaskStatusLabels[task.status] || task.status)}</span></div>
      <code>${escapeHtml(task.id)}</code><p>${escapeHtml(detail)}</p>
    </div>
    <div class="task-actions">
      ${canCopy ? '<button class="secondary" data-task-action="copy">复制 Codex 指令</button>' : ''}
      ${awaiting ? '<button class="primary" data-task-action="approve">批准结果</button><button class="secondary" data-task-action="reject">要求继续修改</button>' : ''}
      ${cancellable ? '<button class="danger-text" data-task-action="cancel">取消任务</button>' : ''}
    </div>
    ${awaiting ? '<label class="task-note"><span>审核说明</span><input placeholder="可选，填写通过说明或需要继续修改的内容"></label>' : ''}
    <details class="task-events"><summary>执行记录</summary><ol>${events || '<li><span>暂无记录</span></li>'}</ol></details>`;
  $$('[data-task-action]', panel).forEach(button => button.onclick = async () => {
    const action = button.dataset.taskAction;
    if (action === 'copy') return copyCodexInstruction(task);
    const note = $('.task-note input', panel)?.value.trim() || '';
    if (action === 'cancel' && !confirm(`确认取消任务 ${task.id}？`)) return;
    try {
      if (action === 'approve' || action === 'reject') {
        await api(`/api/codex-tasks/${task.id}/review`, { method: 'POST', body: { approved: action === 'approve', note } });
      } else if (action === 'cancel') {
        await api(`/api/codex-tasks/${task.id}/cancel`, { method: 'POST', body: {} });
      }
      await refreshState(true);
      if (type === 'implementation' && action === 'approve') await loadSource();
      notify(action === 'approve' ? 'Codex 结果已批准' : action === 'reject' ? '任务已退回等待重新领取' : '任务已取消');
    } catch (error) { notify(error.message, true); }
  });
}

async function createCodexTask(type) {
  try {
    const result = await api('/api/codex-tasks', { method: 'POST', body: { type, maxIterations: state.settings.codexMaxIterations ?? 8 } });
    await refreshState(true);
    await copyCodexInstruction(result.task);
  } catch (error) { notify(error.message, true); }
}

const promptSubmissions = new Set();
function renderPromptLoading() {
  const busy = promptSubmissions.has(state?.project?.activePageId) || (state?.jobs || []).some(job => job.kind === 'generate-image-prompt' && ['queued', 'running'].includes(job.status));
  const button = $('#generatePrompt');
  button.disabled = busy;
  button.classList.toggle('is-loading', busy);
  button.setAttribute('aria-busy', String(busy));
  button.textContent = busy ? '正在生成提示词…' : 'AI 生成提示词';
  for (const id of ['saveDesign', 'generateReference', 'promptNotes', 'designPrompt']) $('#' + id).disabled = busy;
}

function renderAll() {
  renderPromptLoading();
  $('#phaseBadge').textContent = activeView === 'project-settings' ? '项目级' : phaseLabels[state.phase] || state.phase;
  renderNavigation();
  renderOverview();
  renderConfig();
  renderPageSelector();
  renderReference();
  renderCrops();
  renderAssets();
  renderComparison();
  renderCodexTaskPanel('implementation');
  renderCodexTaskPanel('calibration');
  marker.loadState(state);
}

async function refreshState(silent = false) {
  try {
    captureAssetReviewEdits();
    const previousPageId = state?.project?.activePageId;
    const previousSourceRevision = state?.sourceRevision;
    state = await api('/api/state');
    if (loadedPageId && loadedPageId !== state.project.activePageId) {
      sourceFiles = {}; sourceRevision = ''; assetReviewEdits.clear(); $('#assetGallery').innerHTML = ''; marker.dirty = false; marker.ready = false; marker.regions = []; marker.selectedRegion = -1; marker.selectedTarget = -1;
    }
    loadedPageId = state.project.activePageId;
    renderAll();
    if (previousPageId === state.project.activePageId && previousSourceRevision && previousSourceRevision !== state.sourceRevision && activeView === 'code') await loadSource();
    if (!silent) notify('状态已刷新');
  } catch (error) {
    notify(error.message, true);
  }
}

async function waitForJob(jobId) {
  notify('后台任务已启动');
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 1200));
    const job = await api(`/api/jobs/${jobId}`);
    await refreshState(true);
    if (job.status === 'completed') {
      notify('任务执行完成');
      return job;
    }
    if (job.status === 'failed') throw new Error(job.message);
  }
}

async function loadDesign() {
  try {
    const data = await api('/api/design');
    $('#promptNotes').value = data.notes || '';
    $('#designPrompt').value = data.prompt || '';
  } catch (error) { notify(error.message, true); }
}

async function loadProjectSettings() {
  try {
    const data = await api('/api/design');
    $('#projectDesignSpec').value = JSON.stringify(data.spec || {}, null, 2);
    if (window.loadDesignStudio) await window.loadDesignStudio();
  } catch (error) { notify(error.message, true); }
}

async function loadSource() {
  try {
    const source = await api('/api/source');
    sourceFiles = source.files || {};
    sourceRevision = source.revision || '';
    $('#sourceEditor').value = sourceFiles[currentSourceFile] || '';
    reloadPreview();
  } catch (error) { notify(error.message, true); }
}

function retainEditor() { sourceFiles[currentSourceFile] = $('#sourceEditor').value; }
function reloadPreview() {
  const preview = $('#pagePreview');
  if (!state?.sourceReady) {
    preview.removeAttribute('src');
    preview.srcdoc = '<!doctype html><html lang="zh-CN"><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#fff;color:#66716a;font:14px system-ui">当前页面尚未保存源码</body></html>';
    return;
  }
  preview.removeAttribute('srcdoc');
  preview.src = cacheBust(activeFileUrl('src/index.html'));
}

const marker = {
  regions: [], mode: 'crop', selectedRegion: -1, selectedTarget: -1, start: null, draft: null, ready: false, fullTargetView: false,
  loadState(nextState) {
    const image = $('#markerImage');
    if (!nextState.reference) { image.removeAttribute('src'); $('#markerOverlay').replaceChildren(); $('#targetOverlay').replaceChildren(); this.ready = false; this.regions = []; $('#markerStatus').textContent = '当前页面尚无参考图'; return; }
    const incoming = nextState.regions?.regions || [];
    const referenceKey = `${nextState.reference.url}:${nextState.reference.modified || ''}`;
    if (!this.ready || image.dataset.reference !== referenceKey) {
      this.ready = false;
      $('#markerOverlay').replaceChildren();
      $('#targetOverlay').replaceChildren();
      $('#targetCanvas').width = 0;
      $('#targetCanvas').height = 0;
      image.onload = () => { $('#markerOverlay').style.width = `${image.naturalWidth}px`; $('#markerOverlay').style.height = `${image.naturalHeight}px`; this.ready = true; this.render(); };
      image.dataset.reference = referenceKey;
      image.src = cacheBust(nextState.reference.url);
    }
    if (!this.dirty) {
      this.regions = structuredClone(incoming).map(region => ({ ...region, targets: (region.targets || []).map(normalizeTarget) }));
      this.selectedRegion = this.regions.length ? Math.max(0, Math.min(this.selectedRegion, this.regions.length - 1)) : -1;
    }
    this.render();
  },
  payload() {
    const image = $('#markerImage');
    return { canvas: { width: image.naturalWidth, height: image.naturalHeight }, regions: this.regions.map((region, index) => ({ ...region, id: `region-${String(index + 1).padStart(3, '0')}`, targets: region.targets.map((target, targetIndex) => ({ ...target, id: `target-${String(targetIndex + 1).padStart(3, '0')}` })) })) };
  },
  setMode(mode) {
    if (mode === 'target' && !this.regions.length) return notify('请先画绿色切割框', true);
    this.mode = mode;
    if (mode !== 'target') this.fullTargetView = false;
    if (mode === 'target' && this.selectedRegion < 0) this.selectedRegion = 0;
    $('#cropMode').classList.toggle('active', mode === 'crop');
    $('#targetMode').classList.toggle('active', mode === 'target');
    this.render();
  },
  render() {
    if (!this.ready) return;
    $('#markerStage').style.display = this.mode === 'crop' ? 'block' : 'none';
    $('#targetStage').style.display = this.mode === 'target' ? 'block' : 'none';
    if (this.mode === 'crop') {
      const overlay = $('#markerOverlay');
      overlay.replaceChildren();
      this.regions.forEach((region, index) => {
        const box = document.createElement('div');
        box.className = `marker-region${index === this.selectedRegion ? ' selected' : ''}`;
        Object.assign(box.style, { left: `${region.x}px`, top: `${region.y}px`, width: `${region.width}px`, height: `${region.height}px` });
        box.innerHTML = `<span>切割 ${String(index + 1).padStart(2, '0')}</span>`;
        box.onpointerdown = event => event.stopPropagation();
        box.onclick = event => { event.stopPropagation(); this.selectedRegion = index; this.selectedTarget = -1; this.render(); };
        overlay.append(box);
      });
    } else this.drawTargetCanvas();
    const selection = $('#regionSelection');
    selection.innerHTML = this.regions.length ? this.regions.map((region, index) => `<option value="${index}" ${index === this.selectedRegion ? 'selected' : ''}>切割 ${String(index + 1).padStart(2, '0')}，${region.targets.length} 个素材</option>`).join('') : '<option>先画绿色切割框</option>';
    selection.disabled = !this.regions.length;
    const region = this.regions[this.selectedRegion];
    $('#regionPurpose').value = region?.purpose || '';
    $('#markApproved').checked = region?.approved !== false;
    const targets = region?.targets || [];
    if (targets.length && this.selectedTarget < 0) this.selectedTarget = targets.length - 1;
    const targetSelect = $('#targetSelection');
    targetSelect.innerHTML = targets.length ? targets.map((target, index) => `<option value="${index}" ${index === this.selectedTarget ? 'selected' : ''}>素材 ${String(index + 1).padStart(2, '0')}</option>`).join('') : '<option value="-1">尚未标记素材</option>';
    targetSelect.disabled = !targets.length;
    const target = targets[this.selectedTarget];
    $('#targetPurpose').value = target?.purpose || '';
    $('#targetElementType').value = target?.elementType || 'image-asset';
    $('#targetProcessingMode').value = target?.processingMode || 'ai-transparent';
    $('#targetName').value = target?.name || '';
    $('#targetParent').value = target?.parent || '';
    $('#targetZIndex').value = target?.zIndex ?? '';
    $('#backgroundTolerance').value = target?.backgroundTolerance ?? 34;
    $('#edgeFeather').value = target?.edgeFeather ?? 48;
    $('#targetElementType').disabled = !target;
    $('#targetProcessingMode').disabled = !target || target.elementType === 'code-element';
    $('#targetName').disabled = !target;
    $('#targetParent').disabled = !target;
    $('#targetZIndex').disabled = !target;
    $('#backgroundTolerance').disabled = !target;
    $('#edgeFeather').disabled = !target;
    $('#scriptExtractionOptions').hidden = !target || target.processingMode !== 'local-transparent';
    $('#targetConfirmed').checked = target?.reviewStatus === 'confirmed';
    $('#targetConfirmed').disabled = !target;
    const badge = $('#targetReviewBadge');
    badge.textContent = !target ? '未选择' : target.reviewStatus === 'confirmed' ? '已人工确认' : '待人工确认';
    badge.classList.toggle('confirmed', target?.reviewStatus === 'confirmed');
    const totals = { confirmed: 0, pending: 0, image: 0, code: 0, composite: 0 };
    this.regions.forEach(item => item.targets.forEach(candidate => {
      totals[candidate.reviewStatus === 'confirmed' ? 'confirmed' : 'pending'] += 1;
      totals[candidate.elementType === 'code-element' ? 'code' : candidate.elementType === 'complete-composite' ? 'composite' : 'image'] += 1;
    }));
    $('#markerStatus').textContent = `${this.regions.length} 个区域 · 图片 ${totals.image} · 代码 ${totals.code} · 复合 ${totals.composite} · 待确认 ${totals.pending}`;
  },
  drawTargetCanvas() {
    const region = this.regions[this.selectedRegion];
    if (!region) return;
    const canvas = $('#targetCanvas');
    const stage = $('#targetStage');
    const full = this.fullTargetView;
    const width = full ? $('#markerImage').naturalWidth : region.width;
    const height = full ? $('#markerImage').naturalHeight : region.height;
    canvas.width = width; canvas.height = height;
    stage.style.width = `${width}px`; stage.style.height = `${height}px`;
    canvas.getContext('2d').drawImage($('#markerImage'), full ? 0 : region.x, full ? 0 : region.y, width, height, 0, 0, width, height);
    const overlay = $('#targetOverlay');
    overlay.replaceChildren();
    region.targets.forEach((target, index) => {
      const box = document.createElement('div'); box.className = `marker-target${index === this.selectedTarget ? ' selected' : ''}`;
      Object.assign(box.style, { left: `${(full ? region.x : 0) + target.x}px`, top: `${(full ? region.y : 0) + target.y}px`, width: `${target.width}px`, height: `${target.height}px` });
      box.innerHTML = `<span>${elementTypeLabels[target.elementType] || '图片素材'} · ${String(index + 1).padStart(2, '0')}</span>`;
      box.onpointerdown = event => event.stopPropagation();
      box.onclick = event => { event.stopPropagation(); this.selectedTarget = index; this.render(); };
      overlay.append(box);
    });
  },
  pointerStart(event, target) {
    if (event.button !== 0 || !this.ready || event.target !== target || (this.mode === 'target' && this.selectedRegion < 0)) return;
    const rect = target.getBoundingClientRect();
    this.start = { x: Math.round(event.clientX - rect.left), y: Math.round(event.clientY - rect.top) };
    this.draft = document.createElement('div');
    this.draft.className = `marker-draft ${this.mode}`;
    this.draft.innerHTML = '<span>0 × 0</span>';
    target.append(this.draft);
    target.setPointerCapture(event.pointerId);
    event.preventDefault();
  },
  pointerMove(event, target) {
    if (!this.start || !this.draft) return;
    const box = this.draftBox(event, target);
    Object.assign(this.draft.style, { left: `${box.x}px`, top: `${box.y}px`, width: `${box.width}px`, height: `${box.height}px` });
    this.draft.querySelector('span').textContent = `${box.width} × ${box.height}`;
  },
  draftBox(event, target) {
    const rect = target.getBoundingClientRect();
    const maxW = this.mode === 'crop' ? $('#markerImage').naturalWidth : this.fullTargetView ? $('#markerImage').naturalWidth : this.regions[this.selectedRegion].width;
    const maxH = this.mode === 'crop' ? $('#markerImage').naturalHeight : this.fullTargetView ? $('#markerImage').naturalHeight : this.regions[this.selectedRegion].height;
    const endX = Math.max(0, Math.min(maxW, Math.round(event.clientX - rect.left)));
    const endY = Math.max(0, Math.min(maxH, Math.round(event.clientY - rect.top)));
    const x = Math.min(this.start.x, endX);
    const y = Math.min(this.start.y, endY);
    return { x, y, width: Math.abs(endX - this.start.x), height: Math.abs(endY - this.start.y) };
  },
  pointerEnd(event, target) {
    if (!this.start) return;
    const { x, y, width, height } = this.draftBox(event, target);
    this.draft?.remove();
    this.draft = null;
    let openConfig = false;
    if (width >= 8 && height >= 8) {
      if (this.mode === 'crop') {
        this.regions.push({ x, y, width, height, purpose: '', approved: true, targets: [] });
        this.selectedRegion = this.regions.length - 1; this.selectedTarget = -1;
      } else {
        const region = this.regions[this.selectedRegion];
        const localX = this.fullTargetView ? x - region.x : x;
        const localY = this.fullTargetView ? y - region.y : y;
        if (localX < 0 || localY < 0 || localX + width > region.width || localY + height > region.height) {
          this.start = null;
          this.render();
          return;
        }
        this.regions[this.selectedRegion].targets.push(normalizeTarget({ x: localX, y: localY, width, height, purpose: '', approved: true, elementType: 'image-asset', processingMode: 'local-transparent', reviewStatus: 'needs-review', suggestion: { source: 'manual' } }));
        this.selectedTarget = this.regions[this.selectedRegion].targets.length - 1;
        openConfig = true;
      }
      this.dirty = true;
    }
    this.start = null; this.render();
    if (openConfig) this.openTargetConfig();
  },
  pointerCancel() {
    this.draft?.remove();
    this.draft = null;
    this.start = null;
  },
  openTargetConfig() {
    const target = this.regions[this.selectedRegion]?.targets[this.selectedTarget];
    if (!target) return;
    $('#targetConfigSize').textContent = `${target.width} × ${target.height}`;
    $('#dialogElementType').value = target.elementType || 'image-asset';
    $('#dialogProcessingMode').value = target.processingMode || 'local-transparent';
    $('#dialogProcessingMode').disabled = target.elementType === 'code-element';
    $('#dialogTargetName').value = target.name || '';
    $('#dialogTargetPurpose').value = target.purpose || '';
    $('#dialogBackgroundTolerance').value = target.backgroundTolerance ?? 34;
    $('#dialogEdgeFeather').value = target.edgeFeather ?? 48;
    $('#dialogScriptOptions').hidden = target.processingMode !== 'local-transparent';
    $('#targetConfigDialog').showModal();
  },
  applyTargetConfig(useDefaults = false) {
    const target = this.regions[this.selectedRegion]?.targets[this.selectedTarget];
    if (!target) return;
    target.elementType = useDefaults ? 'image-asset' : $('#dialogElementType').value;
    target.processingMode = useDefaults ? 'local-transparent' : modeForType(target.elementType, $('#dialogProcessingMode').value);
    target.name = useDefaults ? '' : $('#dialogTargetName').value.trim();
    target.purpose = useDefaults ? '' : $('#dialogTargetPurpose').value.trim();
    target.backgroundTolerance = useDefaults ? 34 : Number($('#dialogBackgroundTolerance').value);
    target.edgeFeather = useDefaults ? 48 : Number($('#dialogEdgeFeather').value);
    target.reviewStatus = 'confirmed';
    this.dirty = true;
    $('#targetConfigDialog').close();
    this.render();
  },
  undo() {
    if (this.mode === 'crop') { this.regions.pop(); this.selectedRegion = this.regions.length - 1; }
    else if (this.regions[this.selectedRegion]) { this.regions[this.selectedRegion].targets.pop(); this.selectedTarget = this.regions[this.selectedRegion].targets.length - 1; }
    this.dirty = true; this.render();
  },
  applySuggestions(document) {
    const incoming = document?.regions || [];
    this.regions = structuredClone(incoming).map(region => ({ ...region, targets: (region.targets || []).map(target => normalizeTarget({ ...target, reviewStatus: 'needs-review', suggestion: { source: 'ai', confidence: target.confidence } })) }));
    this.selectedRegion = this.regions.length ? 0 : -1;
    this.selectedTarget = this.regions[0]?.targets?.length ? 0 : -1;
    this.dirty = true;
    this.fullTargetView = true;
    this.setMode(this.regions.length ? 'target' : 'crop');
  },
};

function bindEvents() {
  $('#refreshButton').onclick = () => refreshState();
  $('#projectSettingsButton').onclick = () => showView('project-settings');
  $('#pageSelector').onchange = async event => {
    try { await api('/api/pages/select', { method: 'POST', body: { id: event.target.value } }); await refreshState(true); if (activeView === 'design') await loadDesign(); if (activeView === 'code') await loadSource(); notify('已切换页面'); } catch (error) { notify(error.message, true); }
  };
  $('#newPageButton').onclick = () => { $('#newPageForm').reset(); $('#newPageDialog').showModal(); };
  $$('[data-close-dialog]').forEach(button => button.onclick = () => $('#newPageDialog').close());
  $('#createPageSubmit').onclick = async event => {
    event.preventDefault();
    const form = $('#newPageForm');
    if (!form.reportValidity()) return;
    const values = Object.fromEntries(new FormData(form));
    try { await api('/api/pages', { method: 'POST', body: values }); $('#newPageDialog').close(); await refreshState(true); showView('design'); await loadDesign(); notify('新页面已创建，数据目录完全独立'); } catch (error) { notify(error.message, true); }
  };
  $('#renamePageButton').onclick = async () => {
    const name = prompt('新的页面名称', state.project.activePage.name);
    if (!name || name === state.project.activePage.name) return;
    try { await api('/api/pages/rename', { method: 'POST', body: { name } }); await refreshState(true); notify('页面已重命名'); } catch (error) { notify(error.message, true); }
  };
  $('#clearCompleted').onclick = () => { hideCompletedJobs = !hideCompletedJobs; renderJobs(); };
  $('#configForm').onsubmit = async event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    values.padding = Number(values.padding); values.threshold = Number(values.threshold); values.codexMaxIterations = Number(values.codexMaxIterations);
    try {
      await api('/api/config', { method: 'POST', body: values });
      await refreshState(true);
      notify('项目设置已保存，全部页面立即生效');
    } catch (error) { notify(error.message, true); }
  };
  $('#checkApi').onclick = async () => { try { const { jobId } = await api('/api/check-api', { method: 'POST', body: {} }); await waitForJob(jobId); } catch (error) { notify(error.message, true); } };
  $('#fetchImageModels').onclick = async event => {
    const button = event.currentTarget;
    const form = $('#configForm');
    const currentModel = form.elements.imageModel.value;
    button.disabled = true;
    button.textContent = '正在获取...';
    try {
      const result = await api('/api/models/image', { method: 'POST', body: { baseUrl: form.elements.imageBaseUrl.value, apiKey: form.elements.imageApiKey.value } });
      setImageModelOptions(result.models, currentModel);
      $('#imageModelHint').textContent = `已获取 ${result.count} 个模型，请选择后保存项目设置`;
      notify(`已获取 ${result.count} 个模型`);
    } catch (error) { notify(error.message, true); }
    finally { button.disabled = false; button.textContent = '获取模型列表'; }
  };
  $('#checkTextApi').onclick = async () => { try { const { jobId } = await api('/api/check-text-api', { method: 'POST', body: {} }); await waitForJob(jobId); } catch (error) { notify(error.message, true); } };
  $('#saveDesign').onclick = async () => {
    try { await api('/api/design', { method: 'POST', body: { notes: $('#promptNotes').value, prompt: $('#designPrompt').value } }); notify('页面需求已保存'); } catch (error) { notify(error.message, true); }
  };
  $('#generatePrompt').onclick = async () => {
    if ($('#generatePrompt').disabled) return;
    if (!confirm('确认调用文本模型，根据当前设计规范生成图片提示词？')) return;
    const pageId = state.project.activePageId;
    promptSubmissions.add(pageId); renderPromptLoading();
    try {
      await api('/api/design', { method: 'POST', body: { notes: $('#promptNotes').value } });
      const { jobId } = await api('/api/prompt/generate', { method: 'POST', body: { notes: $('#promptNotes').value, confirmed: true } });
      await waitForJob(jobId);
      if (state.project.activePageId === pageId) await loadDesign();
      notify('图片提示词已生成并保存');
    } catch (error) { notify(error.message, true); }
    finally { promptSubmissions.delete(pageId); renderPromptLoading(); }
  };
  $('#generateReference').onclick = async () => {
    if (!confirm('确认调用图像 API 生成一张参考图？此操作会产生费用。')) return;
    try {
      await api('/api/design', { method: 'POST', body: { notes: $('#promptNotes').value, prompt: $('#designPrompt').value } });
      const { jobId } = await api('/api/generate', { method: 'POST', body: { prompt: $('#designPrompt').value, confirmed: true } });
      await waitForJob(jobId); showView('reference');
    } catch (error) { notify(error.message, true); }
  };
  $('#uploadReference').onclick = () => $('#referenceFile').click();
  $('#referenceFile').onchange = () => {
    const file = $('#referenceFile').files[0]; if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => { try { await api('/api/reference/upload', { method: 'POST', body: { data: reader.result } }); await refreshState(true); notify('参考图已上传'); } catch (error) { notify(error.message, true); } };
    reader.readAsDataURL(file);
  };
  $('#referenceEditFile').onchange = () => {
    const file = $('#referenceEditFile').files[0];
    $('#referenceEditFileName').textContent = file ? file.name : '未选择文件';
  };
  $('#editReference').onclick = async () => {
    const prompt = $('#referenceEditPrompt').value.trim();
    if (!prompt) return notify('请先输入修改提示词', true);
    if (!$('#referenceEditFile').files[0]) return notify('请先上传参考图', true);
    if (!confirm('确认调用图片编辑 API 修改参考图？此操作会产生费用。')) return;
    const file = $('#referenceEditFile').files[0];
    try {
      const image = file ? await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); }) : '';
      const { jobId } = await api('/api/generate', {method: 'POST', body: {prompt, image, includeOriginal: $('#referenceIncludeOriginal').checked, edit: true, confirmed: true}});
      await waitForJob(jobId);
      $('#referenceEditPrompt').value = '';
      $('#referenceEditFile').value = '';
      $('#referenceEditFileName').textContent = '未选择文件';
      await refreshState(true);
      notify('参考图已修改，等待审核');
    } catch (error) { notify(error.message, true); }
  };
  $('#approveReference').onclick = async () => { try { await api('/api/reference/approve', { method: 'POST', body: { approved: true } }); await refreshState(true); notify('参考图已批准'); } catch (error) { notify(error.message, true); } };
  $('#rejectReference').onclick = async () => { try { await api('/api/reference/approve', { method: 'POST', body: { approved: false } }); await refreshState(true); notify('已标记为待修改'); } catch (error) { notify(error.message, true); } };

  $('#cropMode').onclick = () => marker.setMode('crop');
  $('#targetMode').onclick = () => marker.setMode('target');
  $('#regionSelection').onchange = event => { marker.selectedRegion = Number(event.target.value); marker.selectedTarget = -1; marker.render(); };
  $('#targetSelection').onchange = event => { marker.selectedTarget = Number(event.target.value); marker.render(); };
  $('#regionPurpose').oninput = event => { if (marker.regions[marker.selectedRegion]) { marker.regions[marker.selectedRegion].purpose = event.target.value; marker.dirty = true; } };
  $('#targetPurpose').oninput = event => { const target = marker.regions[marker.selectedRegion]?.targets[marker.selectedTarget]; if (target) { target.purpose = event.target.value; marker.dirty = true; } };
  $('#targetElementType').onchange = event => { const target = marker.regions[marker.selectedRegion]?.targets[marker.selectedTarget]; if (target) { target.elementType = event.target.value; target.processingMode = modeForType(target.elementType, target.processingMode); target.reviewStatus = 'confirmed'; marker.dirty = true; marker.render(); } };
  $('#targetProcessingMode').onchange = event => { const target = marker.regions[marker.selectedRegion]?.targets[marker.selectedTarget]; if (target && target.elementType !== 'code-element') { target.processingMode = event.target.value; target.reviewStatus = 'confirmed'; marker.dirty = true; marker.render(); } };
  $('#targetName').oninput = event => { const target = marker.regions[marker.selectedRegion]?.targets[marker.selectedTarget]; if (target) { target.name = event.target.value; marker.dirty = true; } };
  $('#targetParent').oninput = event => { const target = marker.regions[marker.selectedRegion]?.targets[marker.selectedTarget]; if (target) { target.parent = event.target.value; marker.dirty = true; } };
  $('#targetZIndex').oninput = event => { const target = marker.regions[marker.selectedRegion]?.targets[marker.selectedTarget]; if (target) { target.zIndex = event.target.value === '' ? null : Number(event.target.value); marker.dirty = true; } };
  $('#targetConfirmed').onchange = event => { const target = marker.regions[marker.selectedRegion]?.targets[marker.selectedTarget]; if (target) { target.reviewStatus = event.target.checked ? 'confirmed' : 'needs-review'; marker.dirty = true; marker.render(); } };
  $('#backgroundTolerance').oninput = event => { const target = marker.regions[marker.selectedRegion]?.targets[marker.selectedTarget]; if (target) { target.backgroundTolerance = Number(event.target.value); marker.dirty = true; } };
  $('#edgeFeather').oninput = event => { const target = marker.regions[marker.selectedRegion]?.targets[marker.selectedTarget]; if (target) { target.edgeFeather = Number(event.target.value); marker.dirty = true; } };
  $('#markApproved').onchange = event => { if (marker.regions[marker.selectedRegion]) { marker.regions[marker.selectedRegion].approved = event.target.checked; marker.dirty = true; } };
  $('#markerOverlay').onpointerdown = event => marker.pointerStart(event, $('#markerOverlay'));
  $('#markerOverlay').onpointermove = event => marker.pointerMove(event, $('#markerOverlay'));
  $('#markerOverlay').onpointerup = event => marker.pointerEnd(event, $('#markerOverlay'));
  $('#markerOverlay').onpointercancel = () => marker.pointerCancel();
  $('#targetOverlay').onpointerdown = event => marker.pointerStart(event, $('#targetOverlay'));
  $('#targetOverlay').onpointermove = event => marker.pointerMove(event, $('#targetOverlay'));
  $('#targetOverlay').onpointerup = event => marker.pointerEnd(event, $('#targetOverlay'));
  $('#targetOverlay').onpointercancel = () => marker.pointerCancel();
  $('#dialogElementType').onchange = event => { $('#dialogProcessingMode').value = modeForType(event.target.value, $('#dialogProcessingMode').value); $('#dialogProcessingMode').disabled = event.target.value === 'code-element'; $('#dialogScriptOptions').hidden = $('#dialogProcessingMode').value !== 'local-transparent'; };
  $('#dialogProcessingMode').onchange = event => { $('#dialogScriptOptions').hidden = event.target.value !== 'local-transparent'; };
  $('#targetConfigForm').onsubmit = event => { event.preventDefault(); marker.applyTargetConfig(false); };
  $('#useDefaultTargetConfig').onclick = () => marker.applyTargetConfig(true);
  $('#undoMark').onclick = () => marker.undo();
  $('#clearMarks').onclick = () => { if (confirm('确认清空全部绿色框和红色框？')) { marker.regions = []; marker.selectedRegion = -1; marker.selectedTarget = -1; marker.dirty = true; marker.setMode('crop'); } };
  $('#suggestElements').onclick = async () => {
    if (marker.dirty && !confirm('AI 建议会替换当前尚未保存的标注，是否继续？')) return;
    if (!confirm('确认调用可看图的文本模型生成元素分类建议？建议仍需逐项人工确认。')) return;
    try {
      const { jobId } = await api('/api/regions/suggest', { method: 'POST', body: { confirmed: true, regions: marker.payload() } });
      const job = await waitForJob(jobId);
      marker.applySuggestions(job.result?.suggestions);
      notify('AI 建议已载入，请逐项人工确认后保存');
    } catch (error) { notify(error.message, true); }
  };
  $('#saveMarks').onclick = async () => { try { await api('/api/regions', { method: 'POST', body: marker.payload() }); marker.dirty = false; await refreshState(true); notify('标注已直接保存到项目'); } catch (error) { notify(error.message, true); } };
  $('#scriptExtract').onclick = async () => {
    try {
      if (!await ensureRasterTargetsConfirmed()) return;
      const result = await api('/api/script-extract', { method: 'POST', body: {} });
      await refreshState(true); showView('assets');
      notify(`本地处理已生成 ${result.assetCount} 张图片，并保留原始裁剪`);
    } catch (error) { notify(error.message, true); }
  };

  $('#cropButton').onclick = async () => {
    try {
      if (!await ensureRasterTargetsConfirmed()) return;
      const impact = state.recropImpact || {};
      if (impact.hasExistingCrops) {
        const affected = ['已有原始裁剪'];
        if (impact.assetReview) affected.push('元素审核及全部处理素材');
        if (impact.pageImplementation) affected.push('页面实现源码');
        if (impact.comparisonCalibration) affected.push('对比校准记录');
        if (impact.codexTaskCount) affected.push(`${impact.codexTaskCount} 条 Codex 任务记录`);
        const message = `当前页面已经生成过裁剪。重新裁剪将删除：\n\n- ${affected.join('\n- ')}\n\n参考图、区域标注和页面需求会保留。是否继续？`;
        if (!confirm(message)) return;
      }
      await api('/api/crop', { method: 'POST', body: { padding: state.settings.padding, confirmedRecrop: Boolean(impact.hasExistingCrops) } });
      if (impact.hasExistingCrops) {
        sourceFiles = {};
        sourceRevision = '';
        assetReviewEdits.clear();
        $('#assetGallery').innerHTML = '';
      }
      await refreshState(true);
      notify(impact.hasExistingCrops ? '已重新裁剪，并清除元素审核、页面实现和对比校准数据' : '裁剪图已生成');
    } catch (error) { notify(error.message, true); }
  };
  $('#extractButton').onclick = async () => {
    if (!await ensureRasterTargetsConfirmed()) return;
    const aiCrops = state.crops.filter(crop => crop.aiTargetCount > 0).length;
    if (!aiCrops) return notify('没有设置为 AI 提取的素材，请先生成对应裁剪图', true);
    if (!confirm(`确认并发调用图像 API 提取 ${aiCrops} 个区域？此操作会产生费用。`)) return;
    aiExtractionPending = true;
    renderCrops();
    try {
      const { jobId } = await api('/api/extract', { method: 'POST', body: { confirmed: true } });
      await waitForJob(jobId);
      notify('素材提取完成，请执行透明对象拆分');
    } catch (error) { notify(error.message, true); }
    finally {
      aiExtractionPending = false;
      await refreshState(true);
    }
  };
  $('#repairBackgrounds').onclick = async () => {
    try {
      if (!await ensureRasterTargetsConfirmed()) return;
      const repair = markerModeCounts('background-repair');
      if (!repair.confirmed) return notify('没有已确认的背景补全目标', true);
      if (!confirm(`确认调用图像 API 补全 ${repair.confirmed} 个背景？此操作会产生费用。`)) return;
      const { jobId } = await api('/api/background-repair', { method: 'POST', body: { confirmed: true } });
      await waitForJob(jobId); await refreshState(true); showView('assets'); notify(`已生成 ${repair.confirmed} 个背景补全结果，原始裁剪仍保留`);
    } catch (error) { notify(error.message, true); }
  };
  $('#splitButton').onclick = async () => { try { await api('/api/split', { method: 'POST', body: { minArea: 256, joinGap: 0 } }); await refreshState(true); showView('assets'); notify('透明对象已拆分'); } catch (error) { notify(error.message, true); } };
  $$('[data-asset-filter]').forEach(button => button.onclick = () => { captureAssetReviewEdits(); assetFilter = button.dataset.assetFilter; $$('[data-asset-filter]').forEach(item => item.classList.toggle('active', item === button)); renderAssets(); });
  $('#selectAllAssets').onclick = () => $$('.asset-review-item:not(.filter-hidden) .asset-approved', $('#assetGallery')).forEach(input => { input.checked = true; });
  $('#approveAssets').onclick = async () => {
    captureAssetReviewEdits();
    const assets = []; const codeElements = [];
    for (const [key, decision] of assetReviewEdits) {
      const [itemKind, id] = key.split(':');
      const item = { id, ...decision };
      if (itemKind === 'asset') assets.push(item);
      else codeElements.push({ ...item, semanticDescription: item.description, placement: { status: 'confirmed-from-browser-review', bbox: item.bbox }, processingMode: 'none', elementType: 'code-element' });
    }
    try { const result = await api('/api/assets/approve', { method: 'POST', body: { assets, codeElements } }); assetReviewEdits.clear(); $('#assetGallery').innerHTML = ''; await refreshState(true); notify(`已批准 ${result.approvedCount} 张图片和 ${result.codeElementCount} 个代码元素`); } catch (error) { notify(error.message, true); }
  };

  $$('#codeTabs button').forEach(button => button.onclick = () => {
    retainEditor(); currentSourceFile = button.dataset.file;
    $$('#codeTabs button').forEach(item => item.classList.toggle('active', item === button));
    $('#sourceEditor').value = sourceFiles[currentSourceFile] || '';
  });
  $('#saveSource').onclick = async () => { retainEditor(); try { const result = await api('/api/source', { method: 'POST', body: { files: sourceFiles, expectedRevision: sourceRevision } }); sourceRevision = result.revision; await refreshState(true); reloadPreview(); notify('源码已保存'); } catch (error) { notify(error.message, true); } };
  $('#copyPersistentSession').onclick = copyPersistentCodexInstruction;
  $('#createImplementationTask').onclick = () => createCodexTask('implementation');
  $('#createCalibrationTask').onclick = () => createCodexTask('calibration');
  $('#reloadPreview').onclick = reloadPreview;
  $('#compareButton').onclick = async () => { try { const { jobId } = await api('/api/render-compare', { method: 'POST', body: { note: $('#iterationNote').value, threshold: state.settings.threshold } }); await waitForJob(jobId); notify('对比图已生成'); } catch (error) { notify(error.message, true); } };
  $('#completeButton').onclick = async () => { try { await api('/api/complete', { method: 'POST', body: { note: $('#iterationNote').value || '浏览器审核通过' } }); await refreshState(true); notify('项目已标记完成'); } catch (error) { notify(error.message, true); } };
}

window.addEventListener('hashchange', () => showView(location.hash.slice(1) || 'overview'));
bindEvents();
refreshState(true).then(() => showView(activeView));
setInterval(() => {
  const backgroundRunning = state?.jobs?.some(job => ['queued', 'running'].includes(job.status));
  const codexRunning = state?.codexTasks?.some(task => ['queued', 'running'].includes(task.status));
  if (backgroundRunning || codexRunning) refreshState(true);
}, 2500);
