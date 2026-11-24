import {heading, panel} from '../components/common/heading.js';
import {entries, records} from '../components/common/records.js';
import {topic} from '../constants/project.js';
import {date, el, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

function releasePanel(role, history) {
  if (!history) return '';
  if (history.state==='missing') {
    return panel('发布字段履历', `<p class="note" data-testid="history-missing">${esc(history.reason)}</p>`);
  }
  if (!history.fields.length) {
    return panel('发布字段履历', '<p class="note">这条会审没有记录任何字段变更，无法按字段撤销。原证据与签署照常保留。</p>');
  }
  const rows = history.fields.map(row=>[
    esc(row['镜头号']), esc(row['字段']), esc(row['发布前'] ?? '—'), esc(row['发布后'] ?? '—'),
    esc(`第 ${row['发布前版次']} 版 → 第 ${row['发布后版次']} 版`),
  ]);
  const conflicts = history.conflicts.length
    ? `<p class="error" data-testid="revoke-conflict">后续发布改过同一字段，整批撤销会被拦下：${
        history.conflicts.map(x=>`${esc(x['镜头号'])} 第 ${esc(x['版次'])} 版「${esc(x['字段'])}」`).join('、')}</p>` : '';
  const status = history.state==='revoked'
    ? `<p class="note" data-testid="revoked">已撤销${history.revocation ? ` · ${esc(history.revocation.actor)} · ${esc(date(history.revocation.created_at))}` : ''}</p>
       ${history.revocation ? `<p>${esc(history.revocation.summary)}</p>` : ''}`
    : conflicts
      ? '<p class="note">存在后续同字段修改，本批无法自动撤销；请保留原证据并人工核对。</p>'
      : role==='reviewer'
        ? '<form id="revoke-form"><label>撤销说明（可选）<textarea name="note" maxlength="500"></textarea></label><button class="secondary">撤销这次发布</button><p class="note">只恢复这次发布改过的字段，保留后续对其他字段的修改；成功后按新版发布，重试不会重复升版。</p></form>'
        : '<p class="note">复核员可从本页撤销这次发布。</p>';
  return panel('发布字段履历', records(['镜头号','字段','发布前','发布后','版次'], rows) + conflicts + status);
}

export function evidencePage(state, identity) {
  const current = state.evidence.find(row=>row.id===identity) || state.evidence[0];
  if (!current) return heading(topic.modules[3]) + '<p class="empty">执行完成后，可查看、复核和导出证据。</p>';
  const {report} = current;
  const metrics = Object.entries(report.metrics).map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  const sections = report.sections.map(x=>panel(x.title, entries(x.rows))).join('');
  const review = current.reviewer ? `<p data-testid="reviewed">${esc(current.reviewer)} · ${esc(date(current.reviewed_at))}</p><p>${esc(current.review_note)}</p>`
    : state.session.role==='reviewer'?'<form id="review-form"><label>复核意见<textarea name="note" maxlength="500" required></textarea></label><button>签署复核</button></form>'
    : '<p class="note">待复核员签署</p>';
  const history = state.evidence.map(row=>[`<a href="#evidence/${row.id}">执行 ${row.run_id}</a>`, esc(row.report.summary), esc(row.reviewer || '待复核')]);
  return `${heading(topic.modules[3], report.summary, `<button id="download" data-id="${current.id}">导出证据</button>`)}
    <div class="spread evidence-top"><dl class="metrics">${metrics}</dl><aside class="brief"><h2>复核签署</h2>${review}<details><summary>结果校验码</summary><code class="digest">${esc(current.digest)}</code></details></aside></div>
    <div id="release-history"></div>
    ${sections}<details><summary>历史证据</summary>${records(['执行','结论','复核'], history)}</details>`;
}

export function bindEvidence(role, api, update, fail) {
  const button = document.getElementById('download');
  if (!button) return;
  const identity = button.dataset.id;
  button.addEventListener('click', async()=>{
    try {
      const response = await api(`/evidence/${identity}/download`, {response:true});
      const url = URL.createObjectURL(await response.blob());
      const link = el('a');
      link.href = url;
      link.download = `evidence-${identity}.json`;
      link.click();
      setTimeout(()=>URL.revokeObjectURL(url), 1000);
    } catch (error) { fail(error); }
  });
  submit('review-form', async data=>{
    await api(`/evidence/${identity}/review`, {method:'POST', data:{note:data.get('note')}});
    await update();
  }, fail);

  const slot = document.getElementById('release-history');
  const mount = async()=>{
    if (!slot) return;
    try {
      const history = await api(`/evidence/${identity}/history`);
      slot.innerHTML = releasePanel(role, history);
      submit('revoke-form', async data=>{
        await api(`/evidence/${identity}/revoke`, {method:'POST', data:{note:String(data.get('note')||'')}});
        await update();
      }, fail);
    } catch (error) { slot.innerHTML = `<p class="note">${esc(error.message)}</p>`; }
  };
  void mount();
}
