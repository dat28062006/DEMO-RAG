'use strict';
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const formatDate = (value) => (value ? value.split('-').reverse().join('/') : 'Không giới hạn');
const formatTimestamp = (value) =>
  new Intl.DateTimeFormat('vi-VN', {
    timeZone: 'Asia/Ho_Chi_Minh',
    dateStyle: 'short',
    timeStyle: 'short',
  }).format(new Date(value));
const state = {
  bootstrap: null,
  userId: 'U01',
  sequence: 0,
  turns: [],
  documents: [],
  groups: {},
  selectedDoc: null,
  reported: null,
  source: null,
};
const labels = {
  'query.ask': 'Hỏi đáp',
  'document.approve': 'Duyệt',
  'document.review_company': 'Duyệt cấp công ty',
  'document.review_department': 'Duyệt người xem trong phòng',
  'document.override_review': 'Duyệt trực tiếp cấp tập đoàn',
  'document.publish': 'Công bố',
  'document.withdraw': 'Thu hồi',
  'document.create_version': 'Tạo phiên bản',
  'acl.manage': 'Chỉnh ACL',
  'feedback.review': 'Xem report',
};
const statusLabels = {
  published: 'Đã công bố',
  staging: 'Chưa công bố',
  withdrawn: 'Đã thu hồi',
  pending: 'Chờ duyệt',
  approved: 'Đã duyệt',
  rejected: 'Từ chối',
};
async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Không thể xử lý yêu cầu.');
  return data;
}
const post = (path, payload) =>
  api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
const profile = () => state.bootstrap?.users.find((u) => u.id === state.userId);
const has = (permission) => profile()?.permissions.includes(permission);
function notice(text, error = false) {
  $(error ? 'error-message' : 'success-message').textContent = text;
  $(error ? 'error-message' : 'success-message').hidden = false;
}
function clearNotices() {
  $('error-message').hidden = true;
  $('success-message').hidden = true;
}
function busy(value) {
  $('ask-button').disabled = value || !has('query.ask');
  $('compare-button').disabled = value || !has('query.ask');
  $('ask-button').textContent = value ? '…' : '↑';
}
function view(name) {
  ['chat', 'library', 'manage', 'reports'].forEach((id) => {
    $(`${id}-view`).hidden = id !== name;
    $(`nav-${id}`).classList.toggle('active', id === name);
  });
  clearNotices();
}
function newChat() {
  state.sequence++;
  state.turns = [];
  $('thread').innerHTML = '';
  $('greeting').hidden = false;
  $('conversation-title').textContent = 'Chưa có câu hỏi';
  $('question').value = '';
  busy(false);
  view('chat');
}
function selectUser(id) {
  state.userId = id;
  state.selectedDoc = null;
  ['source', 'report', 'version', 'import', 'llm'].forEach((name) => $(name + '-dialog').close());
  $('llm-key').value = '';
  newChat();
  renderProfile();
}
function renderProfile() {
  const u = profile();
  $('mode-label').textContent =
    state.bootstrap.mode === 'hybrid_rag'
      ? 'Hybrid RAG · ' +
        ({ openai: 'OpenAI API', groq: 'Groq API' }[state.bootstrap.models?.generation_provider] ||
          'Local')
      : 'Trích xuất từ nguồn';
  $('context-bar').innerHTML =
    `<span>${esc(u.company_name || 'Chưa có đơn vị')}</span>${u.department_name ? `<span>${esc(u.department_name)}</span>` : u.organization_level === 'group' ? '<span>Cấp tập đoàn</span>' : '<span>Chưa có phòng ban</span>'}<span>${esc(state.bootstrap.role_labels[u.role])}</span><span>Danh tính giả lập</span><span>${state.bootstrap.mode === 'hybrid_rag' ? 'Hybrid RAG' : 'Trích xuất từ nguồn'}</span>`;
  const management = u.permissions.some(
    (a) => a.startsWith('document.') || a === 'acl.manage' || a === 'audit.read',
  );
  $('nav-manage').hidden = !management;
  $('nav-reports').hidden = !has('feedback.review');
  $('open-llm').hidden = !has('system.configure_llm');
  $('tools-label').hidden = !management && !has('feedback.review') && !has('system.configure_llm');
  $('ask-button').disabled = !has('query.ask');
  $('open-import').hidden = !has('document.create_version') || !u.create_companies?.length;
}
function sourcesHtml(result) {
  const groups = new Map();
  result.citations.forEach((cite, index) => {
    if (!groups.has(cite.id)) groups.set(cite.id, []);
    groups.get(cite.id).push({ cite, index });
  });
  const passageButton = ({ cite: c, index }) => {
    const passage = result.answer_kind !== 'document_lookup' && c.quote;
    const location =
      (c.location || c.section) +
      (c.line_start
        ? ` · dòng ${c.line_start}${c.line_end !== c.line_start ? '–' + c.line_end : ''}`
        : '');
    const preview = passage ? c.quote.slice(0, 420) + (c.quote.length > 420 ? '…' : '') : '';
    return `<button class="citation ${passage ? 'citation-passage' : ''}" type="button" data-source="${esc(c.id)}" data-source-user="${esc(result.user.id)}" data-source-request="${passage ? esc(result.request_id) : ''}" data-citation-index="${index + 1}"><strong>▤ ${esc(c.title)} · v${esc(c.version)}</strong><span>[${index + 1}] ${esc(location)}</span>${preview ? `<q>${esc(preview)}</q><span>Mở đúng đoạn nguồn</span>` : ''}</button>`;
  };
  return [...groups.values()]
    .map(
      (items) =>
        `<div class="citation-group">${passageButton(items[0])}${items.length > 1 ? `<details><summary>${items.length - 1} đoạn nguồn khác trong tài liệu này</summary>${items.slice(1).map(passageButton).join('')}</details>` : ''}</div>`,
    )
    .join('');
}
function answerTextHtml(result) {
  const normalization =
    result.trace?.hybrid?.query_normalization || result.trace?.query_normalization;
  const notice = normalization?.changes?.length
    ? `<p class="source-note">Đã tìm theo: ${esc(normalization.normalized)}</p>`
    : '';
  if (!result.claims?.length) return notice + esc(result.answer);
  return (
    notice +
    result.claims
      .map(
        (claim) =>
          `<p class="answer-claim">${esc(claim.text)} ${claim.citation_indices
            .map((index) => {
              const cite = result.citations[index - 1];
              return cite
                ? `<button type="button" class="inline-citation" data-source="${esc(cite.id)}" data-source-user="${esc(result.user.id)}" data-source-request="${esc(result.request_id)}" data-citation-index="${index}" aria-label="Xem câu nguồn ${index}">[${index}]</button>`
                : '';
            })
            .join(' ')}</p>`,
      )
      .join('')
  );
}
function answerHtml(result, compare = false) {
  const statusLabel =
    result.reason === 'answer_generation_unavailable'
      ? 'Đã tìm thấy đoạn nguồn'
      : result.status_label;
  return `<div class="assistant-content"><div class="answer-header"><strong>${compare ? esc(result.user.name) + ' · ' + esc(result.user.company_name) : 'RAG'}</strong><span class="status-tag ${result.status === 'answered' ? '' : 'warning'}">${esc(statusLabel)}</span></div><div class="answer-text">${answerTextHtml(result)}</div><div class="follow-up-options">${(result.follow_up_options || []).map((option) => `<button type="button" data-follow-up="${esc(option.question)}" data-follow-up-user="${esc(result.user.id)}" ${compare ? 'disabled' : ''}>${esc(option.label)}</button>`).join('')}</div><div class="citations">${sourcesHtml(result)}</div><div class="answer-actions"><span>${result.mode === 'hybrid_llm' ? esc(result.generation?.model || 'LLM local') : result.mode === 'hybrid_extract' ? 'Hybrid · kiểm quy tắc' : 'Tra cứu nguồn'} · ${(result.elapsed_ms / 1000).toFixed(1)} s</span><button type="button" class="report-button" data-report="${esc(result.request_id)}" data-report-user="${esc(result.user.id)}">⚑ Báo lỗi</button><span>Request ${result.request_id.slice(0, 8)}</span></div><details class="trace-details"><summary>Xem cách kiểm chứng</summary>${result.steps.map((s) => `<p>${esc(s.label)}: ${esc(s.detail)}</p>`).join('')}</details></div>`;
}
function renderThread() {
  $('greeting').hidden = state.turns.length > 0;
  $('thread').innerHTML = state.turns
    .map(
      (turn) =>
        `<article class="turn"><div class="user-message"><p>${esc(turn.question)}</p><span class="message-context">${turn.compare ? 'So sánh An / Bình' : esc(turn.results[0].user.name)} · ${formatDate(turn.asOf)}</span></div><div class="assistant-message"><span class="assistant-icon">✳</span>${turn.compare ? `<div class="compare-answers">${turn.results.map((r) => answerHtml(r, true)).join('')}</div>` : answerHtml(turn.results[0])}</div></article>`,
    )
    .join('');
}
async function ask(compare = false, parentId = null) {
  if (!state.bootstrap || $('ask-button').disabled) return;
  const question = $('question').value.trim(),
    asOf = $('as-of').value;
  if (!question || !asOf) {
    $('ask-form').reportValidity();
    return;
  }
  const sequence = ++state.sequence;
  clearNotices();
  busy(true);
  view('chat');
  try {
    const ids = compare ? ['U01', 'U02'] : [state.userId];
    const results = await Promise.all(
      ids.map((userId) => {
        const previous = state.turns.at(-1)?.results.find((r) => r.user.id === userId);
        return post('/api/ask', {
          user_id: userId,
          question,
          as_of: asOf,
          parent_request_id: parentId || previous?.request_id,
        });
      }),
    );
    if (sequence !== state.sequence) return;
    state.turns.push({ question, asOf, compare, results });
    renderThread();
    $('conversation-title').textContent = question;
    $('question').value = '';
    $('thread').lastElementChild?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    if (sequence === state.sequence) notice(error.message, true);
  } finally {
    if (sequence === state.sequence) busy(false);
  }
}
function renderSourceText(source) {
  const target = $('source-text');
  target.replaceChildren();
  let cursor = 0;
  const ranges = (source.highlights || [])
    .filter(
      (r) =>
        Number.isInteger(r.start) &&
        Number.isInteger(r.end) &&
        r.start >= 0 &&
        r.end > r.start &&
        r.end <= source.text.length &&
        source.text.slice(r.start, r.end) === r.quote,
    )
    .sort((a, b) => a.start - b.start);
  for (const range of ranges) {
    if (range.start < cursor) continue;
    target.append(document.createTextNode(source.text.slice(cursor, range.start)));
    const mark = document.createElement('mark');
    mark.className = 'source-highlight';
    mark.textContent = source.text.slice(range.start, range.end);
    mark.title = 'Câu nguồn [' + range.citation_index + ']';
    target.append(mark);
    cursor = range.end;
  }
  target.append(document.createTextNode(source.text.slice(cursor)));
  $('source-highlight-note').hidden = !ranges.length;
  $('source-highlight-note').textContent = ranges.length
    ? 'Đoạn tô vàng là câu nguồn được viện dẫn [' + ranges[0].citation_index + '].'
    : '';
}
async function openSource(id, userId, requestId = null, citationIndex = null, full = false) {
  const sequence = state.sequence;
  try {
    let url = `/api/sources/${encodeURIComponent(id)}?user_id=${encodeURIComponent(userId)}`;
    if (requestId && citationIndex)
      url += `&request_id=${encodeURIComponent(requestId)}&citation_index=${encodeURIComponent(citationIndex)}&view=${full ? 'full' : 'passage'}`;
    const source = await api(url);
    if (sequence !== state.sequence) return;
    $('source-title').textContent = source.title;
    state.source = { id, userId, requestId, citationIndex };
    $('source-meta').textContent =
      `${source.id} · v${source.version} · ${source.citation_location || source.section}${source.line_start ? ' · dòng ' + source.line_start + '–' + source.line_end : ''} · ${formatDate(source.effective_from)} → ${formatDate(source.effective_to)}`;
    $('source-full').hidden = source.view !== 'passage';
    renderSourceText(source);
    $('source-original').hidden = !source.origin;
    if (source.origin) {
      $('source-original').href =
        `/api/sources/${encodeURIComponent(id)}/file?user_id=${encodeURIComponent(userId)}`;
      $('source-original').download = source.origin.filename;
      $('source-original').textContent = 'Tải file gốc · ' + source.origin.filename;
    }
    if (!$('source-dialog').open) $('source-dialog').showModal();
    $('source-text').querySelector('mark')?.scrollIntoView({ block: 'center' });
  } catch (error) {
    notice(error.message, true);
  }
}

function validityHtml(doc) {
  return `<span class="validity-badge ${esc(doc.validity)}">${esc(doc.validity_label)} · ${formatDate(doc.validity_as_of)}</span>`;
}
function groupedDocuments(documents, renderCard, libraryMode = false) {
  const current = documents.filter((d) => d.validity === 'effective');
  const history = documents.filter((d) =>
    ['expired', 'superseded', 'withdrawn'].includes(d.validity),
  );
  const waiting = documents.filter((d) => ['draft', 'upcoming'].includes(d.validity));
  const section = (key, title, docs, note, empty) =>
    `<section class="document-status-section" data-document-group="${key}"><h2>${title}<span>${docs.length}</span></h2>${note ? `<p class="document-group-note">${note}</p>` : ''}<div class="${libraryMode ? 'document-group-grid' : 'document-group-items'}">${docs.length ? docs.map(renderCard).join('') : `<p class="document-group-empty">${empty}</p>`}</div></section>`;
  return (
    section(
      'current',
      'Còn hiệu lực',
      current,
      '',
      'Không có tài liệu còn hiệu lực trong phạm vi này.',
    ) +
    section(
      'history',
      'Hết hiệu lực / bản cũ',
      history,
      'Nguồn hết hạn, đã thay thế hoặc thu hồi; dùng để đối chiếu lịch sử theo quyền đọc.',
      'Không có tài liệu hết hiệu lực hoặc bản cũ trong phạm vi này.',
    ) +
    (waiting.length
      ? `<details class="document-waiting" ${libraryMode ? '' : 'open'}><summary>Chờ duyệt / chưa đến hiệu lực <span>${waiting.length}</span></summary><p class="document-group-note">Chưa dùng trong hỏi đáp ở ngày này.</p><div class="${libraryMode ? 'document-group-grid' : 'document-group-items'}">${waiting.map(renderCard).join('')}</div></details>`
      : '')
  );
}
async function library() {
  view('library');
  const sequence = state.sequence;
  try {
    const day = $('library-date').value || $('as-of').value;
    const data = await api(`/api/library?user_id=${state.userId}&as_of=${encodeURIComponent(day)}`);
    if (sequence !== state.sequence) return;
    $('library-list').innerHTML = groupedDocuments(
      data.documents,
      (d) =>
        `<button type="button" class="library-card" data-source="${esc(d.id)}" data-source-user="${esc(state.userId)}"><strong>▤ ${esc(d.title)}</strong><span>${esc(d.id)} · v${esc(d.version)} · ${esc(d.section)}</span><span>Hiệu lực ${formatDate(d.effective_from)} → ${formatDate(d.effective_to)}</span>${validityHtml(d)}${d.query_eligible ? '' : d.validity === 'effective' ? '<span>Không tự áp dụng cho quyền lợi cá nhân; có thể tra cứu quy định theo phạm vi được cấp.</span>' : '<span>Đọc để đối chiếu; không dùng trả lời cho ngày này.</span>'}</button>`,
      true,
    );
  } catch (error) {
    notice(error.message, true);
  }
}
function indexLabel(doc) {
  const x = doc.index;
  return x?.status === 'ready'
    ? `Chỉ mục sẵn sàng · ${x.parent_count} parent / ${x.child_count} child`
    : x?.status === 'failed'
      ? 'Lập chỉ mục lỗi'
      : x?.status === 'processing'
        ? 'Đang lập chỉ mục'
        : 'Chưa lập chỉ mục';
}
function documentList() {
  const query = $('document-search').value.trim().toLocaleLowerCase('vi');
  const documents = state.documents.filter((d) =>
    (d.title + ' ' + d.id).toLocaleLowerCase('vi').includes(query),
  );
  $('document-list').innerHTML = groupedDocuments(
    documents,
    (d) =>
      `<button type="button" class="document-item ${state.selectedDoc?.id === d.id ? 'selected' : ''}" data-document="${esc(d.id)}"><strong>${esc(d.title)}</strong><small>${esc(d.id)} · v${esc(d.version)} · ${esc(statusLabels[d.approval])} · ${esc(statusLabels[d.status])}</small>${validityHtml(d)}</button>`,
  );
}
async function management() {
  view('manage');
  const sequence = state.sequence;
  try {
    const data = await api(`/api/review/documents?user_id=${state.userId}`);
    if (sequence !== state.sequence) return;
    state.documents = data.documents;
    state.groups = data.groups;
    documentList();
    if (state.selectedDoc && state.documents.some((d) => d.id === state.selectedDoc.id))
      await documentDetail(state.selectedDoc.id);
    else {
      state.selectedDoc = null;
      $('document-detail').innerHTML =
        '<div class="empty-panel">Chọn tài liệu để đọc và xem các thao tác được phép.</div>';
    }
  } catch (error) {
    notice(error.message, true);
  }
}
function accessPicker(selected, attribute = '', companyId = null, departmentId = null) {
  const tree = state.bootstrap.access_tree || { companies: [], unassigned: [] };
  const represented = new Set();
  const row = (key, label, aliases = [], level = '') => {
    const actual = [key, ...aliases].find((value) => selected.includes(value)) || key;
    [key, ...aliases].forEach((value) => represented.add(value));
    return `<label class="acl-row ${level}"><input type="checkbox" ${attribute} value="${esc(actual)}" data-acl-label="${esc(label)}" ${selected.includes(actual) ? 'checked' : ''}><span>${esc(label)}</span></label>`;
  };
  const person = (person) => {
    const u = state.bootstrap.users.find((u) => u.id === person.id);
    return row(
      'user:' + person.id,
      person.name + (u ? ' · ' + state.bootstrap.role_labels[u.role] : ''),
      [],
      'acl-person',
    );
  };
  const companies = tree.companies.filter((company) => !companyId || company.id === companyId);
  let html =
    (companyId ? '' : row('PUBLIC', 'Toàn bộ tổ chức')) +
    companies
      .map(
        (company) =>
          `<details class="acl-company" open><summary>${esc(company.name)}</summary>${departmentId ? '' : row('company:' + company.id, 'Cả ' + company.name, [company.id + '_STAFF'], 'acl-company-choice')}${company.departments
            .filter((d) => !departmentId || d.id === departmentId)
            .map(
              (department) =>
                `<details class="acl-department" open><summary>${esc(department.name)}</summary>${row('department:' + department.id, 'Cả phòng ' + department.name, [department.id], 'acl-department-choice')}<div class="acl-people">${department.people.map(person).join('') || '<span class="muted">Chưa có nhân sự.</span>'}</div></details>`,
            )
            .join('')}${departmentId ? '' : company.people.map(person).join('')}</details>`,
      )
      .join('');
  if (!companyId)
    html += (tree.groups || [])
      .map(
        (group) =>
          `<details class="acl-company"><summary>${esc(group.name)} · nhân sự cấp tập đoàn</summary>${group.people.map(person).join('')}</details>`,
      )
      .join('');
  if (!companyId && tree.unassigned.length)
    html += `<details class="acl-company"><summary>Nhân sự chưa có đơn vị</summary>${tree.unassigned.map(person).join('')}</details>`;
  const other = Object.entries(state.groups).filter(
    ([key]) => !represented.has(key) && (!companyId || selected.includes(key)),
  );
  if (other.length)
    html += `<details class="acl-legacy"><summary>Nhóm riêng đã cấu hình${companyId ? ' (vẫn giới hạn trong công ty)' : ''}</summary>${other.map(([key, label]) => row(key, label)).join('')}</details>`;
  if (companyId)
    html +=
      '<p class="muted">Nhân sự nội bộ cần ACL phù hợp. Người được cấp quyền đọc tổ chức ở cấp tập đoàn có thể đọc công ty con; quyền này cấu hình riêng và deny vẫn được ưu tiên.</p>';
  return `<div class="acl-tree">${html}<p class="acl-selection" aria-live="polite"></p></div>`;
}
function aclSelection(root) {
  const selected = [...root.querySelectorAll('input:checked')].map(
    (input) => input.dataset.aclLabel || input.value,
  );
  root.querySelectorAll('.acl-selection').forEach((line) => {
    const own = [...line.parentElement.querySelectorAll('input:checked')].map(
      (input) => input.dataset.aclLabel || input.value,
    );
    line.textContent = own.length ? 'Đã chọn: ' + own.join('; ') : 'Chưa chọn quyền đọc.';
  });
}
function permissionInputs(doc, type) {
  return `<div class="permission-group"><strong>${type === 'allow' ? 'Cho phép đọc' : 'Từ chối đọc (ưu tiên hơn allow)'}</strong>${accessPicker(doc[type], `data-permission="${type}"`, doc.applies_company, doc.applies_department)}</div>`;
}
function reviewHtml(doc) {
  const review = doc.review;
  if (!review) return '';
  const actions = doc.review_actions || {};
  const names = (ids) =>
    ids.map((id) => state.bootstrap.users.find((u) => u.id === id)?.name || id).join(', ');
  const stage = {
    company: 'Chờ giám đốc xác nhận phạm vi người xem',
    departments: 'Chờ trưởng phòng duyệt nhân sự',
    approved: 'Đã hoàn tất duyệt người xem',
  }[review.stage];
  let html = `<section class="permission-editor"><h3>Duyệt người xem theo cấp tổ chức</h3><p><strong>${esc(stage)}</strong> · Lượt duyệt ${review.revision}</p><p>Giám đốc chọn phạm vi tối đa. Trưởng phòng chỉ chọn người của phòng mình trong phạm vi đó. Hoàn tất các phòng mới được công bố.</p>`;
  if (review.company_approved_by)
    html += `<p>Người duyệt cấp công ty/tập đoàn: ${esc(names([review.company_approved_by]))}</p>`;
  if (review.override_reason)
    html += `<p>Duyệt trực tiếp cấp tập đoàn: ${esc(review.override_reason)}</p>`;
  if (doc.status === 'staging' && review.stage === 'company' && actions.company) {
    html += `<div class="permission-grid"><div><strong>Phạm vi người xem được duyệt</strong>${accessPicker(review.proposed_allow, 'data-review-company="allow"', doc.applies_company, doc.applies_department)}</div><div><strong>Loại trừ khỏi quyền đọc</strong>${accessPicker(review.proposed_deny, 'data-review-company="deny"', doc.applies_company, doc.applies_department)}</div></div><button class="action-button" id="approve-company">Duyệt phạm vi và chuyển trưởng phòng</button>`;
  }
  for (const department of review.departments) {
    const actionable =
      doc.status === 'staging' &&
      department.status === 'pending' &&
      actions.departments?.includes(department.department_id);
    html += `<div class="permission-group"><h4>${esc(department.department_name)} · ${department.status === 'approved' ? 'Đã duyệt' : 'Chờ duyệt'}</h4>`;
    if (actionable) {
      html += department.candidates
        .map(
          (id) =>
            `<label class="acl-row"><input type="checkbox" data-review-person="${esc(department.department_id)}" value="${esc(id)}" checked><span>${esc(names([id]))}</span></label>`,
        )
        .join('');
      html += `<button class="action-button" data-approve-department="${esc(department.department_id)}">Duyệt người xem của phòng</button>`;
    } else
      html += `<p>${esc(names(department.status === 'approved' ? department.selected : department.candidates)) || 'Không cấp người xem trong phòng này.'}</p>`;
    html += '</div>';
  }
  if (doc.status === 'staging' && review.stage !== 'approved' && actions.override) {
    html += `<div class="permission-group"><h4>Quyền quản trị tập đoàn</h4><p>Chủ tịch có thể duyệt trực tiếp phạm vi đã đề xuất; lý do và danh sách người xem được lưu audit.</p><label>Lý do duyệt trực tiếp<input id="override-review-reason" type="text" maxlength="1000" placeholder="Ghi lý do duyệt trực tiếp..."></label><button class="action-button" id="approve-override">Duyệt trực tiếp cấp tập đoàn</button></div>`;
  }
  return html + '</section>';
}
async function documentDetail(id) {
  const sequence = state.sequence;
  try {
    const doc = await api(
      `/api/review/documents/${encodeURIComponent(id)}?user_id=${state.userId}`,
    );
    if (sequence !== state.sequence) return;
    state.selectedDoc = doc;
    const allowed = (a) => doc.permissions.includes(a);
    const buttons = [
      !doc.review &&
      allowed('document.approve') &&
      doc.status === 'staging' &&
      doc.approval === 'pending'
        ? '<button class="action-button" data-action="approve">Duyệt tài liệu</button>'
        : '',
      allowed('document.publish') && doc.status === 'staging'
        ? `<button class="action-button" data-action="publish" ${doc.approval !== 'approved' ? 'disabled' : ''}>${doc.validity === 'expired' ? 'Công bố bản lịch sử' : 'Công bố'}</button>`
        : '',
      allowed('document.withdraw') && doc.status === 'published'
        ? '<button class="action-button danger" data-action="withdraw">Thu hồi</button>'
        : '',
      allowed('document.create_version')
        ? '<button class="action-button secondary" id="create-version">Chỉnh sửa / tạo phiên bản</button><button class="action-button secondary" data-action="reindex">Lập lại chỉ mục</button>'
        : '',
    ].join('');
    $('document-detail').innerHTML =
      `<h2>${esc(doc.title)}</h2><div class="doc-meta"><span>${esc(doc.id)} · v${esc(doc.version)}</span><span>${esc(statusLabels[doc.approval])}</span><span>${esc(statusLabels[doc.status])}</span><span>${formatDate(doc.effective_from)} → ${formatDate(doc.effective_to)}</span>${validityHtml(doc)}</div>${doc.validity === 'expired' ? '<p class="validity-note">Tài liệu đã hết hiệu lực. Chỉ tra cứu lịch sử; muốn áp dụng lại cần tạo bản mới, duyệt và công bố.</p>' : ''}<div class="muted">Quyền thao tác: ${
        doc.permissions
          .filter((a) => labels[a])
          .map((a) => esc(labels[a]))
          .join(' · ') || 'Chỉ xem'
      }. Duyệt và công bố là hai thao tác; một người có thể được cấp cả hai.</div><p class="muted">${esc(indexLabel(doc))}${doc.index?.error ? ' · ' + esc(doc.index.error) : ''}</p><div class="doc-actions">${buttons}</div>${reviewHtml(doc)}${doc.origin ? `<a class="action-button" download href="/api/review/documents/${encodeURIComponent(doc.id)}/file?user_id=${encodeURIComponent(state.userId)}">Tải file gốc · ${esc(doc.origin.filename)}</a>` : ''}<pre class="document-text">${esc(doc.text)}</pre>${allowed('acl.manage') ? `<section class="permission-editor"><h3>Quyền truy cập tài liệu</h3><p>Deny thắng allow. Đổi quyền ở cấp công ty sẽ đưa tài liệu về chuỗi duyệt; quản trị tập đoàn có thể cập nhật trực tiếp.</p><div class="permission-grid">${permissionInputs(doc, 'allow')}${permissionInputs(doc, 'deny')}</div><button class="action-button" id="save-permissions">Lưu quyền truy cập</button></section>` : ''}${doc.audit.length ? `<div class="audit-list"><strong>Audit</strong>${doc.audit.map((a) => `<p>${esc(formatTimestamp(a.occurred_at))} · ${esc(a.actor_id)} · ${esc(a.action)}</p>`).join('')}</div>` : ''}`;
    documentList();
    aclSelection($('document-detail'));
  } catch (error) {
    state.selectedDoc = null;
    notice(error.message, true);
  }
}
async function updateDocument(action, extra = {}) {
  const id = state.selectedDoc?.id;
  if (!id) return;
  try {
    const result = await post(`/api/review/documents/${encodeURIComponent(id)}`, {
      user_id: state.userId,
      action,
      expected_checksum: state.selectedDoc.checksum,
      expected_review_revision: state.selectedDoc.review?.revision,
      ...extra,
    });
    await management();
    notice(
      action === 'set_permissions'
        ? 'Đã lưu quyền truy cập. Kiểm tra bước duyệt hiện tại trước khi công bố.'
        : action === 'create_version'
          ? `Đã tạo phiên bản chờ duyệt ${result.document_id}.`
          : 'Đã cập nhật trạng thái tài liệu.',
    );
    if (action === 'create_version') await documentDetail(result.document_id);
    if (result.index && result.index.status !== 'ready' && result.index.status !== 'disabled')
      notice(
        'Đã lưu tài liệu, nhưng chỉ mục chưa sẵn sàng: ' +
          (result.index.error || result.index.status) +
          '. Chọn Lập lại chỉ mục trước khi công bố.',
        true,
      );
    return true;
  } catch (error) {
    notice(error.message, true);
    return false;
  }
}
async function reports() {
  view('reports');
  const sequence = state.sequence;
  try {
    const data = await api(`/api/reports?user_id=${state.userId}`);
    if (sequence !== state.sequence) return;
    $('reports-list').innerHTML =
      data.reports
        .map(
          (r) =>
            `<article class="report-card"><h3>${esc(r.id)} ${r.restricted ? '· Context bị hạn chế quyền' : ''}</h3><div class="report-meta"><span>${esc(formatTimestamp(r.created_at))}</span><span>${esc(r.category)}</span>${r.actor_id ? `<span>Hồ sơ ${esc(r.actor_id)} · Request ${esc(r.request_id.slice(0, 8))}</span>` : ''}</div><p>${r.restricted ? 'Quyền hiện hành chưa cho phép xem toàn bộ nguồn của context này. Nội dung report và hội thoại được ẩn.' : esc(r.description)}</p>${r.restricted ? '' : `<details><summary>Xem toàn bộ hội thoại & context (${r.snapshot.conversation.length} lượt)</summary><pre>${esc(JSON.stringify(r.snapshot, null, 2))}</pre></details>`}<select data-report-status="${esc(r.id)}" aria-label="Trạng thái ${esc(r.id)}"><option value="open" ${r.status === 'open' ? 'selected' : ''}>Mới tiếp nhận</option><option value="investigating" ${r.status === 'investigating' ? 'selected' : ''}>Đang xác minh</option><option value="resolved" ${r.status === 'resolved' ? 'selected' : ''}>Đã xử lý</option></select></article>`,
        )
        .join('') ||
      '<div class="empty-panel">Chưa có report. Người dùng có thể báo lỗi dưới mỗi câu trả lời.</div>';
  } catch (error) {
    notice(error.message, true);
  }
}
$('profile-select').addEventListener('change', (e) => selectUser(e.target.value));
$('new-chat').addEventListener('click', newChat);
$('nav-chat').addEventListener('click', () => view('chat'));
$('nav-library').addEventListener('click', library);
$('nav-manage').addEventListener('click', management);
$('nav-reports').addEventListener('click', reports);
$('ask-form').addEventListener('submit', (e) => {
  e.preventDefault();
  ask();
});
$('compare-button').addEventListener('click', () => ask(true));
$('question').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    ask();
  }
});
document.querySelectorAll('[data-question]').forEach((button) =>
  button.addEventListener('click', () => {
    $('question').value = button.dataset.question;
    ask();
  }),
);
document.body.addEventListener('click', (e) => {
  if (e.target.closest('#approve-company')) {
    const selected = (type) =>
      [
        ...$('document-detail').querySelectorAll(`input[data-review-company="${type}"]:checked`),
      ].map((input) => input.value);
    updateDocument('approve_company', { allow: selected('allow'), deny: selected('deny') });
    return;
  }
  const departmentReview = e.target.closest('[data-approve-department]');
  if (departmentReview) {
    const department = departmentReview.dataset.approveDepartment;
    const selected = [...$('document-detail').querySelectorAll('input[data-review-person]:checked')]
      .filter((input) => input.dataset.reviewPerson === department)
      .map((input) => input.value);
    updateDocument('approve_department', {
      department_id: department,
      selected_user_ids: selected,
    });
    return;
  }
  if (e.target.closest('#approve-override')) {
    const fields = $('document-detail').querySelectorAll('input[data-review-company]');
    const extra = fields.length
      ? {
          allow: [...fields]
            .filter((f) => f.checked && f.dataset.reviewCompany === 'allow')
            .map((f) => f.value),
          deny: [...fields]
            .filter((f) => f.checked && f.dataset.reviewCompany === 'deny')
            .map((f) => f.value),
        }
      : {};
    updateDocument('approve_override', { ...extra, reason: $('override-review-reason').value });
    return;
  }
  const followUp = e.target.closest('[data-follow-up]');
  if (followUp) {
    if (
      followUp.disabled ||
      followUp.dataset.followUpUser !== state.userId ||
      $('ask-button').disabled
    )
      return;
    $('question').value = followUp.dataset.followUp;
    const parentId = followUp.closest('.assistant-content').querySelector('[data-report]')
      .dataset.report;
    ask(false, parentId);
    return;
  }
  const source = e.target.closest('[data-source]');
  if (source) {
    openSource(
      source.dataset.source,
      source.dataset.sourceUser,
      source.dataset.sourceRequest,
      source.dataset.citationIndex,
    );
    return;
  }
  if (e.target.closest('#source-full') && state.source) {
    const { id, userId, requestId, citationIndex } = state.source;
    openSource(id, userId, requestId, citationIndex, true);
    return;
  }
  const doc = e.target.closest('[data-document]');
  if (doc) {
    documentDetail(doc.dataset.document);
    return;
  }
  const report = e.target.closest('[data-report]');
  if (report) {
    state.reported = { requestId: report.dataset.report, userId: report.dataset.reportUser };
    $('report-description').value = '';
    $('report-dialog').showModal();
    return;
  }
  const action = e.target.closest('[data-action]');
  if (action) {
    updateDocument(action.dataset.action, {
      archive: action.dataset.action === 'publish' && state.selectedDoc.validity === 'expired',
      expected_checksum: state.selectedDoc.checksum,
    });
    return;
  }
  if (e.target.closest('#save-permissions')) {
    const values = (type) =>
      [...$('document-detail').querySelectorAll(`input[data-permission="${type}"]:checked`)].map(
        (input) => input.value,
      );
    updateDocument('set_permissions', { allow: values('allow'), deny: values('deny') });
  }
  if (e.target.closest('#create-version')) {
    $('version-title').textContent = state.selectedDoc.title;
    $('version-name').value = state.selectedDoc.title;
    $('version-date').value = state.bootstrap.default_as_of;
    $('version-end').value = '';
    $('version-body').value = state.selectedDoc.section_text;
    $('version-dialog').showModal();
  }
});
$('document-search').addEventListener('input', documentList);
$('report-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!state.reported) return;
  const button = $('submit-report');
  button.disabled = true;
  try {
    const uid = state.reported.userId;
    const ids = state.turns.flatMap((t) =>
      t.results.filter((r) => r.user.id === uid).map((r) => r.request_id),
    );
    const result = await post('/api/feedback', {
      user_id: uid,
      request_id: state.reported.requestId,
      conversation_request_ids: ids,
      category: $('report-category').value,
      description: $('report-description').value,
    });
    $('report-dialog').close();
    notice(`Đã lưu report ${result.id} cùng context của ${result.turns_saved} lượt hỏi.`);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});
$('version-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (
    await updateDocument('create_version', {
      title: $('version-name').value,
      body: $('version-body').value,
      effective_from: $('version-date').value,
      effective_to: $('version-end').value || null,
      expected_checksum: state.selectedDoc.checksum,
    })
  )
    $('version-dialog').close();
});
$('reports-list').addEventListener('change', async (e) => {
  const select = e.target.closest('[data-report-status]');
  if (!select) return;
  try {
    await post(`/api/reports/${encodeURIComponent(select.dataset.reportStatus)}`, {
      user_id: state.userId,
      status: select.value,
    });
    notice('Đã cập nhật trạng thái report.');
  } catch (error) {
    notice(error.message, true);
  }
});
function importDepartments() {
  const company = $('import-company').value;
  if (company === '__group__') {
    $('import-department').innerHTML =
      '<option value="">Mọi phòng ban trong phạm vi được cấp đọc</option>';
    $('import-department').disabled = true;
    return;
  }
  $('import-department').disabled = false;
  const scopes = profile().create_scopes || [];
  const wholeCompany = scopes.some((s) => s.company_id === company && !s.department_id);
  const departments = scopes.filter((s) => s.company_id === company).map((s) => s.department_id);
  $('import-department').innerHTML =
    (wholeCompany ? '<option value="">Mọi phòng ban trong công ty</option>' : '') +
    state.bootstrap.organizations
      .filter(
        (o) =>
          o.level === 'department' &&
          o.parent_id === company &&
          (wholeCompany || departments.includes(o.id)),
      )
      .map((o) => `<option value="${esc(o.id)}">${esc(o.name)}</option>`)
      .join('');
}
function importReaders() {
  const current = [...$('import-readers').querySelectorAll('input:checked')].map(
    (input) => input.value,
  );
  const companyId = $('import-company').value,
    company = state.bootstrap.organizations.find((o) => o.id === companyId);
  const groupScope = companyId === '__group__';
  $('import-readers').innerHTML =
    (groupScope
      ? '<p class="muted">Tài liệu dùng chung cấp tập đoàn. Có thể tích đồng thời cả Công ty A, Công ty B, hoặc chọn từng phòng/nhân sự trong cây bên dưới. Chỉ danh sách được cấp quyền mới được đọc; import vẫn cần duyệt và công bố.</p>'
      : `<p class="muted">Đang cấp quyền đọc cho tài liệu thuộc ${esc(company?.name || companyId)}. Muốn cấp cho nhiều công ty, chọn phạm vi Tập đoàn / nhiều công ty ở phía trên (nếu được cấp quyền).</p>`) +
    accessPicker(current, '', groupScope ? null : companyId, $('import-department').value || null);
  aclSelection($('import-readers'));
}
function importFields() {
  const file = $('import-file').files[0],
    json = file?.name.toLowerCase().endsWith('.json');
  $('import-metadata').hidden = !file || json;
  ['import-title', 'import-from', 'import-version'].forEach(
    (id) => ($(id).required = !!file && !json),
  );
  $('import-leave').hidden = $('import-topic').value !== 'leave';
  $('import-personal').hidden = $('import-kind').value === 'entitlement';
  ['import-period-from', 'import-period-to'].forEach(
    (id) =>
      ($(id).required =
        !!file &&
        !json &&
        $('import-topic').value === 'leave' &&
        $('import-kind').value !== 'entitlement'),
  );
}
$('open-import').addEventListener('click', async () => {
  try {
    if (!(await refreshBootstrap())) return;
    $('import-file').value = '';
    $('import-readers').innerHTML = '';
    $('import-error').hidden = true;
    $('import-topic').value = 'general';
    $('import-kind').value = 'entitlement';
    $('import-role').innerHTML =
      '<option value="">Mọi chức danh trong phạm vi</option>' +
      state.bootstrap.roles
        .map((role) => `<option value="${esc(role.id)}">${esc(role.name)}</option>`)
        .join('');
    $('import-role').value = '';
    const companies = state.bootstrap.organizations.filter(
      (o) => o.level === 'company' && profile().create_companies.includes(o.id),
    );
    if (!companies.length) throw new Error('Bạn chưa được cấp quyền import trong công ty nào.');
    $('import-company').innerHTML =
      (profile().create_group
        ? '<option value="__group__">Tập đoàn / nhiều công ty</option>'
        : '') +
      companies.map((o) => `<option value="${esc(o.id)}">${esc(o.name)}</option>`).join('');
    $('import-company').value = companies.some((o) => o.id === profile().company_id)
      ? profile().company_id
      : companies[0]?.id;
    $('import-subject').innerHTML = state.bootstrap.users
      .map((u) => `<option value="${esc(u.id)}">${esc(u.name)}</option>`)
      .join('');
    $('import-subject').value = 'U01';
    importDepartments();
    importReaders();
    importFields();
    $('import-dialog').showModal();
  } catch (error) {
    notice(error.message, true);
  }
});
$('import-file').addEventListener('change', () => {
  const file = $('import-file').files[0];
  if (file) $('import-title').value = file.name.replace(/\.[^.]+$/, '');
  importFields();
  $('import-error').hidden = true;
});
$('import-company').addEventListener('change', () => {
  importDepartments();
  importReaders();
});
$('import-department').addEventListener('change', importReaders);
['import-topic', 'import-kind', 'import-subject'].forEach((id) =>
  $(id).addEventListener('change', () => {
    importFields();
    importReaders();
  }),
);
document.body.addEventListener('change', (e) => {
  const tree = e.target.closest('.acl-tree');
  if (tree) aclSelection(tree);
});
$('import-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const file = $('import-file').files[0];
  if (!file) return;
  const button = $('submit-import');
  button.disabled = true;
  $('import-error').hidden = true;
  try {
    let data;
    if (file.name.toLowerCase().endsWith('.json')) {
      if (file.size > 120000) throw new Error('Gói JSON vượt quá 120 KB.');
      const bundle = JSON.parse(await file.text());
      data = await post('/api/import', { user_id: state.userId, bundle });
    } else {
      if (file.size > 2 * 1024 * 1024) throw new Error('File vượt quá 2 MB.');
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = '';
      for (let offset = 0; offset < bytes.length; offset += 8192)
        binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
      const personal =
        $('import-topic').value === 'leave' && $('import-kind').value !== 'entitlement';
      const metadata = {
        title: $('import-title').value,
        version: $('import-version').value,
        policy_key: $('import-topic').value,
        effective_from: $('import-from').value,
        effective_to: $('import-to').value || null,
        applies_company:
          $('import-company').value === '__group__' ? null : $('import-company').value,
        scope_level: $('import-company').value === '__group__' ? 'group' : 'company',
        applies_department: $('import-department').value || null,
        applies_role: $('import-role').value || null,
        allow: [...$('import-readers').querySelectorAll('input:checked')].map(
          (input) => input.value,
        ),
        deny: [],
      };
      if (metadata.policy_key === 'leave')
        metadata.facts = {
          kind: $('import-kind').value,
          leave_type: 'nghỉ phép năm',
          ...(personal
            ? {
                subject_id: $('import-subject').value,
                period_start: $('import-period-from').value,
                period_end: $('import-period-to').value,
              }
            : {}),
        };
      data = await post('/api/import-file', {
        user_id: state.userId,
        file_name: file.name,
        file_base64: btoa(binary),
        metadata,
      });
    }
    $('import-dialog').close();
    await management();
    notice(
      `Đã import ${data.count} tài liệu: ${data.document_ids.join(', ')}. Tất cả chờ duyệt và chưa công bố. Chỉ mục: ${Object.entries(
        data.indexes || {},
      )
        .map(([id, x]) => id + ' — ' + x.status)
        .join('; ')}. Chọn hồ sơ có quyền duyệt, sau đó hồ sơ có quyền công bố.`,
    );
  } catch (error) {
    $('import-error').textContent = error.message;
    $('import-error').hidden = false;
  } finally {
    button.disabled = false;
  }
});
['source', 'report', 'version', 'import'].forEach((name) => {
  $(`close-${name}`).addEventListener('click', () => $(name + '-dialog').close());
  $(name + '-dialog').addEventListener('click', (e) => {
    if (e.target === $(name + '-dialog')) $(name + '-dialog').close();
  });
});
function populateProfiles(data, selectedId) {
  const rolesSeen = new Set();
  const representatives = data.users.filter((u) => {
    const key = `${u.role}:${u.company_id}:${u.department_id}`;
    if (rolesSeen.has(key)) return false;
    rolesSeen.add(key);
    return true;
  });
  if (selectedId && !representatives.some((u) => u.id === selectedId)) {
    const selected = data.users.find((u) => u.id === selectedId);
    if (selected) representatives.push(selected);
  }
  $('profile-select').innerHTML = representatives
    .map(
      (u) =>
        `<option value="${esc(u.id)}">${esc(u.name)} · ${esc(data.role_labels[u.role])} · ${esc(u.department_name || u.company_name || 'Chưa có đơn vị')}</option>`,
    )
    .join('');
  if (selectedId) $('profile-select').value = selectedId;
  return representatives;
}
async function refreshBootstrap() {
  const uid = state.userId,
    data = await api('/api/bootstrap');
  if (uid !== state.userId) return false;
  state.bootstrap = data;
  populateProfiles(data, uid);
  renderProfile();
  return true;
}
api('/api/bootstrap')
  .then((data) => {
    state.bootstrap = data;
    $('as-of').value = data.default_as_of;
    $('library-date').value = data.default_as_of;
    const representatives = populateProfiles(data);
    selectUser(representatives[0].id);
  })
  .catch((error) => {
    notice(error.message, true);
    busy(true);
  });
window.addEventListener('focus', () => {
  if (state.bootstrap) refreshBootstrap().catch(() => {});
});

$('library-date').addEventListener('change', library);
$('as-of').addEventListener('change', () => {
  $('library-date').value = $('as-of').value;
});

function llmStatus(data) {
  const name = { openai: 'OpenAI', groq: 'Groq' }[data.provider];
  $('llm-status').textContent = name
    ? `${name} · ${data.model} · ${data.key_ready ? 'Sẵn sàng' : 'Cần nhập lại key cho phiên server này'}`
    : `Đang dùng local · ${data.model || 'Chưa cấu hình'}`;
}
function llmProviderFields() {
  const provider = $('llm-provider').value,
    info = state.llmProviders?.find((p) => p.id === provider);
  $('llm-key-title').textContent = (info?.name || provider) + ' API key';
  $('activate-api').textContent = 'Kiểm tra kết nối & dùng ' + (info?.name || provider);
  $('llm-consent-label').textContent =
    'Cho phép gửi câu hỏi và đoạn tài liệu được quyền đọc tới ' +
    (info?.name || provider) +
    (provider === 'groq' ? '. Free Plan có giới hạn lượt/token.' : '. API có thể tính phí.');
}
$('llm-provider').addEventListener('change', () => {
  const info = state.llmProviders?.find((p) => p.id === $('llm-provider').value);
  $('llm-model').value = info?.default_model || '';
  $('llm-key').value = '';
  $('llm-consent').checked = false;
  llmProviderFields();
});
$('open-llm').addEventListener('click', async () => {
  const uid = state.userId;
  try {
    const data = await api(`/api/llm-settings?user_id=${encodeURIComponent(uid)}`);
    if (uid !== state.userId) return;
    state.llmProviders = data.providers;
    $('llm-key').value = '';
    $('llm-consent').checked = false;
    $('llm-error').hidden = true;
    $('llm-provider').value = ['openai', 'groq'].includes(data.provider) ? data.provider : 'groq';
    $('llm-model').value =
      data.provider === $('llm-provider').value
        ? data.model
        : data.providers.find((p) => p.id === $('llm-provider').value).default_model;
    llmProviderFields();
    llmStatus(data);
    $('llm-dialog').showModal();
  } catch (error) {
    notice(error.message, true);
  }
});
$('close-llm').addEventListener('click', () => $('llm-dialog').close());
$('llm-dialog').addEventListener('close', () => {
  $('llm-key').value = '';
  $('llm-consent').checked = false;
});
$('llm-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const uid = state.userId;
  $('activate-api').disabled = true;
  $('use-local').disabled = true;
  $('llm-error').hidden = true;
  $('llm-status').textContent = 'Đang kiểm tra kết nối API bằng dữ liệu mẫu…';
  try {
    const data = await post('/api/llm-settings', {
      user_id: uid,
      provider: $('llm-provider').value,
      model: $('llm-model').value.trim(),
      api_key: $('llm-key').value.trim(),
      consent: $('llm-consent').checked,
    });
    if (uid !== state.userId) return;
    llmStatus(data);
    await refreshBootstrap();
    notice(
      'Đã chuyển sinh đáp án và kiểm chứng sang ' +
        { openai: 'OpenAI', groq: 'Groq' }[data.provider] +
        ' API. Bạn có thể hỏi lại ngay.',
    );
    $('llm-dialog').close();
  } catch (error) {
    if (uid === state.userId) {
      $('llm-error').textContent = error.message;
      $('llm-error').hidden = false;
      $('llm-status').textContent = 'Chưa đổi cấu hình LLM.';
    }
  } finally {
    $('llm-key').value = '';
    $('activate-api').disabled = false;
    $('use-local').disabled = false;
  }
});
$('use-local').addEventListener('click', async () => {
  const uid = state.userId;
  $('use-local').disabled = true;
  $('activate-api').disabled = true;
  try {
    const data = await post('/api/llm-settings', { user_id: uid, provider: 'ollama' });
    if (uid !== state.userId) return;
    llmStatus(data);
    await refreshBootstrap();
    $('llm-dialog').close();
    notice('Đã dùng lại model local và xóa key API khỏi bộ nhớ server.');
  } catch (error) {
    $('llm-error').textContent = error.message;
    $('llm-error').hidden = false;
  } finally {
    $('use-local').disabled = false;
    $('activate-api').disabled = false;
  }
});
