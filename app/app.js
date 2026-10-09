// =============================================================
// タスク管理アプリ本体
// データはブラウザの localStorage（ブラウザ内の保存領域）に JSON 形式で保存する。
// FileMaker で言うと「タスク」テーブル 1 つだけのファイル。
// =============================================================

// 保存領域でのデータの名前（FileMaker のテーブル名のようなもの）
const STORAGE_KEY = 'task-app.tasks';

// 優先度の並び順（並べ替えに使う）
const PRIORITY_ORDER = { 高: 0, 中: 1, 低: 2 };

// ----- 画面の部品を取得しておく -----
const listView = document.getElementById('list-view');
const formView = document.getElementById('form-view');
const rowsEl = document.getElementById('task-rows');
const emptyEl = document.getElementById('empty');
const countEl = document.getElementById('count');
const filterEl = document.getElementById('filter-status');
const sortEl = document.getElementById('sort-order');
const formEl = document.getElementById('task-form');
const dialogEl = document.getElementById('confirm-dialog');

const f = {
  id: document.getElementById('f-id'),
  title: document.getElementById('f-title'),
  memo: document.getElementById('f-memo'),
  due: document.getElementById('f-due'),
  priority: document.getElementById('f-priority'),
  status: document.getElementById('f-status'),
  meta: document.getElementById('f-meta'),
  errTitle: document.getElementById('err-title'),
};

// =============================================================
// データの読み書き
// =============================================================

// 全タスクを読み込む（保存がなければダミーデータを入れる）
function loadTasks() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch (e) {
    console.warn('データの読み込みに失敗しました', e);
  }
  const dummy = createDummyTasks();
  saveTasks(dummy);
  return dummy;
}

// 全タスクを保存する
function saveTasks(tasks) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks));
  } catch (e) {
    alert('保存に失敗しました。ブラウザの設定（プライベートモード等）を確認してください。');
  }
}

// タスクID を作る（日時 + 乱数で重複しない値にする。FileMaker のシリアル番号の代わり）
function newId() {
  return 'T' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

// 今日から n 日後の日付を "YYYY-MM-DD" 形式で返す
function dateFromToday(n) {
  const d = new Date();
  d.setDate(d.getDate() + n);
  return toDateString(d);
}

function toDateString(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

// ダミーデータ（架空の内容のみ。実在の人名・社名は使わない）
function createDummyTasks() {
  const now = new Date().toISOString();
  const samples = [
    ['週次報告書を作成する', '先週分の作業内容をまとめる', -1, '高', '進行中'],
    ['会議室を予約する', '来週の定例会議用', 2, '中', '未着手'],
    ['サンプル資料の誤字チェック', '', 5, '低', '未着手'],
    ['経費精算を提出する', '交通費 3 件分', 0, '高', '未着手'],
    ['備品の在庫を確認する', 'コピー用紙とトナー', 7, '低', '完了'],
    ['テスト用データを準備する', 'ダミーの取引先データを 10 件', 3, '中', '進行中'],
  ];
  return samples.map(([title, memo, dueOffset, priority, status]) => ({
    id: newId(),
    title,
    memo,
    due_date: dateFromToday(dueOffset),
    priority,
    status,
    created_at: now,
    updated_at: now,
  }));
}

// =============================================================
// 一覧画面
// =============================================================

function renderList() {
  const tasks = loadTasks();
  const filter = filterEl.value;

  // 絞り込み（FileMaker の検索に相当）
  const shown = tasks.filter((t) => !filter || t.status === filter);

  // 並べ替え（どちらの順でも、完了したタスクは一番下にまとめる）
  //   期限が近い順  ：期限日の早い順（未設定は最後）→ 同じ日なら優先度の高い順
  //   優先度が高い順：高 → 中 → 低 → 同じ優先度なら期限日の早い順
  const byDue = (a, b) => {
    const dueA = a.due_date || '9999-99-99';
    const dueB = b.due_date || '9999-99-99';
    return dueA === dueB ? 0 : dueA < dueB ? -1 : 1;
  };
  const byPriority = (a, b) => PRIORITY_ORDER[a.priority] - PRIORITY_ORDER[b.priority];
  const [first, second] = sortEl.value === 'priority' ? [byPriority, byDue] : [byDue, byPriority];

  shown.sort((a, b) => {
    const doneA = a.status === '完了' ? 1 : 0;
    const doneB = b.status === '完了' ? 1 : 0;
    if (doneA !== doneB) return doneA - doneB;
    return first(a, b) || second(a, b);
  });

  rowsEl.innerHTML = '';
  const today = toDateString(new Date());

  shown.forEach((t) => {
    const tr = document.createElement('tr');
    if (t.status === '完了') tr.classList.add('done');
    tr.addEventListener('click', () => openForm(t.id));

    const overdue = t.due_date && t.due_date < today && t.status !== '完了';

    tr.append(
      cell('タイトル', t.title, 'title'),
      cell('期限日', t.due_date ? formatDate(t.due_date) + (overdue ? '（期限切れ）' : '') : '—', overdue ? 'overdue' : ''),
      badgeCell('優先度', t.priority, 'p-' + t.priority),
      badgeCell('ステータス', t.status, 's'),
      cell('更新日時', formatDateTime(t.updated_at)),
    );
    rowsEl.append(tr);
  });

  countEl.textContent = `${shown.length} 件 / 全 ${tasks.length} 件`;
  emptyEl.hidden = shown.length > 0;
}

// 表のセルを 1 つ作る（textContent を使うので、入力に記号が含まれても安全）
function cell(label, text, className) {
  const td = document.createElement('td');
  td.dataset.label = label;
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

// ラベル付きのセルを作る
function badgeCell(label, text, badgeClass) {
  const td = document.createElement('td');
  td.dataset.label = label;
  const span = document.createElement('span');
  span.className = 'badge ' + badgeClass;
  span.textContent = text;
  td.append(span);
  return td;
}

// "2026-10-07" → "2026/10/07"
function formatDate(s) {
  return s.replaceAll('-', '/');
}

// 保存された日時 → "2026/10/07 14:30"
function formatDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${formatDate(toDateString(d))} ${hh}:${mm}`;
}

// =============================================================
// 登録・編集画面
// =============================================================

// id を渡すと編集、渡さないと新規登録
function openForm(id) {
  formEl.reset();
  f.errTitle.hidden = true;

  if (id) {
    const t = loadTasks().find((x) => x.id === id);
    if (!t) return;
    document.getElementById('form-title').textContent = '編集';
    f.id.value = t.id;
    f.title.value = t.title;
    f.memo.value = t.memo;
    f.due.value = t.due_date;
    f.priority.value = t.priority;
    f.status.value = t.status;
    f.meta.textContent = `作成：${formatDateTime(t.created_at)}　更新：${formatDateTime(t.updated_at)}`;
    document.getElementById('btn-delete').hidden = false;
  } else {
    document.getElementById('form-title').textContent = '新規登録';
    f.id.value = '';
    f.priority.value = '中';    // 初期値
    f.status.value = '未着手';  // 初期値
    f.meta.textContent = '';
    document.getElementById('btn-delete').hidden = true;
  }

  showView('form');
  f.title.focus();
}

// 保存ボタン
formEl.addEventListener('submit', (e) => {
  e.preventDefault(); // 画面の再読み込みを止める

  const title = f.title.value.trim();
  if (!title) {
    f.errTitle.hidden = false;
    f.title.focus();
    return;
  }

  const tasks = loadTasks();
  const now = new Date().toISOString();
  const values = {
    title,
    memo: f.memo.value.trim(),
    due_date: f.due.value,
    priority: f.priority.value,
    status: f.status.value,
    updated_at: now, // 修正タイムスタンプ
  };

  if (f.id.value) {
    // 編集：既存レコードを上書き
    const t = tasks.find((x) => x.id === f.id.value);
    if (t) Object.assign(t, values);
  } else {
    // 新規：レコードを追加
    tasks.push({ id: newId(), created_at: now, ...values });
  }

  saveTasks(tasks);
  showView('list');
});

// キャンセルボタン
document.getElementById('btn-cancel').addEventListener('click', () => showView('list'));

// 削除ボタン → 確認ダイアログを開く
document.getElementById('btn-delete').addEventListener('click', () => {
  document.getElementById('confirm-message').textContent =
    `「${f.title.value}」を削除しますか？この操作は元に戻せません。`;
  dialogEl.showModal();
});

// 確認ダイアログ：削除する
document.getElementById('confirm-ok').addEventListener('click', () => {
  const tasks = loadTasks().filter((x) => x.id !== f.id.value);
  saveTasks(tasks);
  dialogEl.close();
  showView('list');
});

// 確認ダイアログ：やめる
document.getElementById('confirm-cancel').addEventListener('click', () => dialogEl.close());

// =============================================================
// 画面の切り替え・その他のボタン
// =============================================================

function showView(name) {
  listView.hidden = name !== 'list';
  formView.hidden = name !== 'form';
  if (name === 'list') renderList();
  window.scrollTo(0, 0);
}

document.getElementById('btn-new').addEventListener('click', () => openForm());
filterEl.addEventListener('change', renderList);
sortEl.addEventListener('change', renderList);

// ダミーデータに戻す
document.getElementById('btn-reset').addEventListener('click', () => {
  if (!confirm('今のデータをすべて消して、ダミーデータに戻しますか？')) return;
  saveTasks(createDummyTasks());
  renderList();
});

// 最初の表示
showView('list');
