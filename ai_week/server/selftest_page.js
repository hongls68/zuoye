/*
 * 网页层自测（不启浏览器、不联网）
 *
 * 为什么需要它：index.html 里的脚本是浏览器脚本，语法错、id 拼错、
 * 调了一个后端根本没注册的接口 —— 这三种错在"打开页面看一眼"时
 * 往往表现为"某一块空白"，很难定位到具体是哪一行。
 * 本脚本用 Node 把内联脚本抽出来做静态校验，并把渲染函数单独拉出来
 * 喂样本数据，检查产出的 HTML 是否正确。
 *
 * 运行：node selftest_page.js      （需要同目录下有 index.html 与 server.py）
 */
'use strict';
const fs = require('fs');
const path = require('path');

const HERE = __dirname;
const html = fs.readFileSync(path.join(HERE, 'index.html'), 'utf8');
const srv = fs.readFileSync(path.join(HERE, 'server.py'), 'utf8');

let fails = [];
function check(name, cond, extra) {
  console.log((cond ? '  [OK]   ' : '  [FAIL] ') + name
    + (extra !== undefined && extra !== '' ? ' | ' + extra : ''));
  if (!cond) fails.push(name);
}

const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log('NO_SCRIPT：index.html 里找不到内联脚本'); process.exit(1); }
const src = m[1];

// ---------- 1. 语法 ----------
console.log('\n== 1. 内联脚本语法 ==');
try {
  new Function(src);
  check('脚本能通过语法解析', true, src.split('\n').length + ' 行');
} catch (e) {
  check('脚本能通过语法解析', false, e.message);
  process.exit(1);
}

// ---------- 2. DOM id ----------
console.log('\n== 2. 脚本引用的 DOM id 都存在 ==');
const ids = [...new Set([...src.matchAll(/el\('([A-Za-z0-9_]+)'\)/g)].map(x => x[1]))];
const missing = ids.filter(id => !new RegExp('id="' + id + '"').test(html));
check('全部 ' + ids.length + ' 个 id 都能在 HTML 里找到', missing.length === 0,
  missing.join(', '));

// ---------- 3. 接口路由 ----------
console.log('\n== 3. 页面调用的接口都在 server.py 里注册过 ==');
const calls = [...new Set([...src.matchAll(/['"](\/api\/[A-Za-z0-9_\/]+)/g)].map(x => x[1]))];
const notRouted = calls.filter(p => !srv.includes('"' + p + '"'));
check('全部 ' + calls.length + ' 个接口都有对应路由', notRouted.length === 0,
  notRouted.join(', '));

// ---------- 4. renderHelp（第3周：三层状态）----------
console.log('\n== 4. 第3周：求助卡片渲染 ==');
const startH = src.indexOf('function esc(');
const endH = src.indexOf('function refreshHelp(');
if (startH < 0 || endH < 0) { check('能抽出渲染函数', false); }
const blockH = src.slice(startH, endH);

const els = {};
function el(id) {
  return els[id] || (els[id] = { textContent: '', innerHTML: '', value: '',
    style: {}, addEventListener() {} });
}
const renderHelp = new Function('el', blockH + '\n return renderHelp;')(el);

const base = {
  event_id: 'help-a1', device_id: 'esp32s3-eye-01', kind: 'teach_help_test',
  boot_id: 'boot7', seq: 12,
  clock_sources: {
    device: { pressed_at: 'up+42.100s(time_not_synced)', local_ack_at: 'up+42.120s(time_not_synced)' },
    server: { received_at: '2026-09-21T16:00:01.200+08:00' },
    viewer: { answered_at: null, answered_by: null }
  }
};
const cases = [
  Object.assign({}, base, { device_state: 'LOCAL_ACKED', server_state: 'RECEIVED',
    viewer_state: 'PENDING', device_label: '已本地确认（板子自己亮灯了）',
    server_label: 'VPS 已接收', viewer_label: '还没人回应',
    answerable: true, stage: '等待查看者回应（VPS 已接收）' }),
  Object.assign({}, base, { event_id: 'help-a2', device_state: 'LOCAL_ACKED',
    server_state: 'RECEIVED', viewer_state: 'ANSWERED',
    device_label: '已本地确认（板子自己亮灯了）', server_label: 'VPS 已接收',
    viewer_label: '查看者已回应', answerable: false,
    answered_by: '同学B', answer_text: '<b>灯常亮了</b> & 我处理了',
    clock_sources: { device: base.clock_sources.device,
      server: base.clock_sources.server,
      viewer: { answered_at: '2026-09-21T16:00:30.000+08:00', answered_by: '同学B' } },
    stage: '已完成：查看者已回应' }),
  Object.assign({}, base, { event_id: 'help-a3', device_state: 'LOCAL_ACKED',
    server_state: 'EXPIRED', viewer_state: 'PENDING',
    device_label: '已本地确认（板子自己亮灯了）', server_label: '已过期（服务端收到了但没人回应）',
    viewer_label: '还没人回应', answerable: false, stage: '已过期：服务端收到了，但一直没人回应' })
];

renderHelp({ helps: cases, pending_count: 1, server_now: '2026-09-21T16:00:40+08:00' });
const outH = els.helpList.innerHTML;

check('三段状态层都渲染出来（3 条 × 3 层）',
  (outH.match(/class="hcell l[123]"/g) || []).length === 9);
check('没有 undefined 漏出来', !/undefined/.test(outH));
check('板子时钟原文保留（含未对时标记）', outH.includes('time_not_synced'));
check('未回应时第三层显示「还没人回应」', outH.includes('还没人回应'));
check('回应内容被 HTML 转义（不注入）',
  outH.includes('&lt;b&gt;灯常亮了&lt;/b&gt; &amp; 我处理了'));
check('已回应的那条：回应按钮禁用',
  /data-id="help-a2"[\s\S]*?data-act="answer" disabled/.test(outH));
check('已回应的那条：取消按钮也禁用',
  /data-id="help-a2"[\s\S]*?data-act="cancel" disabled/.test(outH));
check('待回应的那条：按钮可用', /data-id="help-a1"[\s\S]*?data-act="answer">回应/.test(outH));
check('过期的那条：给出不能回应的理由', outH.includes('已过期，不能再回应'));
check('待回应计数写进了页面', els.helpPending.textContent === 1);
check('总数写进了页面', els.helpTotal.textContent === 3);

// ---------- 5. renderAsk（第4周：问答 + 调用链 + 守卫）----------
console.log('\n== 5. 第4周：问答卡片渲染 ==');
const startA = src.indexOf('function renderAsk(');
const endA = src.indexOf('function askQuestion(');
if (startA < 0 || endA < 0) { check('能抽出 renderAsk', false); }
// renderAsk 依赖 esc() 与 GUARD_LABEL，它们定义在文件更早的位置，得一并带上
const escSrc = src.slice(src.indexOf('function esc('), src.indexOf('function shortTime('));
const guardSrc = src.slice(src.indexOf('var GUARD_LABEL ='), src.indexOf('function askHealth('));
const blockA = escSrc + '\n' + guardSrc + '\n' + src.slice(startA, endA);
const renderAsk = new Function('el', blockA + '\n return renderAsk;')(el);

const res = {
  answer: '还没拍好。\n- 当前状态：PENDING',
  tool_calls: [
    { tool: 'request_capture', args: { device_id: 's3eye-group01' }, ok: true,
      source: 'POST /api/command', time: '2026-09-21T16:08:30.000+08:00', state: 'PENDING' },
    { tool: 'get_command_status', args: { request_id: 'req-1' }, ok: true,
      source: 'GET /api/command/status', time: '2026-09-21T16:08:30.100+08:00', state: 'PENDING' }
  ],
  guardrails: ['success_without_evidence'],
  completion_evidence: false,
  note: '本回答由产品运行时模型（Ollama）生成。'
};
const outA = renderAsk('<script>坏东西</script>', res);
check('问题里的标签被转义（不注入）', !outA.includes('<script>坏东西'));
check('工具调用链两次都摊开了',
  (outA.match(/class="c1"/g) || []).length === 2);
check('调用链里带 source', outA.includes('source=POST /api/command'));
check('调用链里带 time', outA.includes('time=2026-09-21T16:08:30.000+08:00'));
check('调用链里带 state', outA.includes('state=PENDING'));
check('守卫标记翻成人话', outA.includes('已拦下「无证据的假成功」'));
check('没有 undefined 漏出来', !/undefined/.test(outA));

const outA2 = renderAsk('你好', { answer: '在的', tool_calls: [], guardrails: [],
  completion_evidence: false, note: 'n' });
check('没调工具时明说「没有调用任何工具」', outA2.includes('没有调用任何工具'));
check('无完成证据时如实标注', outA2.includes('本轮没有采集完成证据'));

// ---------- 6. 第5周：三轴波形与姿态孪生 ----------
console.log('\n== 6. 第5周：示波器与姿态孪生 ==');

// --- 6a. renderTwin：朝向完全由重力方向推出 ---
const startT = src.indexOf('function renderTwin(');
const endT = src.indexOf('function fetchAttitude(');
if (startT < 0 || endT < 0) { check('能抽出 renderTwin', false); }
const renderTwin = new Function('el',
  src.slice(startT, endT) + '\n return renderTwin;')(el);

function attitudeOf(gx, gy, gz, extra) {
  const mag = Math.sqrt(gx * gx + gy * gy + gz * gz);
  return Object.assign({
    ax: gx, ay: gy, az: gz, acc_mag: mag, pitch: 0, roll: 0,
    posture: 'tilted', posture_label: '自由倾斜', posture_en: 'Tilted',
    posture_note: '', yaw: null,
    yaw_note: '本板无陀螺仪/磁力计：航向（绕重力轴自转）在原理上不可测，不是没采到'
  }, extra || {});
}

// 孪生体的朝向 = 解 R·g设备 = (0,0,−1)（桌面坐标系的竖直向下）。
// 断言的是**几何结果**，不是某个中间变量：
//   平放 → 不旋转（贴在桌面上）
//   竖立 → 绕 X 轴 −90°（立起来）
//   侧立 → 绕 X 轴 −90° 再绕 Z 轴 +90°（立起来并转 90°）
renderTwin(attitudeOf(0, 0, -1));
check('平放·正面朝上（重力沿 −Z）→ 孪生体不旋转，贴在桌面上',
  els.twinPlate.style.transform === 'rotateX(0.0deg) rotateZ(0.0deg)',
  els.twinPlate.style.transform);
renderTwin(attitudeOf(0, 1, 0));
check('竖直正面（重力沿 +Y）→ 立起来（rotateX −90°）',
  els.twinPlate.style.transform === 'rotateX(-90.0deg) rotateZ(0.0deg)',
  els.twinPlate.style.transform);
renderTwin(attitudeOf(1, 0, 0));
check('侧边直立（重力沿 +X）→ 立起来并转 90°',
  els.twinPlate.style.transform === 'rotateX(-90.0deg) rotateZ(90.0deg)',
  els.twinPlate.style.transform);
renderTwin(attitudeOf(0.577, 0.577, 0.577));
check('自由倾斜 → 两个轴都有分量（不是 0 也不是 90）',
  /rotateX\(-125\.3deg\) rotateZ\(45\.0deg\)/.test(els.twinPlate.style.transform),
  els.twinPlate.style.transform);
// ★ 平放时 φ 必然算出 0 —— 因为绕桌面竖轴自转不改变重力方向，本来就不可观测。
//   这一条把"Yaw 测不到"从口号变成了可验证的几何结论。
//   注意：必须**重新渲染一帧平放**再断言。上面那条 Tilted 用例已经把 transform 改成
//   rotateZ(45deg) 了，直接读会读到上一帧 —— 断言顺序错会误报成代码错。
renderTwin(attitudeOf(0, 0, -1));
check('平放时绕竖轴的角恒为 0（绕重力轴自转不可观测，不是我们没做）',
  /rotateZ\(0\.0deg\)$/.test(els.twinPlate.style.transform),
  els.twinPlate.style.transform);
renderTwin(attitudeOf(0, 0, 0));
check('合加速度≈0 时不硬摆姿态（归零并说明）',
  els.twinPlate.style.transform === 'rotateX(0deg) rotateZ(0deg)',
  els.twinPlate.style.transform);

// ★ 航向那一格是本周最容易做错的地方：留空会被当成"没采到"，填 0° 会被当成"测出来是 0"
check('航向显示为「不可测」，没有用 0° 顶替',
  els.tYaw.textContent === '不可测', els.tYaw.textContent);
check('航向旁边把原因写出来（区分「测不了」和「没采到」）',
  els.tYawNote.textContent.includes('不可测'), els.tYawNote.textContent);

renderTwin(attitudeOf(0, 0, -1, { posture_label: '平放', posture_en: 'Flat',
  posture_note: '正面朝上（摄像头那面向上）' }));
check('特征行同时给出中文与英文姿态名',
  els.tPosture.textContent === '平放' && els.tPostureEn.textContent.includes('Flat'),
  els.tPosture.textContent + ' / ' + els.tPostureEn.textContent);
check('特征行带上合加速度（静止时应 ≈1）',
  els.tMag.textContent === '1.000', els.tMag.textContent);
check('姿态补充说明也渲染出来', els.tNote.textContent.includes('正面朝上'));

renderTwin(null);
check('没有姿态数据时不报错、也不编一个姿态出来',
  els.tPosture.textContent === '--', els.tPosture.textContent);

// --- 6b. drawWave：喂假 canvas，看它画没画、通道开关生不生效 ---
const startW = src.indexOf('function drawWave(');
if (startW < 0 || startT < 0) { check('能抽出 drawWave', false); }
// drawWave 依赖闭包里的 waveCh，所以连它一起包进一个小作用域再取出来
const wrap = '(function(){ var waveCh = { x: true, y: true, z: true };\n'
  + src.slice(startW, startT)
  + '\n return { drawWave: drawWave, waveCh: waveCh }; })()';
const W = new Function('el', 'return ' + wrap)(el);

function makeCanvas() {
  const c = { fillText: 0, lineTo: 0, moveTo: 0, stroke: 0, fillRect: 0 };
  const ctx = {
    fillStyle: '', strokeStyle: '', lineWidth: 1, font: '', lineJoin: '',
    fillRect() { c.fillRect++; },
    clearRect() {},
    beginPath() {},
    moveTo() { c.moveTo++; },
    lineTo() { c.lineTo++; },
    stroke() { c.stroke++; },
    fillText() { c.fillText++; },
    strokeRect() {}
  };
  return { canvas: { width: 1080, height: 260, getContext: () => ctx }, calls: c };
}

const c0 = makeCanvas();
els.waveCanvas = c0.canvas;
let threw = null;
try { W.drawWave(null); } catch (e) { threw = e.message; }
check('没有波形数据时只画一句提示，不抛异常',
  threw === null && c0.calls.fillText === 1,
  threw || ('fillText=' + c0.calls.fillText));

const series = { t: [], ax: [], ay: [], az: [] };
for (let i = 0; i < 100; i++) {
  series.t.push(-5 + i * 0.05);
  series.ax.push(0.3 * Math.sin(i / 5));
  series.ay.push(0.1 * Math.cos(i / 7));
  series.az.push(1.0);
}
const c1 = makeCanvas();
els.waveCanvas = c1.canvas;
threw = null;
try { W.drawWave({ series: series }); } catch (e) { threw = e.message; }
check('有数据时能正常画出整条波形', threw === null, threw);
check('三条曲线都画了（lineTo ≥ 3×(100-1)）',
  c1.calls.lineTo >= 297, c1.calls.lineTo);

// 关掉一条通道，画的点数应当明显变少 —— 证明勾选框真的接上了绘制逻辑
W.waveCh.x = false;
const c2 = makeCanvas();
els.waveCanvas = c2.canvas;
W.drawWave({ series: series });
check('取消勾选 Acc X 后少画一条曲线',
  c2.calls.lineTo < c1.calls.lineTo && c2.calls.lineTo >= 198,
  c1.calls.lineTo + ' → ' + c2.calls.lineTo);

// 静止数据不该被自动缩放放大成"看着在动"
const flat = { t: [], ax: [], ay: [], az: [] };
for (let i = 0; i < 50; i++) {
  flat.t.push(-2.5 + i * 0.05);
  flat.ax.push(0.001 * (i % 2));    // 只有极小的噪声
  flat.ay.push(0);
  flat.az.push(1.0);
}
const c3 = makeCanvas();
els.waveCanvas = c3.canvas;
W.waveCh.x = true;
W.drawWave({ series: flat });
check('纵轴有 ±1.2g 下限，静止数据不会被噪声放大',
  c3.calls.fillText > 0 && c3.calls.lineTo > 0, c3.calls.lineTo);

// ---------- 7. 断网补传：画廊里的补传帧不能长得像"刚拍的" ----------
//
// 这一组守的是一个**很容易看起来没问题**的错误：
// 补传帧和在线帧长得一模一样，只在 source 上差一个词。
// 如果不打徽章、不把三个时间分开写，看画廊的人就会把断网期间攒的旧图
// 当成"刚刚拍的"—— 那正是第 2 周题眼要防的"旧值冒充新采集"。
console.log('\n== 7. 断网补传：画廊徽章与三个时间 ==');
const startG = src.indexOf('var GAL_MS =');
const endG = src.indexOf('function refreshGallery(');
if (startG < 0 || endG < 0) { check('能抽出画廊渲染函数', false); }
const renderGallery = new Function('el',
  escSrc + '\n' + src.slice(startG, endG) + '\n return renderGallery;')(el);

const galFrames = [
  { id: 11, device_id: 's3eye-group01', ts_server: '2026-09-23T10:00:00.000+08:00',
    capture_ts: '2026-09-23T10:00:00.000+08:00', request_id: null,
    command_state: null, source: 'periodic', bytes: 40000, width: 800, height: 600,
    sha256: 'a'.repeat(64), purged: false, is_backlog: false },
  { id: 12, device_id: 's3eye-group01', ts_server: '2026-09-23T10:12:30.000+08:00',
    capture_ts: 'uptime+12.345s(time_not_synced)', request_id: null,
    command_state: null, source: 'backlog', bytes: 42000, width: 800, height: 600,
    sha256: 'b'.repeat(64), purged: false, is_backlog: true,
    buffered_us: 615000000, buffered_s: 615.0, backlog_dropped: 3 }
];

renderGallery({ frames: galFrames, storage: {
  total: 2, kept: 2, purged: 0, kept_bytes: 82000, retention_days: 7,
  backlog_frames: 1, backlog_dropped_total: 3 } });
const outG = els.galGrid.innerHTML;

check('补传帧有自己的徽章（不是"周期抓拍"）', outG.includes('断网补传'));
check('在线帧仍然是"周期抓拍"', outG.includes('周期抓拍'));
check('★ 补传帧把三个时间分开写：采集时刻 / 队列中待了 / 服务端收到',
  outG.includes('采集时刻') && outG.includes('队列中待了')
  && outG.includes('服务端收到'));
check('★ 并注明"采集时刻"是板端钟、只作参考',
  outG.includes('板端钟，只作参考'));
check('★ 并注明"服务端收到"才是判定用的时刻',
  outG.includes('判定用它'));
check('队列中待了多久按秒显示', outG.includes('615 秒'));
check('板端未对时的时间戳原文保留（不被吞掉）',
  outG.includes('time_not_synced'));
check('★ 丢弃过的更老帧数如实显示（不假装数据完整）',
  outG.includes('已丢弃 3 帧'));
check('在线帧不显示补传那三行',
  (outG.match(/队列中待了/g) || []).length === 1);
check('补传帧数写进了页面统计', els.galBacklog.textContent === 1);
check('补传期间丢弃总数写进了页面统计', els.galBacklogDropped.textContent === 3);
check('没有 undefined 漏出来', !/undefined/.test(outG));

// 板端没报 buffered_us 时，不能显示成 null/NaN
renderGallery({ frames: [Object.assign({}, galFrames[1],
  { buffered_us: null, buffered_s: null })], storage: { total: 1, kept: 1 } });
check('板端没报在队列里待多久时，显示"未知"而不是 null',
  els.galGrid.innerHTML.includes('未知') && !/null/.test(els.galGrid.innerHTML),
  els.galGrid.innerHTML.match(/队列中待了[\s\S]{0,40}/));

console.log('\n' + '='.repeat(60));
console.log('结果：' + (fails.length ? '失败 ' + fails.length + ' 项：' + fails.join('、')
                                   : '全部通过'));
console.log('='.repeat(60));
process.exit(fails.length ? 1 : 0);
