/* 编排流台：组件化 SPA（Svelte 式状态驱动渲染，无构建步骤便于容器验收） */
const state = {
  user: '',
  lab: '',
  items: [],
  selected: null,
  confirmations: [],
  depForm: { upstreamIds: new Set(), policy: '作废' },
  uriDraft: '',
  forceDraft: '',
  error: '',
  ok: '',
  busy: false,
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || '请求失败');
  return j;
}

function esc(v) {
  return String(v ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

async function boot() {
  try {
    const me = await api('/api/me');
    state.user = me.user;
    state.lab = me.lab;
    state.items = (await api('/api/flows')).items;
    render();
  } catch (e) {
    location.href = '/login';
  }
}

async function refreshList() {
  state.items = (await api('/api/flows')).items;
}

async function select(id) {
  state.error = '';
  state.ok = '';
  const [detail, conf] = await Promise.all([
    api('/api/flows/' + id),
    api('/api/flows/' + id + '/confirmations'),
  ]);
  state.selected = detail;
  state.confirmations = conf.items;
  state.uriDraft = detail.source_uri;
  state.forceDraft = '';
  const dep = detail.dependency;
  state.depForm = {
    upstreamIds: new Set(dep ? dep.upstreams.map((u) => u.id) : []),
    policy: dep ? dep.policy : '作废',
  };
  render();
}

async function doExtract() {
  if (state.busy || !state.selected) return;
  state.busy = true;
  state.error = '';
  state.ok = '';
  try {
    // 先开抽（后台闸口在此强制），短暂保留抽取中态再收尾
    state.selected = await api('/api/flows/' + state.selected.id + '/extract/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    render();
    await new Promise((r) => setTimeout(r, 650));
    state.selected = await api('/api/flows/' + state.selected.id + '/extract/finish', {
      method: 'POST',
    });
    state.ok = '抽取完成';
    await refreshList();
    await select(state.selected.id);
  } catch (e) {
    state.error = e.message;
    await refreshList();
    if (state.selected) await select(state.selected.id);
    state.error = e.message;
    render();
  } finally {
    state.busy = false;
  }
}

async function doForceExtract() {
  if (state.busy || !state.selected) return;
  state.busy = true;
  state.error = '';
  state.ok = '';
  try {
    state.selected = await api('/api/flows/' + state.selected.id + '/force-extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: state.forceDraft }),
    });
    state.ok = '人工确认通过，已强行开抽并完成';
    await refreshList();
    await select(state.selected.id);
  } catch (e) {
    state.error = e.message;
    render();
  } finally {
    state.busy = false;
  }
}

async function doLand() {
  if (!state.selected) return;
  state.error = '';
  try {
    state.selected = await api('/api/flows/' + state.selected.id + '/land', { method: 'POST' });
    await refreshList();
    render();
  } catch (e) {
    state.error = e.message;
    render();
  }
}

async function saveDeps() {
  if (!state.selected) return;
  state.error = '';
  state.ok = '';
  try {
    state.selected = await api('/api/flows/' + state.selected.id + '/dependencies', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upstream_ids: [...state.depForm.upstreamIds],
        policy: state.depForm.policy,
      }),
    });
    state.ok = '依赖图与策略已钉死保存';
    await refreshList();
    await select(state.selected.id);
  } catch (e) {
    state.error = e.message;
    render();
  }
}

async function saveUri() {
  if (!state.selected) return;
  state.error = '';
  state.ok = '';
  try {
    state.selected = await api('/api/flows/' + state.selected.id + '/source-uri', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_uri: state.uriDraft }),
    });
    state.ok = '路径已更新（若与钉死路径漂移，开抽将被拦截）';
    render();
  } catch (e) {
    state.error = e.message;
    render();
  }
}

async function logout() {
  await api('/api/logout', { method: 'POST' });
  location.href = '/login';
}

function statusChip(status) {
  const tone =
    status === '抽取中' ? 'tone-run'
    : status === '等待舱' ? 'tone-hold'
    : status === '已中断' ? 'tone-bad'
    : status === '已落库' ? 'tone-ok'
    : '';
  return `<span class="chip ${tone}">${esc(status)}</span>`;
}

function renderDepList() {
  const sel = state.selected;
  const dep = sel.dependency;
  if (!dep) {
    return `
      <div class="card">
        <h3>前置依赖列表</h3>
        <p class="hint">尚未挂前置依赖图，空依赖图不准保存，请在下方至少勾选一个前置任务编号。</p>
      </div>`;
  }
  const rows = dep.upstreams.map((u) => `
    <tr>
      <td class="mono">#${u.id}</td>
      <td>${esc(u.title)}</td>
      <td class="mono">${esc(u.code)}</td>
      <td>${statusChip(u.status)}</td>
      <td>${u.ready ? '<span class="gate gate-ok">齐件</span>' : '<span class="gate gate-bad">未齐件</span>'}</td>
    </tr>`).join('');
  return `
    <div class="card">
      <h3>前置依赖列表 <span class="sub">（与运行详情并列，前置齐件闸在此核对）</span></h3>
      <p class="kv">
        策略名：<strong>${esc(dep.policy)}</strong>
        · 齐件闸：${dep.ready ? '<span class="gate gate-ok">齐件</span>' : '<span class="gate gate-bad">未齐件</span>'}
        · 等待舱：${sel.in_hold ? '<span class="gate gate-hold">在舱</span>' : '否'}
        · 中断数：<strong>${sel.interrupt_count}</strong>
      </p>
      <p class="kv mono small">钉死路径：${esc(dep.pinned_source_uri)}</p>
      <p class="kv mono small">钉死时间：${esc(dep.pinned_at)}（当日钉死不许再换）</p>
      <table class="dep-table">
        <thead><tr><th>前置编号</th><th>前置任务</th><th>编码</th><th>前置态</th><th>齐件核对</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderRunDetail() {
  const sel = state.selected;
  return `
    <div class="card">
      <h3>运行详情</h3>
      <p class="kv">
        状态：${statusChip(sel.status)}
        · 已抽取 <strong>${sel.extracted}</strong>
        · 已落库 <strong>${sel.landed}</strong>
      </p>
      <p class="kv mono small">${esc(sel.source_uri)}</p>
      <p class="kv">中断数：<strong>${sel.interrupt_count}</strong>
        · 下游争用败绩：<strong>${sel.contest_lost_count}</strong> 次
        ${sel.lock_owner ? `· 抽取锁：${esc(sel.lock_owner)} @ ${esc(sel.lock_at)}` : ''}
      </p>
      ${sel.interrupt_reason ? `<p class="reason">中断缘由：${esc(sel.interrupt_reason)}</p>` : ''}
      ${sel.last_gate_reason ? `<p class="reason">闸口回写：${esc(sel.last_gate_reason)}</p>` : ''}
      ${sel.force_reason ? `<p class="reason ok-line">强抽缘故：${esc(sel.force_reason)}（${esc(sel.force_confirmed_by)} · ${esc(sel.force_confirmed_at)}）</p>` : ''}
    </div>`;
}

function renderConfirmations() {
  if (!state.confirmations.length) {
    return `<div class="card"><h3>等待舱人工确认痕迹</h3><p class="hint">暂无确认记录。</p></div>`;
  }
  const rows = state.confirmations.map((c) => `
    <li>
      <strong>${esc(c.confirmed_at)}</strong> · ${esc(c.confirmed_by)}
      <div>强抽缘故：${esc(c.reason)}</div>
      <div class="mono small">前置快照：${esc(c.upstream_snapshot)}</div>
    </li>`).join('');
  return `
    <div class="card">
      <h3>等待舱人工确认痕迹 <span class="sub">（可单独核对）</span></h3>
      <ul class="confirm-list">${rows}</ul>
    </div>`;
}

function renderActions() {
  const sel = state.selected;
  if (!sel.extract_entry_visible) {
    return `
      <div class="card gate-card">
        <h3>抽取入口</h3>
        <p class="reason">前置仍在抽取中（或本任务在抽），下游入口保持看不见，待前置抽取结束后开放。</p>
      </div>`;
  }
  if (sel.in_hold) {
    return `
      <div class="card gate-card tone-hold-bg">
        <h3>等待舱 · 人工确认强行开抽</h3>
        <p class="hint">任务已进等待舱，须填写强抽缘故并人工确认，才允许强行开抽。</p>
        <label class="field">强抽缘故
          <input id="force-reason" value="${esc(state.forceDraft)}" placeholder="例如：前置数据已人工核对可用，申请强抽" />
        </label>
        <div class="actions">
          <button type="button" id="btn-force" ${state.busy ? 'disabled' : ''}>人工确认并强行开抽</button>
        </div>
      </div>`;
  }
  return `
    <div class="card">
      <h3>操作</h3>
      <div class="actions">
        <button type="button" id="btn-ex" ${state.busy ? 'disabled' : ''}>执行抽取</button>
        <button type="button" id="btn-land" class="ghost">执行落库</button>
      </div>
    </div>`;
}

function renderEditor() {
  const sel = state.selected;
  const checks = state.items
    .filter((it) => it.id !== sel.id)
    .map((it) => `
      <label class="check">
        <input type="checkbox" data-up="${it.id}" ${state.depForm.upstreamIds.has(it.id) ? 'checked' : ''} />
        <span class="mono">#${it.id} ${esc(it.code)}</span> ${esc(it.title)} ${statusChip(it.status)}
      </label>`).join('');
  return `
    <div class="card">
      <h3>编排前置依赖图</h3>
      <p class="hint">下游至少挂一个前置任务编号；空依赖图不准保存。策略同轮钉死，当天不许再换。</p>
      <div class="check-grid">${checks}</div>
      <div class="policy-row">
        依赖策略：
        <label class="check"><input type="radio" name="policy" value="作废" ${state.depForm.policy === '作废' ? 'checked' : ''} /> 整批作废（未齐件→下游整批作废并标注中断缘由）</label>
        <label class="check"><input type="radio" name="policy" value="等待舱" ${state.depForm.policy === '等待舱' ? 'checked' : ''} /> 等待舱（进舱，人工确认后强行开抽）</label>
      </div>
      <div class="actions"><button type="button" id="btn-save-deps">钉死保存依赖图</button></div>
    </div>
    <div class="card">
      <h3>抽取路径</h3>
      <label class="field">路径（改漂后开抽会被后台拦截并回写中文原因）
        <input id="uri-input" value="${esc(state.uriDraft)}" />
      </label>
      <div class="actions"><button type="button" id="btn-save-uri" class="ghost">更新路径</button></div>
    </div>`;
}

function render() {
  const el = document.getElementById('app');
  const rows = state.items.map((it) => `
    <button type="button" class="row ${state.selected && state.selected.id === it.id ? 'on' : ''}" data-id="${it.id}">
      <strong>${esc(it.title)}</strong>
      <span class="mono">${esc(it.code)}</span>
      ${statusChip(it.status)}
    </button>`).join('');

  const detail = state.selected ? `
    <section class="detail">
      <h2>${esc(state.selected.title)} <span class="mono">#${state.selected.id}</span></h2>
      ${state.error ? `<p class="reason banner-bad">${esc(state.error)}</p>` : ''}
      ${state.ok ? `<p class="reason banner-ok">${esc(state.ok)}</p>` : ''}
      <div class="two-col">
        ${renderRunDetail()}
        ${renderDepList()}
      </div>
      ${renderActions()}
      ${renderConfirmations()}
      ${renderEditor()}
    </section>` : '<p class="hint">从左侧选择一条编排流</p>';

  el.innerHTML = `
    <header class="bar">
      <div class="brand">${esc(state.lab)}</div>
      <div>${esc(state.user)} · <button type="button" id="out" class="link">退出</button></div>
    </header>
    <div class="layout"><aside>${rows}</aside><main>${detail}</main></div>`;

  el.querySelectorAll('.row').forEach((b) => { b.onclick = () => select(+b.dataset.id); });
  const out = el.querySelector('#out');
  if (out) out.onclick = logout;
  const be = el.querySelector('#btn-ex');
  if (be) be.onclick = doExtract;
  const bl = el.querySelector('#btn-land');
  if (bl) bl.onclick = doLand;
  const bf = el.querySelector('#btn-force');
  if (bf) {
    bf.onclick = () => {
      state.forceDraft = document.getElementById('force-reason').value;
      doForceExtract();
    };
  }
  const bd = el.querySelector('#btn-save-deps');
  if (bd) bd.onclick = saveDeps;
  const bu = el.querySelector('#btn-save-uri');
  if (bu) bu.onclick = () => {
    state.uriDraft = document.getElementById('uri-input').value;
    saveUri();
  };
  el.querySelectorAll('input[data-up]').forEach((cb) => {
    cb.onchange = () => {
      const id = +cb.dataset.up;
      if (cb.checked) state.depForm.upstreamIds.add(id);
      else state.depForm.upstreamIds.delete(id);
    };
  });
  el.querySelectorAll('input[name=policy]').forEach((rb) => {
    rb.onchange = () => { state.depForm.policy = rb.value; };
  });
}

boot();
