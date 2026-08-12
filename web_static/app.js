let projectId = null;
let projects = [];
let poller = null;
let renderedMessageKey = "";
let viewEpoch = 0;
let refreshing = false;
let sending = false;
let selectedCandidateIds = new Set();

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

async function api(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const contentType = response.headers.get("content-type") || "";
  const value = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof value === "object" ? value.detail : value;
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  return value;
}

function showToast(message, kind = "info") {
  const toast = document.createElement("div");
  toast.className = `toast ${kind === "error" ? "error" : ""}`;
  toast.textContent = message;
  $("toastRegion").append(toast);
  window.setTimeout(() => toast.remove(), 4200);
}

function relativeTime(raw) {
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)} 天前`;
  return date.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

function inlineMarkup(value) {
  return value
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function renderProse(value) {
  return esc(value).split("\n").map((line) => {
    const text = line.trim();
    if (!text) return "";
    if (text.startsWith("### ")) return `<h4>${inlineMarkup(text.slice(4))}</h4>`;
    if (text.startsWith("## ")) return `<h3>${inlineMarkup(text.slice(3))}</h3>`;
    if (text.startsWith("# ")) return `<h2>${inlineMarkup(text.slice(2))}</h2>`;
    if (/^[-*] /.test(text)) return `<div class="message-list-item">${inlineMarkup(text.slice(2))}</div>`;
    if (/^\d+\. /.test(text)) return `<div class="message-list-item">${inlineMarkup(text)}</div>`;
    return `<p>${inlineMarkup(text)}</p>`;
  }).join("");
}

function richText(value) {
  return String(value ?? "").split("```").map((chunk, index) => (
    index % 2 ? `<pre>${esc(chunk.replace(/^\w+\n/, ""))}</pre>` : renderProse(chunk)
  )).join("");
}

function messageNode(message, transient = false) {
  const role = message.role === "user" ? "user" : "assistant";
  const node = document.createElement("article");
  node.className = `message ${role}${transient ? " transient" : ""}`;
  node.dataset.messageId = message.id || "transient";
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.textContent = role === "user" ? "你" : "CN";
  const content = document.createElement("div");
  content.className = "message-content";
  content.innerHTML = role === "assistant" ? richText(message.content) : `<p>${esc(message.content)}</p>`;
  node.append(avatar, content);
  return node;
}

function typingNode() {
  const node = messageNode({ role: "assistant", content: "" }, true);
  node.id = "typingMessage";
  node.querySelector(".message-content").innerHTML = '<div class="typing" aria-label="CellNote 正在思考"><i></i><i></i><i></i></div>';
  return node;
}

function welcomeMarkup() {
  return `<div class="welcome">
    <div class="welcome-orbit" aria-hidden="true"></div>
    <h2>从数据问题开始</h2>
    <p>搜索公开 scATAC / Multiome 数据，或分析服务器上的现有文件。所有下载与 QC 操作都会先经过你的确认。</p>
    <div class="starter-grid">
      <button class="starter-card" type="button" data-prompt="搜索人类 PBMC scATAC 公开数据集"><b>搜索公开数据</b><span>从 GEO、SRA 与文献证据中整理候选</span></button>
      <button class="starter-card" type="button" data-prompt="搜索人类 10x Multiome 公开数据集，优先 GRCh38"><b>寻找 Multiome</b><span>优先配对完整、可进入 QC 的数据</span></button>
      <button class="starter-card" type="button" data-prompt="我有服务器上的数据，请引导我完成输入检测和 QC"><b>分析已有数据</b><span>识别 fragments、peak matrix 或 multiome</span></button>
    </div>
  </div>`;
}

function bindPromptButtons(root = document) {
  root.querySelectorAll("[data-prompt]").forEach((button) => {
    button.onclick = () => {
      $("prompt").value = button.dataset.prompt || "";
      resizeComposer();
      $("prompt").focus();
    };
  });
}

function renderMessages(messages, gates = []) {
  const key = `${messages.map((message) => message.id).join(",")}|${gates.map((gate) => `${gate.id}:${gate.status}`).join(",")}`;
  if (key === renderedMessageKey) return;
  renderedMessageKey = key;
  const conversation = $("conversation");
  conversation.innerHTML = "";
  const hasUserMessage = messages.some((message) => message.role === "user");
  if (!hasUserMessage) {
    conversation.innerHTML = welcomeMarkup();
    bindPromptButtons(conversation);
  } else {
    messages.forEach((message) => conversation.append(messageNode(message)));
  }
  const activeGate = gates[0];
  if (activeGate) {
    const host = document.createElement("div");
    host.className = "interaction-host";
    host.append(gateCard(activeGate));
    conversation.append(host);
  }
  requestAnimationFrame(() => { conversation.scrollTop = conversation.scrollHeight; });
}

function showOptimisticMessage(prompt) {
  const conversation = $("conversation");
  if (conversation.querySelector(".welcome")) conversation.innerHTML = "";
  conversation.append(messageNode({ role: "user", content: prompt }, true), typingNode());
  conversation.scrollTop = conversation.scrollHeight;
}

function gateShell(title, description = "") {
  const card = document.createElement("section");
  card.className = "gate-card";
  card.innerHTML = `<div class="gate-kicker">需要你的确认</div><h4>${esc(title)}</h4>${description ? `<p>${esc(description)}</p>` : ""}`;
  return card;
}

function actionButton(label, className = "button-primary", attributes = "") {
  return `<button type="button" class="${className}" ${attributes}>${esc(label)}</button>`;
}

async function runCardAction(card, task) {
  const controls = [...card.querySelectorAll("button, input, textarea, select")];
  controls.forEach((control) => { control.disabled = true; });
  try {
    await task();
  } catch (error) {
    controls.forEach((control) => { control.disabled = false; });
    showToast(error.message, "error");
  }
}

async function postGate(gate, payload) {
  const result = await api(`/api/projects/${projectId}/gates/${gate.id}`, {
    method: "POST",
    body: JSON.stringify({ payload }),
  });
  renderedMessageKey = "";
  await refresh();
  return result;
}

function searchGate(gate) {
  const preferences = gate.payload.preferences || {};
  const spec = gate.payload.search_spec || preferences.search_spec || {};
  const explicit = preferences.explicit_slots || {};
  const unknown = new Set(spec.unknown_fields || preferences.unknown_fields || []);
  const entityLabels = { species: "物种", disease: "疾病", tissue: "组织", cell_type: "细胞类型", modality: "模态", genome_build: "基因组" };
  const entityRows = Object.entries(entityLabels).map(([key, label]) => {
    const entity = spec[key] || {};
    if (!entity.normalized && !unknown.has(key)) return "";
    const confidence = Math.round(Number(entity.confidence || 0) * 100);
    const status = unknown.has(key) ? "待确认" : entity.inferred ? "模型推断" : "已识别";
    return `<div class="semantic-row ${unknown.has(key) ? "uncertain" : ""}"><span>${esc(label)}</span><strong>${esc(entity.raw || entity.normalized || "未识别")}</strong>${entity.normalized && entity.raw !== entity.normalized ? `<small>${esc(entity.normalized)}</small>` : ""}<em>${esc(status)}${confidence ? ` · ${confidence}%` : ""}</em></div>`;
  }).join("");
  const card = gateShell("确认搜索条件", "Agent 已提取首句中明确的要求。下方只显示仍需确认的检索条件；也可以直接在聊天框继续补充或修改。确认前不会启动 crawler。");
  const radio = (name, title, values, selected) => `<fieldset><legend>${esc(title)}</legend><div class="option-grid">${values.map((value) => `<label class="option"><input type="radio" name="${esc(name)}" value="${esc(value)}" ${value === selected ? "checked" : ""}> <span>${esc(value)}</span></label>`).join("")}</div></fieldset>`;
  const onlyIfMissing = (slot, html) => explicit[slot] ? "" : html;
  const acquisitionValues = ["处理后的矩阵或 fragments", "原始 FASTQ/SRA", "都行/越方便越好", "其他"];
  const acquisitionDefault = acquisitionValues.includes(preferences.acquisition) ? preferences.acquisition : "都行/越方便越好";
  const speciesValues = ["Homo sapiens", "Mus musculus", "Rattus norvegicus", "其他"];
  const speciesDefault = speciesValues.includes(preferences.species) ? preferences.species : "其他";
  const detailDisplay = preferences.tissue_hint_display || (spec.disease?.raw || spec.tissue?.raw || preferences.tissue_hint || "");
  const detailNormalized = preferences.tissue_hint || spec.disease?.normalized || spec.tissue?.normalized || "";
  const chosenEntity = spec.disease?.normalized ? spec.disease : (spec.tissue?.normalized ? spec.tissue : spec.cell_type || {});
  const aliasValues = preferences.biology_aliases || chosenEntity.aliases || [];
  const aliasText = Array.isArray(aliasValues) ? aliasValues.join(", ") : String(aliasValues || "");
  const body = document.createElement("div");
  body.innerHTML = `<section class="semantic-summary"><div class="semantic-heading"><strong>Agent 的结构化理解</strong><span>${esc(spec.extraction_mode === "model_structured" ? "大模型抽取 + 本地校验" : "确定性 fallback")}</span></div>${entityRows || '<p class="semantic-empty">尚未识别出足够的生物学实体，请在下方补充。</p>'}${aliasText ? `<div class="semantic-aliases"><span>用于扩展检索</span><strong>${esc(aliasText)}</strong><small>这些词只扩大召回，不作为已验证事实，也不生成候选评分。</small></div>` : ""}</section>
    ${onlyIfMissing("species", `${radio("species", "物种", speciesValues, speciesDefault)}<label id="speciesOtherLabel" hidden>其他物种（学名）<input id="speciesOther" type="text" value="${speciesDefault === "其他" ? esc(preferences.species || "") : ""}" placeholder="例如：Macaca mulatta"></label>`)}
    ${onlyIfMissing("data_type", radio("dataType", "数据类型", ["纯 scATAC-seq", "10x Multiome", "两者都要"], preferences.data_type || "纯 scATAC-seq"))}
    ${onlyIfMissing("tissue_or_disease", `${radio("tissue", "组织 / 疾病方向", ["泛癌/肿瘤", "正常组织图谱", "特定疾病", "特定组织", "不限/广泛搜集"], preferences.tissue_or_disease || "不限/广泛搜集")}<div id="biologyDetailGroup" hidden><label>用户原始表述<input id="diseaseDetail" type="text" value="${esc(detailDisplay)}" placeholder="例如：溃疡性结肠炎、肝脏"></label><label>标准化检索词<input id="normalizedDetail" type="text" value="${esc(detailNormalized)}" placeholder="例如：ulcerative colitis、liver"></label><label>检索同义词（逗号分隔，可修改）<input id="biologyAliases" type="text" value="${esc(aliasText)}" placeholder="例如：scleroderma, systemic scleroderma, SSc"><small>仅用于扩大公开数据库召回。</small></label></div>`)}
    ${onlyIfMissing("acquisition", `${radio("acquisition", "希望获取的形式", acquisitionValues, acquisitionDefault)}<label id="acquisitionOtherLabel" hidden>自定义获取要求<input id="acquisitionOther" type="text" placeholder="例如：只要带细胞注释的 h5ad"></label>`)}
    ${onlyIfMissing("candidate_limit", `${radio("limit", "候选数据集展示数量上限（软限制）", ["5", "10", "30", "其他"], ["5", "10", "30"].includes(String(preferences.candidate_limit)) ? String(preferences.candidate_limit) : "其他")}<small>只控制结果页最多整理和展示多少个候选，不代表数据库中的真实总数，也不缩小基础检索范围。</small><label id="customLimitLabel" hidden>自定义展示要求<input id="customLimit" type="text" value="${![5, 10, 30].includes(Number(preferences.candidate_limit)) ? esc(String(preferences.candidate_limit_request || preferences.candidate_limit || "")) : ""}" placeholder="例如：50、全部、尽量多"></label>`)}
    ${onlyIfMissing("size_limit", radio("size", "单个候选预计下载体积上限", ["5GB以内", "20GB以内", "100GB以内", "不限制"], ["5GB以内", "20GB以内", "100GB以内", "不限制"].includes(preferences.size_limit) ? preferences.size_limit : "20GB以内"))}
    ${onlyIfMissing("target_genome_build", radio("genome", "目标基因组版本", ["GRCh38", "hg19/GRCh37", "不确定，先检测/询问"], ["GRCh38", "hg19/GRCh37", "不确定，先检测/询问"].includes(preferences.target_genome_build) ? preferences.target_genome_build : "不确定，先检测/询问"))}
    <label>补充检索要求（可选）<input id="searchNote" type="text" placeholder="例如：优先 10x、只要带细胞注释的 h5ad"></label>
    <div class="gate-actions">${actionButton("确认并开始检索")}${actionButton("取消", "button-secondary")}</div>`;
  card.append(body);
  const choice = (name, fallback) => card.querySelector(`input[name="${name}"]:checked`)?.value || fallback;
  const toggle = () => {
    const states = [
      ["customLimitLabel", choice("limit", "") === "其他"],
      ["biologyDetailGroup", ["特定疾病", "特定组织"].includes(choice("tissue", ""))],
      ["speciesOtherLabel", choice("species", "") === "其他"],
      ["acquisitionOtherLabel", choice("acquisition", "") === "其他"],
    ];
    states.forEach(([id, visible]) => { const node = card.querySelector(`#${id}`); if (node) node.hidden = !visible; });
  };
  ["limit", "tissue", "species", "acquisition"].forEach((name) => card.querySelectorAll(`input[name="${name}"]`).forEach((node) => { node.onchange = toggle; }));
  toggle();
  const buttons = card.querySelectorAll(".gate-actions button");
  buttons[0].onclick = () => runCardAction(card, async () => {
    const limitChoice = choice("limit", String(preferences.candidate_limit || "其他"));
    const limitRequest = card.querySelector('input[name="limit"]') ? (limitChoice === "其他" ? card.querySelector("#customLimit")?.value.trim() : limitChoice) : String(preferences.candidate_limit_request || preferences.candidate_limit || 10);
    if (limitChoice === "其他" && !limitRequest) throw new Error("请填写候选展示数量");
    const tissue = choice("tissue", preferences.tissue_or_disease);
    const acquisition = choice("acquisition", preferences.acquisition || "都行/越方便越好");
    const acquisitionOther = acquisition === "其他" ? card.querySelector("#acquisitionOther")?.value.trim() : "";
    if (acquisition === "其他" && !acquisitionOther) throw new Error("请填写自定义获取要求");
    const speciesChoice = choice("species", preferences.species || "Homo sapiens");
    const species = speciesChoice === "其他" ? card.querySelector("#speciesOther")?.value.trim() : speciesChoice;
    if (!species) throw new Error("请填写物种学名");
    const requiresDetail = ["特定疾病", "特定组织"].includes(tissue);
    const rawDetail = requiresDetail ? (card.querySelector("#diseaseDetail")?.value.trim() || preferences.tissue_hint_display || preferences.tissue_hint || "") : "";
    const normalizedDetail = requiresDetail ? (card.querySelector("#normalizedDetail")?.value.trim() || preferences.tissue_hint || rawDetail) : "";
    const biologyAliases = requiresDetail ? (card.querySelector("#biologyAliases")?.value || aliasText).split(/[,;，；\n]+/).map((item) => item.trim()).filter(Boolean).slice(0, 8) : [];
    if (requiresDetail && (!rawDetail || !normalizedDetail)) throw new Error("请确认生物学实体和标准化检索词");
    const extras = [
      card.querySelector("#searchNote")?.value.trim(),
    ].filter(Boolean);
    const updated = {
      ...preferences,
      species,
      data_type: choice("dataType", preferences.data_type),
      tissue_or_disease: tissue,
      tissue_hint: normalizedDetail,
      tissue_hint_display: rawDetail,
      biology_aliases: biologyAliases,
      acquisition,
      candidate_limit: limitChoice === "其他" ? null : Number(limitChoice),
      candidate_limit_request: limitRequest,
      size_limit: choice("size", preferences.size_limit),
      target_genome_build: choice("genome", preferences.target_genome_build),
      prefer_analysis_ready: acquisition.includes("矩阵") || acquisition.includes("fragments"),
      user_note: [...extras, acquisitionOther].filter(Boolean).join(" "),
      explicit_slots: { ...explicit, species, data_type: choice("dataType", preferences.data_type), tissue_or_disease: tissue, acquisition, candidate_limit: limitRequest, size_limit: choice("size", preferences.size_limit), target_genome_build: choice("genome", preferences.target_genome_build) },
    };
    await postGate(gate, { preferences: updated });
    activateDetailTab("tasksPanel");
    openDetails();
  });
  buttons[1].onclick = () => runCardAction(card, () => postGate(gate, { cancelled: true }));
  return card;
}

function candidateTriageGate(gate) {
  const data = gate.payload || {};
  const card = gateShell("搜索结果已整理", `当前保留 ${data.candidate_count || 0} 条候选记录。请根据来源、模态、文件角色、文件数量和体积等客观信息审阅。`);
  const actions = [
    ["manual", "在右侧查看候选", "button-primary", false],
    ["stop", "暂不继续", "button-quiet", false],
  ];
  card.insertAdjacentHTML("beforeend", `<div class="gate-actions">${actions.map(([choice, label, style, disabled]) => actionButton(label, style, `data-choice="${choice}" ${disabled ? "disabled" : ""}`)).join("")}</div>`);
  card.querySelectorAll("button").forEach((button) => {
    button.onclick = () => runCardAction(card, async () => {
      await postGate(gate, { choice: button.dataset.choice });
      if (button.dataset.choice === "manual") {
        activateDetailTab("dataPanel");
        openDetails();
      }
    });
  });
  return card;
}

function manifestReviewGate(gate) {
  const data = gate.payload || {};
  const rows = data.rows || [];
  const card = gateShell("下载清单已生成", `${rows.length} 个文件 / ${Number(data.total_size_gb || 0).toFixed(3)} GB。当前仅展示计划，不会自动下载。`);
  card.insertAdjacentHTML("beforeend", `<details class="manifest-details" open><summary>查看下载文件</summary><div class="manifest-list">${rows.slice(0, 50).map((row, offset) => {
      const size = Number(row.size_bytes || 0);
      return `<article class="manifest-plan-row"><div><strong>[${offset + 1}] ${esc(row.dataset_id || "-")}</strong><span>${esc(row.artifact_id || row.source_uri || "-")}</span></div><b>${esc(row.role || "unknown")} · ${size ? formatBytes(size) : "大小未知"}</b><p>${esc(row.source_uri || "缺少下载地址")}</p></article>`;
    }).join("")}${rows.length > 50 ? `<small>其余 ${rows.length - 50} 个条目已省略</small>` : ""}</div></details>
    <label>修改清单（可选）<textarea rows="2" placeholder="例如：只保留最小 1 个文件、控制在 5GB 内、删除 1,2"></textarea></label>
    <div class="gate-actions">
      ${actionButton("进入最终下载确认", "button-primary", 'data-choice="download"')}
      ${actionButton("提交修改", "button-secondary", 'data-choice="other"')}
      ${actionButton("暂不下载", "button-quiet", 'data-choice="cancel"')}
    </div>`);
  const instruction = card.querySelector("textarea");
  card.querySelectorAll("button").forEach((button) => {
    button.onclick = () => runCardAction(card, async () => {
      const choice = button.dataset.choice;
      if (choice === "other" && !instruction.value.trim()) throw new Error("请先填写清单修改要求");
      await postGate(gate, { choice, instruction: instruction.value.trim() });
    });
  });
  return card;
}

function analysisGate(gate) {
  const context = gate.payload.context || {};
  const isPeak = context.input_kind === "peak_matrix";
  const isLarge = context.size_risk === "large";
  const card = gateShell("输入识别与 QC 参数", `${context.input_kind || "未知类型"} · ${context.reason || "已完成输入检测"}`);
  const qcControls = isPeak
    ? `<label>QC 阈值档位<select id="tier"><option value="loose">宽松（500 / 500 / 5）</option><option value="standard" selected>标准（1000 / 1000 / 10）</option><option value="strict">严格（2000 / 2000 / 20）</option></select></label>${isLarge ? '<label class="option"><input id="skipEmbed" type="checkbox" checked><span>过滤后跳过 embed-cluster（大矩阵推荐）</span></label>' : ""}`
    : `<label>最小 fragments 数<select id="minFragments"><option value="1000">1000（宽松）</option><option value="3000" selected>3000（标准）</option><option value="5000">5000（严格）</option></select></label><label>最小 TSS enrichment<select id="minTsse"><option value="4">4.0（宽松）</option><option value="6" selected>6.0（标准）</option><option value="8">8.0（严格）</option></select></label>`;
  card.insertAdjacentHTML("beforeend", `<small>检测基因组：${esc(context.genome_hint || context.genome_build || "未知")}</small>
    <label>分析模式<select id="analysisMode"><option value="full_qc">完整 QC（推荐）</option><option value="packaging_only">仅 packaging（不做阈值过滤）</option></select></label>
    <label>参考基因组<select id="genome"><option value="GRCh38">GRCh38 / hg38（推荐）</option><option value="GRCh37">GRCh37 / hg19</option><option value="mm10">mm10</option></select></label>
    ${qcControls}
    <div class="gate-actions">${actionButton("生成受控 QC 计划")}${actionButton("取消分析", "button-secondary")}</div>`);
  const mode = card.querySelector("#analysisMode");
  if (!isPeak) { mode.value = "full_qc"; mode.disabled = true; }
  const buttons = card.querySelectorAll(".gate-actions button");
  buttons[0].onclick = () => runCardAction(card, async () => {
    const qcParams = { genome_build: card.querySelector("#genome").value };
    if (isPeak) {
      const presets = { loose: [500, 500, 5], standard: [1000, 1000, 10], strict: [2000, 2000, 20] };
      const values = presets[card.querySelector("#tier").value];
      Object.assign(qcParams, { min_peaks: values[0], min_counts: values[1], min_cells_per_peak: values[2] });
      if (isLarge) qcParams.skip_embed_cluster = card.querySelector("#skipEmbed").checked;
    } else {
      Object.assign(qcParams, { min_fragments: Number(card.querySelector("#minFragments").value), min_tsse: Number(card.querySelector("#minTsse").value) });
    }
    await postGate(gate, { analysis_mode: mode.value, qc_params: qcParams });
  });
  buttons[1].onclick = () => runCardAction(card, () => postGate(gate, { cancelled: true }));
  return card;
}

function simpleConfirmGate(gate, { title, description, confirmLabel = "确认", cancelLabel = "取消" }) {
  const card = gateShell(title, description);
  card.insertAdjacentHTML("beforeend", `<div class="gate-actions">${actionButton(confirmLabel)}${actionButton(cancelLabel, "button-secondary")}</div>`);
  const buttons = card.querySelectorAll("button");
  buttons[0].onclick = () => runCardAction(card, () => postGate(gate, { confirm: true }));
  buttons[1].onclick = () => runCardAction(card, () => postGate(gate, { confirm: false }));
  return card;
}

function gateCard(gate) {
  if (gate.kind === "search") return searchGate(gate);
  if (gate.kind === "candidate_triage") return candidateTriageGate(gate);
  if (gate.kind === "manifest_review") return manifestReviewGate(gate);
  if (gate.kind === "analysis") return analysisGate(gate);
  if (gate.kind === "download") {
    const plan = gate.payload.acquisition_plan || {};
    const detail = `${Number(plan.file_count || (gate.payload.rows || []).length)} 个文件，约 ${Number(plan.total_size_gb || 0).toFixed(3)} GB；当前清单的源端校验和覆盖 ${Math.round(Number(plan.manifest_checksum_coverage || 0) * 100)}%。`;
    return simpleConfirmGate(gate, { title: "最终确认下载", description: `${detail} 系统将依次执行 plan → fetch → verify，并把文件保存在当前任务的隔离目录中。`, confirmLabel: "确认开始下载" });
  }
  if (gate.kind === "analysis_execute") return simpleConfirmGate(gate, { title: "确认执行 QC", description: "将启动上方已经解释的白名单 QC stages；长任务由服务器托管，可在运行详情中暂停。", confirmLabel: "确认执行 QC" });
  if (gate.kind === "paused_task") {
    const label = gate.payload.kind === "download" ? "下载" : "QC";
    const card = gateShell(`${label}已暂停`, `任务停在约 ${gate.payload.progress || 0}%，日志与已有结果均已保留。`);
    card.insertAdjacentHTML("beforeend", `<div class="gate-actions">${actionButton(label === "下载" ? "继续下载" : "重新运行 QC")}</div>`);
    card.querySelector("button").onclick = () => runCardAction(card, async () => {
      await api(`/api/projects/${projectId}/jobs/${gate.payload.job_id}/resume`, { method: "POST" });
      renderedMessageKey = "";
      await refresh();
    });
    return card;
  }
  const messages = {
    download_complete: ["下载与校验完成", gate.payload.message || "文件已通过完整性校验，可以进入输入检测与 QC。"],
    cancelled_task: ["任务已中断", "已有记录和结果已保留，可以重新描述需求或继续查看现有内容。"],
    qc_complete: ["QC 已完成", gate.payload.message || "标准化结果已生成，可在文件面板下载。"],
  };
  const [title, description] = messages[gate.kind] || ["下一步", "请继续在对话中说明你的要求。"];
  return gateShell(title, description);
}

function renderProjectList() {
  const query = $("projectSearch").value.trim().toLowerCase();
  const visible = projects.filter((project) => project.name.toLowerCase().includes(query));
  $("projectCount").textContent = String(projects.length || "");
  $("projects").innerHTML = visible.map((project) => `<div class="project ${project.id === projectId ? "active" : ""}" data-id="${esc(project.id)}">
    <i class="project-bullet"></i>
    <div class="project-info"><div class="project-name">${esc(project.name)}</div><div class="project-time">${esc(relativeTime(project.updated_at))}</div></div>
    <button type="button" class="project-delete" aria-label="删除任务 ${esc(project.name)}" title="删除任务">×</button>
  </div>`).join("") || `<div class="sidebar-empty">${query ? "没有匹配的任务" : "还没有任务"}</div>`;
  $("projects").querySelectorAll(".project").forEach((node) => {
    node.onclick = (event) => { if (!event.target.closest(".project-delete")) openProject(node.dataset.id); };
  });
  $("projects").querySelectorAll(".project-delete").forEach((button) => {
    button.onclick = async (event) => {
      event.stopPropagation();
      const node = button.closest(".project");
      const project = projects.find((item) => item.id === node.dataset.id);
      if (!window.confirm(`删除“${project?.name || "这个任务"}”及其下载、分析和日志数据？此操作不可恢复。`)) return;
      try {
        await api(`/api/projects/${node.dataset.id}`, { method: "DELETE" });
        if (projectId === node.dataset.id) projectId = null;
        await loadProjects(true);
        showToast("任务已删除");
      } catch (error) { showToast(error.message, "error"); }
    };
  });
}

async function loadProjects(autoOpen = false) {
  projects = await api("/api/projects");
  if (projectId && !projects.some((project) => project.id === projectId)) projectId = null;
  renderProjectList();
  if (autoOpen && !projectId && projects[0]) await openProject(projects[0].id, false);
  if (!projectId && !projects.length) showNoProject();
}

function showNoProject() {
  clearInterval(poller);
  projectId = null;
  $("projectTitle").textContent = "开始一个新任务";
  $("projectState").className = "state-pill";
  $("projectState").innerHTML = "<i></i>就绪";
  $("conversation").innerHTML = welcomeMarkup();
  bindPromptButtons($("conversation"));
  updateStageRail(0);
  clearDetails();
}

async function openProject(id, reloadProjects = true) {
  if (id === projectId && poller) return;
  clearInterval(poller);
  projectId = id;
  viewEpoch += 1;
  selectedCandidateIds = new Set();
  renderedMessageKey = "";
  clearDetails();
  if (reloadProjects) renderProjectList();
  closeSidebar();
  await refresh();
  poller = window.setInterval(() => { if (!document.hidden) refresh(true); }, 2500);
}

async function newProject() {
  $("newProject").disabled = true;
  try {
    const project = await api("/api/projects", { method: "POST", body: JSON.stringify({}) });
    await loadProjects(false);
    await openProject(project.id, false);
    $("prompt").focus();
  } catch (error) { showToast(error.message, "error"); }
  finally { $("newProject").disabled = false; }
}

function clearDetails() {
  $("jobs").className = "empty-block";
  $("jobs").textContent = "暂无任务";
  $("candidates").className = "empty-block";
  $("candidates").textContent = "搜索完成后显示";
  $("artifacts").className = "empty-block";
  $("artifacts").textContent = "暂无产物";
  $("activeJobCount").textContent = "";
  $("candidateCount").textContent = "";
  $("artifactCount").textContent = "";
}

const jobLabels = { crawl: "公开数据搜索", manifest: "生成下载清单", download: "下载与校验", qc: "质量控制" };
const jobIcons = { crawl: "搜", manifest: "单", download: "下", qc: "QC" };
const statusLabels = { queued: "排队中", running: "运行中", submitted: "后台运行", completed: "已完成", failed: "失败", paused: "已暂停", cancelled: "已取消" };

function jobProgress(job) {
  return Math.max(0, Math.min(100, Number(job.progress ?? job.detail?.progress ?? 0)));
}

function taskControl(job) {
  if (["queued", "running", "submitted"].includes(job.status)) return `<button type="button" class="job-action" data-job-id="${esc(job.id)}" data-action="pause">${job.kind === "download" || job.kind === "qc" ? "暂停任务" : "停止任务"}</button>`;
  if (job.status === "paused" && ["download", "qc"].includes(job.kind)) return `<button type="button" class="job-action" data-job-id="${esc(job.id)}" data-action="resume">${job.kind === "download" ? "继续下载" : "重新运行 QC"}</button>`;
  return "";
}

function jobCard(job, compact = false) {
  const progress = jobProgress(job);
  const active = ["queued", "running", "submitted", "paused"].includes(job.status);
  const failed = job.status === "failed";
  const stage = job.stage || job.detail?.stage || (active ? "等待服务器更新" : "任务结束");
  return `<article class="job-card ${compact ? "compact" : ""}">
    <div class="job-head">
      <div class="job-title"><span class="job-kind">${esc(jobIcons[job.kind] || "任务")}</span>${esc(jobLabels[job.kind] || job.kind)}</div>
      <span class="status-badge ${active ? "working" : ""} ${failed ? "failed" : ""}">${esc(statusLabels[job.status] || job.status)}</span>
    </div>
    <div class="progress"><span style="width:${progress}%"></span></div>
    <div class="job-meta"><span>${esc(stage)}</span><strong>${progress}%</strong></div>
    ${taskControl(job)}
    ${job.log_tail ? `<details class="job-log"><summary>查看最近日志</summary><pre>${esc(job.log_tail)}</pre></details>` : ""}
  </article>`;
}

function renderJobs(jobs = []) {
  const active = jobs.filter((job) => ["queued", "running", "submitted", "paused"].includes(job.status));
  const current = active[0] || jobs[0];
  const history = jobs.filter((job) => job !== current);
  $("activeJobCount").textContent = active.length ? `${active.length} 个进行中` : jobs.length ? `${jobs.length} 条记录` : "";
  if (!current) {
    $("jobs").className = "empty-block";
    $("jobs").textContent = "发送请求后，任务进度会显示在这里";
    return;
  }
  $("jobs").className = "";
  $("jobs").innerHTML = `${jobCard(current)}${history.length ? `<details><summary class="history-summary">历史任务 · ${history.length}</summary>${history.map((job) => jobCard(job, true)).join("")}</details>` : ""}`;
  $("jobs").querySelectorAll(".job-action").forEach((button) => {
    button.onclick = async () => {
      const action = button.dataset.action;
      if (action === "pause" && !window.confirm("停止当前任务？已生成的结果和日志会保留。")) return;
      button.disabled = true;
      try {
        await api(`/api/projects/${projectId}/jobs/${button.dataset.jobId}/${action}`, { method: "POST" });
        renderedMessageKey = "";
        await refresh();
      } catch (error) { button.disabled = false; showToast(error.message, "error"); }
    };
  });
}

function renderCandidates(payload = {}) {
  const rows = payload.rows || [];
  const availableIds = new Set(rows.map((row) => String(row.candidate_id)));
  selectedCandidateIds = new Set([...selectedCandidateIds].filter((id) => availableIds.has(id)));
  $("candidateCount").textContent = payload.total ? `显示 ${rows.length} / 共 ${payload.total}` : "";
  if (!rows.length) {
    $("candidates").className = "empty-block";
    $("candidates").textContent = "搜索完成后显示候选数据集";
    return;
  }
  $("candidates").className = "";
  $("candidates").innerHTML = `<div class="candidate-toolbar"><p>候选按“是否有可下载文件、文件角色、体积”排序，不计算综合分。</p><p id="selectionText">选择候选后生成下载清单</p><button id="createManifest" class="manifest-button" type="button" disabled>生成下载清单</button></div>
    ${rows.map((row) => {
      const id = String(row.candidate_id || "");
      const downloadable = row.metadata_only !== "yes" && Number(row.file_count || 0) > 0 && row.best_file_role !== "metadata_only";
      if (!downloadable) selectedCandidateIds.delete(id);
      const selected = downloadable && selectedCandidateIds.has(id);
      const sizeText = downloadable && Number(row.total_size_bytes || 0) > 0 ? `${esc(row.total_size_gb || "-")} GB` : "大小未知";
      const studySize = Number(row.study_total_size_bytes || 0) > 0 ? `${esc(row.study_total_size_gb || "-")} GB` : "未知";
      const evidenceLabels = { confirmed: "信息已确认", partial: "部分信息已确认", unknown: "信息不足", mismatch: "存在明确不匹配" };
      const reasons = Array.isArray(row.match_reasons) ? row.match_reasons : [];
      const uncertainties = Array.isArray(row.uncertainty_flags) ? row.uncertainty_flags : [];
      const title = row.title && row.title !== row.study_accession ? row.title : "";
      return `<label class="candidate-card ${selected ? "selected" : ""} ${downloadable ? "" : "unavailable"}" data-id="${esc(id)}">
        <input type="checkbox" ${selected ? "checked" : ""} ${downloadable ? "" : "disabled"} aria-label="${downloadable ? "选择候选" : "仅元数据，暂不可下载"} ${esc(row.study_accession || row.dataset_id || id)}">
        <span><span class="candidate-title"><span><span class="candidate-id">#${esc(id || "-")}</span> ${esc(row.study_accession || row.dataset_id || "未命名数据集")}</span><span>${sizeText}</span></span>
        ${title ? `<span class="candidate-study-title">${esc(title)}</span>` : ""}
        <span class="candidate-tags"><span>${esc(row.repository_source || "未知来源")}</span><span>${esc(row.inferred_modality || "未知模态")}</span><span>${esc(row.best_file_role || "待识别")}</span><span>${esc(evidenceLabels[row.evidence_status] || "待核实")}</span>${downloadable ? "" : '<span class="unavailable-tag">仅元数据</span>'}</span>
        <span class="candidate-size">优先文件 ${esc(String(row.preferred_file_count || row.file_count || 0))} 个 / 研究全部文件 ${esc(String(row.file_count || 0))} 个 · 研究总体积 ${studySize} · 基因组 ${esc(row.genome_build || "未知")}</span>
        ${reasons.length ? `<span class="candidate-why"><b>元数据说明</b>${esc(reasons.slice(0, 3).join(" · "))}</span>` : ""}
        ${uncertainties.length ? `<span class="candidate-warning"><b>待核实</b>${esc(uncertainties.slice(0, 2).join(" · "))}</span>` : ""}
        </span>
      </label>`;
    }).join("")}`;
  const updateSelection = () => {
    const count = selectedCandidateIds.size;
    $("selectionText").textContent = count ? `已选择 ${count} 个候选；尚未开始下载` : "选择候选后生成下载清单";
    $("createManifest").disabled = count === 0;
    $("candidates").querySelectorAll(".candidate-card").forEach((card) => card.classList.toggle("selected", selectedCandidateIds.has(card.dataset.id)));
  };
  $("candidates").querySelectorAll(".candidate-card input").forEach((checkbox) => {
    checkbox.onchange = () => {
      const id = checkbox.closest(".candidate-card").dataset.id;
      if (checkbox.checked) selectedCandidateIds.add(id); else selectedCandidateIds.delete(id);
      updateSelection();
    };
  });
  $("createManifest").onclick = async () => {
    const button = $("createManifest");
    button.disabled = true;
    try {
      await api(`/api/projects/${projectId}/manifest`, { method: "POST", body: JSON.stringify({ payload: { candidate_ids: [...selectedCandidateIds].map(Number) } }) });
      selectedCandidateIds.clear();
      activateDetailTab("tasksPanel");
      await refresh();
      showToast("正在生成下载清单");
    } catch (error) { button.disabled = false; showToast(error.message, "error"); }
  };
  updateSelection();
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1048576) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1073741824) return `${(value / 1048576).toFixed(1)} MB`;
  return `${(value / 1073741824).toFixed(2)} GB`;
}

function renderArtifacts(items = []) {
  $("artifactCount").textContent = items.length ? `${items.length} 个` : "";
  if (!items.length) {
    $("artifacts").className = "empty-block";
    $("artifacts").textContent = "下载或分析产物出现后显示";
    return;
  }
  $("artifacts").className = "";
  $("artifacts").innerHTML = items.map((artifact) => {
    const filename = artifact.path.split("/").pop();
    const extension = filename.includes(".") ? filename.split(".").pop().slice(0, 4) : "file";
    return `<article class="artifact-card"><div class="artifact-head"><span class="artifact-icon">${esc(extension)}</span><div class="artifact-copy"><div class="artifact-name" title="${esc(artifact.path)}">${esc(filename)}</div><div class="artifact-meta">${esc(formatBytes(artifact.size))}${artifact.downloading ? ` · 下载中 ${Number(artifact.progress || 0)}%` : " · 已就绪"}</div></div><a class="artifact-download" href="/api/projects/${projectId}/artifacts/${encodeURI(artifact.path)}" target="_blank" rel="noopener">下载</a></div>${artifact.downloading ? `<div class="progress"><span style="width:${Number(artifact.progress || 0)}%"></span></div>` : ""}</article>`;
  }).join("");
}

function inferWorkflowStage(data, candidateData) {
  const state = data.state || {};
  const jobs = data.jobs || [];
  const artifacts = data.artifacts || [];
  const gateKinds = new Set((data.gates || []).map((gate) => gate.kind));
  const jobKinds = new Set(jobs.map((job) => job.kind));
  let stage = (data.messages || []).some((message) => message.role === "user") ? 0 : 0;
  if (jobKinds.has("crawl") || state.crawl_dir || state.last_crawl_run) stage = 1;
  if ((candidateData.rows || []).length || state.candidate_catalog || gateKinds.has("candidate_triage")) stage = 2;
  if (jobKinds.has("manifest") || state.manifest || gateKinds.has("manifest_review")) stage = 3;
  if (jobKinds.has("download") || gateKinds.has("download") || artifacts.some((item) => item.path.startsWith("raw/"))) stage = 4;
  if (jobKinds.has("qc") || state.analysis_context || gateKinds.has("analysis") || gateKinds.has("analysis_execute")) stage = 5;
  if (gateKinds.has("qc_complete") || artifacts.some((item) => item.path.startsWith("results/"))) stage = 6;
  return stage;
}

function updateStageRail(current) {
  const stages = [...$("stageRail").querySelectorAll("span")];
  const connectors = [...$("stageRail").querySelectorAll("i")];
  stages.forEach((stage, index) => {
    stage.classList.toggle("done", index < current);
    stage.classList.toggle("current", index === current);
  });
  connectors.forEach((connector, index) => connector.classList.toggle("done", index < current));
}

async function refresh(silent = false) {
  if (!projectId || refreshing) return;
  refreshing = true;
  const activeProjectId = projectId;
  const activeEpoch = viewEpoch;
  try {
    const [data, candidateData] = await Promise.all([
      api(`/api/projects/${activeProjectId}`),
      api(`/api/projects/${activeProjectId}/candidates`),
    ]);
    if (projectId !== activeProjectId || viewEpoch !== activeEpoch) return;
    $("projectTitle").textContent = data.project.name;
    renderMessages(data.messages || [], data.gates || []);
    renderJobs(data.jobs || []);
    renderCandidates(candidateData);
    renderArtifacts(data.artifacts || []);
    updateStageRail(inferWorkflowStage(data, candidateData));
    const busy = (data.jobs || []).some((job) => ["queued", "running", "submitted"].includes(job.status));
    $("projectState").className = `state-pill ${busy ? "busy" : ""}`;
    $("projectState").innerHTML = `<i></i>${busy ? "运行中" : data.gates?.length ? "等待确认" : "就绪"}`;
  } catch (error) {
    if (!silent) showToast(error.message, "error");
  } finally { refreshing = false; }
}

function resizeComposer() {
  const input = $("prompt");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  $("characterCount").textContent = `${input.value.length} / 4000`;
  $("sendButton").disabled = sending || !input.value.trim();
}

async function sendMessage(event) {
  event.preventDefault();
  const prompt = $("prompt").value.trim();
  if (!prompt || sending) return;
  if (!projectId) {
    await newProject();
    if (!projectId) return;
  }
  sending = true;
  $("prompt").value = "";
  resizeComposer();
  showOptimisticMessage(prompt);
  try {
    await api(`/api/projects/${projectId}/chat`, { method: "POST", body: JSON.stringify({ prompt }) });
    renderedMessageKey = "";
    await Promise.all([loadProjects(false), refresh()]);
  } catch (error) {
    renderedMessageKey = "";
    await refresh(true);
    showToast(error.message, "error");
  } finally {
    sending = false;
    resizeComposer();
    $("prompt").focus();
  }
}

function activateDetailTab(panelId) {
  document.querySelectorAll(".detail-tab").forEach((tab) => {
    const active = tab.dataset.panel === panelId;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".detail-view").forEach((panel) => panel.classList.toggle("active", panel.id === panelId));
}

function openDetails() {
  const shell = $("appShell");
  if (window.innerWidth > 1240) shell.classList.remove("details-collapsed");
  else shell.classList.add("details-open");
  $("toggleDetails").setAttribute("aria-expanded", "true");
}

function closeDetails() {
  const shell = $("appShell");
  if (window.innerWidth > 1240) shell.classList.add("details-collapsed");
  else shell.classList.remove("details-open");
  $("toggleDetails").setAttribute("aria-expanded", "false");
}

function toggleDetails() {
  const shell = $("appShell");
  const open = window.innerWidth > 1240 ? !shell.classList.contains("details-collapsed") : shell.classList.contains("details-open");
  if (open) closeDetails(); else openDetails();
}

function openSidebar() { $("appShell").classList.add("sidebar-open"); }
function closeSidebar() { $("appShell").classList.remove("sidebar-open"); }

async function checkHealth() {
  try {
    const health = await api("/api/health");
    $("healthDot").className = "online";
    $("healthText").textContent = "服务器已连接";
  } catch (_error) {
    $("healthDot").className = "offline";
    $("healthText").textContent = "服务器连接异常";
  }
}

async function boot() {
  bindPromptButtons();
  resizeComposer();
  await Promise.all([checkHealth(), loadProjects(false)]);
  if (projects[0]) await openProject(projects[0].id, false);
  else showNoProject();
}

$("newProject").onclick = newProject;
$("projectSearch").oninput = renderProjectList;
$("chatForm").onsubmit = sendMessage;
$("prompt").oninput = resizeComposer;
$("prompt").onkeydown = (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("chatForm").requestSubmit();
  }
};
$("toggleDetails").onclick = toggleDetails;
$("closeDetails").onclick = closeDetails;
$("detailsBackdrop").onclick = closeDetails;
$("openSidebar").onclick = openSidebar;
$("closeSidebar").onclick = closeSidebar;
$("sidebarBackdrop").onclick = closeSidebar;
document.querySelectorAll(".detail-tab").forEach((tab) => { tab.onclick = () => activateDetailTab(tab.dataset.panel); });
window.addEventListener("resize", () => {
  if (window.innerWidth > 760) closeSidebar();
  if (window.innerWidth > 1240) $("appShell").classList.remove("details-open");
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) { checkHealth(); refresh(true); } });

boot();
