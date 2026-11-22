/**
 * 提示词组合注入器 · 可视化排序编辑面板前端逻辑
 * 支持：
 * 1. 多提示词组合（Presets）新增、复制、删除、重命名、一键切换当前生效组合、JSON 导入导出。
 * 2. 统一可视化排序流：将「🔒人格设定 (Persona)」与「💬用户最新输入 (Latest User Input)」直接作为锚点呈现在排序列表中。
 * 3. 支持拖拽排序、上下移动、跨锚点移动自动更新位置（人格设定前 / 人格设定后 / 用户最新输入前 / 用户最新输入后）。
 * 4. 支持设置每个位置的最大提示词注入数量（0 为不限制），并在排序列表上实时高亮超出数量上限的条目。
 * 5. 右侧实时请求组装预览（System Prompt & User Prompt）。
 */

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const POS_META = {
  before_persona: {
    key: "before_persona",
    label: "人格设定前",
    shortLabel: "人格前",
    badgeClass: "pos-bp",
  },
  after_persona: {
    key: "after_persona",
    label: "人格设定后",
    shortLabel: "人格后",
    badgeClass: "pos-ap",
  },
  before_user: {
    key: "before_user",
    label: "用户最新输入前",
    shortLabel: "输入前",
    badgeClass: "pos-bu",
  },
  after_user: {
    key: "after_user",
    label: "用户最新输入后",
    shortLabel: "输入后",
    badgeClass: "pos-au",
  },
};

const state = {
  enabled: true,
  activePresetName: "",
  activePresetId: "",
  maxPromptsPerPosition: 10,
  separator: "\\n\\n",
  saveUserInjectedToHistory: false,
  presets: [],
  personas: [],
  variablesHelp: [],
  // 当前正在面板中查看/编辑的组合 ID
  currentPresetId: "",
  // 当前在右侧编辑器选中的提示词条目 ID
  selectedItemId: "",
  dirty: false,
  dragSourceIndex: null,
};

// DOM 引用
const els = {
  toastContainer: document.getElementById("toast-container"),
  globalEnabled: document.getElementById("global-enabled"),
  globalEnabledText: document.getElementById("global-enabled-text"),
  btnRefresh: document.getElementById("btn-refresh"),
  btnSaveAll: document.getElementById("btn-save-all"),
  unsavedIndicator: document.getElementById("unsaved-indicator"),

  presetTabs: document.getElementById("preset-tabs"),
  btnNewPreset: document.getElementById("btn-new-preset"),
  btnDupPreset: document.getElementById("btn-dup-preset"),
  btnExportPreset: document.getElementById("btn-export-preset"),
  btnDelPreset: document.getElementById("btn-del-preset"),

  presetNameInput: document.getElementById("preset-name-input"),
  presetScopeSelect: document.getElementById("preset-scope-select"),
  presetDescInput: document.getElementById("preset-desc-input"),
  maxPerPosInput: document.getElementById("max-per-pos-input"),
  btnActivateCurrent: document.getElementById("btn-activate-current"),

  positionStatsBar: document.getElementById("position-stats-bar"),
  flowList: document.getElementById("flow-list"),

  editorTitle: document.getElementById("editor-title"),
  editorPosBadge: document.getElementById("editor-pos-badge"),
  editorEmpty: document.getElementById("editor-empty"),
  editorForm: document.getElementById("editor-form"),
  itemNameInput: document.getElementById("item-name-input"),
  itemPosSelect: document.getElementById("item-pos-select"),
  itemEnabledInput: document.getElementById("item-enabled-input"),
  itemCharCount: document.getElementById("item-char-count"),
  itemContentInput: document.getElementById("item-content-input"),
  varChips: document.getElementById("var-chips"),
  btnItemDelete: document.getElementById("btn-item-delete"),
  btnItemUp: document.getElementById("btn-item-up"),
  btnItemDown: document.getElementById("btn-item-down"),

  sepInput: document.getElementById("sep-input"),
  saveHistoryInput: document.getElementById("save-history-input"),
  previewPersonaSelect: document.getElementById("preview-persona-select"),
  previewUserInput: document.getElementById("preview-user-input"),
  previewSystem: document.getElementById("preview-system"),
  previewUser: document.getElementById("preview-user"),

  jsonModal: document.getElementById("json-modal"),
  btnModalClose: document.getElementById("btn-modal-close"),
  jsonModalTextarea: document.getElementById("json-modal-textarea"),
  btnJsonCopy: document.getElementById("btn-json-copy"),
  btnJsonImportNew: document.getElementById("btn-json-import-new"),
  btnJsonImportOverwrite: document.getElementById("btn-json-import-overwrite"),
};

function showToast(msg, type = "info") {
  const div = document.createElement("div");
  div.className = `toast toast-${type}`;
  div.textContent = msg;
  els.toastContainer.appendChild(div);
  setTimeout(() => {
    div.remove();
  }, 3200);
}

function setDirty(flag = true) {
  state.dirty = flag;
  els.unsavedIndicator.hidden = !flag;
}

function genId(prefix = "p") {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

function getCurrentPreset() {
  return (
    state.presets.find((p) => p.preset_id === state.currentPresetId) ||
    state.presets[0] ||
    null
  );
}

/**
 * 保证组合的 sequence 中始终存在：
 * 1. anchor_persona (🔒 人格设定)
 * 2. anchor_user_input (💬 用户最新输入)
 * 且 anchor_persona 在 anchor_user_input 之前，
 * 位于两个锚点之间的条目按照 after_persona 在前、before_user 在后有序排列。
 */
function normalizeSequence(rawSeq) {
  const list = Array.isArray(rawSeq) ? rawSeq : [];
  let personaIdx = list.findIndex(
    (x) => (x.__template_key || x.type) === "anchor_persona"
  );
  let userIdx = list.findIndex(
    (x) => (x.__template_key || x.type) === "anchor_user_input"
  );

  // 如果两个锚点均已存在且顺序正确，按当前顺序计算每项的位置归属并规整中间段顺序
  const beforePersona = [];
  const afterPersona = [];
  const beforeUser = [];
  const afterUser = [];

  let anchorPersona = {
    __template_key: "anchor_persona",
    note: "【系统锚点】此处为 AstrBot 的人格设定（Persona）。排在此锚点上方的提示词注入在「人格设定前」，下方的提示词注入在「人格设定后」。",
  };
  let anchorUser = {
    __template_key: "anchor_user_input",
    note: "【系统锚点】此处为「用户最新输入」。排在此锚点上方（且在人格设定锚点下方）的提示词可注入在「用户最新输入前」，排在此锚点下方的提示词注入在「用户最新输入后」。",
  };

  let seenPersona = false;
  let seenUser = false;

  for (let i = 0; i < list.length; i++) {
    const item = list[i];
    if (!item || typeof item !== "object") continue;
    const tkey = item.__template_key || item.type || "prompt_item";
    if (tkey === "anchor_persona") {
      seenPersona = true;
      if (item.note) anchorPersona.note = item.note;
      continue;
    }
    if (tkey === "anchor_user_input") {
      seenUser = true;
      if (item.note) anchorUser.note = item.note;
      continue;
    }

    const clean = {
      __template_key: "prompt_item",
      id: item.id || genId("p"),
      name: (item.name || "未命名提示词").trim() || "未命名提示词",
      enabled: item.enabled !== false,
      position_override: item.position_override || "auto",
      content: typeof item.content === "string" ? item.content : "",
    };

    // 根据所在锚点区间确定有效位置
    let pos = clean.position_override;
    if (personaIdx !== -1 && userIdx !== -1 && personaIdx < userIdx) {
      if (i < personaIdx) {
        pos = "before_persona";
      } else if (i > userIdx) {
        pos = "after_user";
      } else {
        // 位于两个锚点之间
        if (pos !== "before_user") {
          pos = "after_persona";
        }
      }
    } else {
      if (! POS_META[pos]) {
        if (!seenPersona && !seenUser) pos = "before_persona";
        else if (seenPersona && !seenUser) pos = "after_persona";
        else pos = "after_user";
      }
    }

    clean.position_override = pos;
    if (pos === "before_persona") beforePersona.push(clean);
    else if (pos === "after_persona") afterPersona.push(clean);
    else if (pos === "before_user") beforeUser.push(clean);
    else afterUser.push(clean);
  }

  return [
    ...beforePersona,
    anchorPersona,
    ...afterPersona,
    ...beforeUser,
    anchorUser,
    ...afterUser,
  ];
}

/**
 * 为当前组合的 sequence 计算每个节点的展示信息（含有效位置与是否超出单位置数量上限）
 */
function computeFlowNodes(preset) {
  if (!preset) return [];
  preset.sequence = normalizeSequence(preset.sequence);
  const maxLimit = Number(state.maxPromptsPerPosition) || 0;
  const counts = {
    before_persona: 0,
    after_persona: 0,
    before_user: 0,
    after_user: 0,
  };

  return preset.sequence.map((item, index) => {
    const tkey = item.__template_key || item.type || "prompt_item";
    if (tkey === "anchor_persona" || tkey === "anchor_user_input") {
      return {
        index,
        isAnchor: true,
        anchorType: tkey,
        raw: item,
      };
    }

    const pos = POS_META[item.position_override]
      ? item.position_override
      : "after_persona";
    let exceeded = false;
    if (item.enabled && item.content && item.content.trim()) {
      counts[pos] += 1;
      if (maxLimit > 0 && counts[pos] > maxLimit) {
        exceeded = true;
      }
    }

    return {
      index,
      isAnchor: false,
      pos,
      exceeded,
      orderInPos: counts[pos],
      raw: item,
    };
  });
}

// ================================================================
// 渲染 UI
// ================================================================

function renderAll() {
  renderTopbar();
  renderPresetTabs();
  renderPresetMeta();
  renderFlowPanel();
  renderItemEditor();
  renderLivePreview();
}

function renderTopbar() {
  els.globalEnabled.checked = state.enabled;
  els.globalEnabledText.textContent = state.enabled
    ? "总开关：已开启"
    : "总开关：已关闭";
  els.sepInput.value = state.separator;
  els.saveHistoryInput.checked = state.saveUserInjectedToHistory;
  els.maxPerPosInput.value = state.maxPromptsPerPosition;
}

function renderPresetTabs() {
  els.presetTabs.innerHTML = "";
  const activeName = state.activePresetName;
  const activeId = state.activePresetId;

  state.presets.forEach((preset) => {
    const isSelected = preset.preset_id === state.currentPresetId;
    const isActive =
      preset.preset_id === activeId || preset.preset_name === activeName;

    const promptCount = (preset.sequence || []).filter(
      (x) =>
        (x.__template_key || x.type) === "prompt_item" &&
        x.enabled !== false &&
        (x.content || "").trim()
    ).length;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `preset-tab ${isSelected ? "is-selected" : ""} ${
      isActive ? "is-active-preset" : ""
    }`;
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected", isSelected ? "true" : "false");

    const dot = document.createElement("span");
    dot.className = "active-dot";
    dot.title = isActive ? "当前正在生效的提示词组合" : "点击切换到此组合进行编辑";

    const title = document.createElement("span");
    title.textContent = preset.preset_name;

    const badge = document.createElement("span");
    badge.className = "tab-badge";
    badge.textContent = isActive
      ? `生效中 · ${promptCount}条`
      : `${promptCount}条`;

    btn.append(dot, title, badge);
    btn.addEventListener("click", () => {
      state.currentPresetId = preset.preset_id;
      autoSelectFirstPrompt();
      renderAll();
    });

    els.presetTabs.appendChild(btn);
  });
}

function renderPresetMeta() {
  const preset = getCurrentPreset();
  if (!preset) return;

  els.presetNameInput.value = preset.preset_name || "";
  els.presetScopeSelect.value = preset.scope || "all";
  els.presetDescInput.value = preset.description || "";

  const isActive =
    preset.preset_id === state.activePresetId ||
    preset.preset_name === state.activePresetName;
  els.btnActivateCurrent.disabled = isActive;
  els.btnActivateCurrent.textContent = isActive
    ? "✅ 当前生效组合"
    : "⚡ 设为当前生效组合";
}

function renderFlowPanel() {
  const preset = getCurrentPreset();
  if (!preset) return;

  const nodes = computeFlowNodes(preset);
  const maxLimit = Number(state.maxPromptsPerPosition) || 0;

  // 统计四个位置的数量
  const activeCounts = {
    before_persona: 0,
    after_persona: 0,
    before_user: 0,
    after_user: 0,
  };
  const totalCounts = {
    before_persona: 0,
    after_persona: 0,
    before_user: 0,
    after_user: 0,
  };

  nodes.forEach((n) => {
    if (!n.isAnchor) {
      totalCounts[n.pos] += 1;
      if (n.raw.enabled && (n.raw.content || "").trim() && !n.exceeded) {
        activeCounts[n.pos] += 1;
      }
    }
  });

  els.positionStatsBar.innerHTML = "";
  Object.values(POS_META).forEach((meta) => {
    const box = document.createElement("div");
    box.className = `pos-stat-box ${meta.badgeClass}`;
    const limitStr = maxLimit > 0 ? ` / 上限 ${maxLimit}` : "";
    box.innerHTML = `
      <span class="stat-title">${meta.label}</span>
      <span class="stat-count">生效 ${activeCounts[meta.key]} 条 (共 ${totalCounts[meta.key]} 条${limitStr})</span>
    `;
    els.positionStatsBar.appendChild(box);
  });

  // 渲染排序列表
  els.flowList.innerHTML = "";
  nodes.forEach((node, idx) => {
    const li = document.createElement("li");
    li.dataset.index = String(idx);
    li.draggable = true;

    if (node.isAnchor) {
      const isPersona = node.anchorType === "anchor_persona";
      li.className = `flow-node anchor-node ${
        isPersona ? "anchor-persona" : "anchor-user"
      }`;

      const selectedPersonaText = getSelectedPreviewPersonaText();
      const previewSub = isPersona
        ? `【系统核心锚点】AstrBot 人格设定 (Persona) 在此位置渲染。上方为「人格设定前」，下方为「人格设定后」。当前预览人格：「${selectedPersonaText.slice(0, 48)}${selectedPersonaText.length > 48 ? "..." : ""}」`
        : `【系统核心锚点】用户最新输入 (Latest User Input) 在此位置组装。上方可设「用户最新输入前」，下方为「用户最新输入后」。`;

      li.innerHTML = `
        <div class="node-left">
          <span class="drag-handle" title="可拖动锚点相对提示词上下移动">⠿</span>
          <span class="node-index">${idx + 1}</span>
          <div class="node-main">
            <div class="node-title-row">
              <span class="node-name">${
                isPersona
                  ? "🔒 【人格设定 · Persona】"
                  : "💬 【用户最新输入 · Latest User Input】"
              }</span>
              <span class="badge-inline" style="background:rgba(255,255,255,0.45);">排序基准锚点</span>
            </div>
            <p class="node-snippet">${escapeHtml(previewSub)}</p>
          </div>
        </div>
        <div class="node-actions">
          <button type="button" class="icon-action-btn" data-act="up" title="锚点上移" ${
            idx === 0 ? "disabled" : ""
          }>↑</button>
          <button type="button" class="icon-action-btn" data-act="down" title="锚点下移" ${
            idx === nodes.length - 1 ? "disabled" : ""
          }>↓</button>
        </div>
      `;
    } else {
      const item = node.raw;
      const meta = POS_META[node.pos] || POS_META.after_persona;
      const isSelected = item.id === state.selectedItemId;
      li.className = `flow-node ${isSelected ? "is-selected" : ""} ${
        !item.enabled ? "is-disabled" : ""
      } ${node.exceeded ? "is-exceeded" : ""}`;

      const snippet = (item.content || "").replace(/\s+/g, " ").trim();
      const limitBadge = node.exceeded
        ? `<span class="limit-warning-badge" title="该位置已达到单位置数量上限 (${maxLimit}条)，此条不会生效">超出数量上限(${maxLimit})</span>`
        : "";

      li.innerHTML = `
        <div class="node-left">
          <span class="drag-handle" title="按住拖拽调整注入顺序">⠿</span>
          <span class="node-index">${idx + 1}</span>
          <div class="node-main">
            <div class="node-title-row">
              <span class="badge-inline ${meta.badgeClass}">${meta.label}</span>
              <span class="node-name">${escapeHtml(item.name)}</span>
              ${!item.enabled ? '<span class="badge-inline">已停用</span>' : ""}
              ${limitBadge}
            </div>
            <p class="node-snippet">${
              snippet ? escapeHtml(snippet) : "（点击右侧编辑器填写提示词内容...）"
            }</p>
          </div>
        </div>
        <div class="node-actions">
          <select class="node-pos-select" data-act="pos" title="快速切换该条提示词的注入位置">
            <option value="before_persona" ${
              node.pos === "before_persona" ? "selected" : ""
            }>人格设定前</option>
            <option value="after_persona" ${
              node.pos === "after_persona" ? "selected" : ""
            }>人格设定后</option>
            <option value="before_user" ${
              node.pos === "before_user" ? "selected" : ""
            }>用户输入前</option>
            <option value="after_user" ${
              node.pos === "after_user" ? "selected" : ""
            }>用户输入后</option>
          </select>
          <input type="checkbox" data-act="toggle" title="启用/停用该条提示词" ${
            item.enabled ? "checked" : ""
          } />
          <button type="button" class="icon-action-btn" data-act="up" title="上移" ${
            idx === 0 ? "disabled" : ""
          }>↑</button>
          <button type="button" class="icon-action-btn" data-act="down" title="下移" ${
            idx === nodes.length - 1 ? "disabled" : ""
          }>↓</button>
          <button type="button" class="icon-action-btn" data-act="del" title="删除此条提示词">✕</button>
        </div>
      `;

      // 点击卡片选中编辑
      li.addEventListener("click", (e) => {
        if (
          e.target.closest("button") ||
          e.target.closest("select") ||
          e.target.closest("input")
        ) {
          return;
        }
        state.selectedItemId = item.id;
        renderFlowPanel();
        renderItemEditor();
      });

      // 快速切换位置下拉框
      const posSel = li.querySelector('[data-act="pos"]');
      posSel.addEventListener("change", (e) => {
        changePromptPosition(item.id, e.target.value);
      });

      // 快速启用/禁用复选框
      const toggleChk = li.querySelector('[data-act="toggle"]');
      toggleChk.addEventListener("change", (e) => {
        item.enabled = e.target.checked;
        setDirty(true);
        renderAll();
      });

      // 删除按钮
      const delBtn = li.querySelector('[data-act="del"]');
      delBtn.addEventListener("click", () => {
        deletePromptItem(item.id);
      });
    }

    // 上移 / 下移按钮（同时支持提示词与系统锚点）
    const upBtn = li.querySelector('[data-act="up"]');
    const downBtn = li.querySelector('[data-act="down"]');
    if (upBtn) {
      upBtn.addEventListener("click", () => moveSequenceNode(idx, idx - 1));
    }
    if (downBtn) {
      downBtn.addEventListener("click", () => moveSequenceNode(idx, idx + 1));
    }

    // HTML5 拖拽排序事件
    bindDragEvents(li, idx);

    els.flowList.appendChild(li);
  });
}

function renderItemEditor() {
  const preset = getCurrentPreset();
  if (!preset) return;

  const nodes = computeFlowNodes(preset);
  const foundNode = nodes.find(
    (n) => !n.isAnchor && n.raw.id === state.selectedItemId
  );

  if (!foundNode) {
    els.editorEmpty.hidden = false;
    els.editorForm.hidden = true;
    els.editorPosBadge.textContent = "未选择条目";
    els.editorPosBadge.className = "badge-inline";
    return;
  }

  els.editorEmpty.hidden = true;
  els.editorForm.hidden = false;

  const item = foundNode.raw;
  const meta = POS_META[foundNode.pos] || POS_META.after_persona;
  els.editorPosBadge.textContent = meta.label;
  els.editorPosBadge.className = `badge-inline ${meta.badgeClass}`;

  if (document.activeElement !== els.itemNameInput) {
    els.itemNameInput.value = item.name || "";
  }
  els.itemPosSelect.value = foundNode.pos;
  els.itemEnabledInput.checked = item.enabled !== false;

  if (document.activeElement !== els.itemContentInput) {
    els.itemContentInput.value = item.content || "";
  }
  els.itemCharCount.textContent = `${(item.content || "").length} 字符`;
}

function getSelectedPreviewPersonaText() {
  const val = els.previewPersonaSelect.value;
  if (val) return val;
  return "你是一个温柔、博学且富有同理心的 AI 助手。";
}

function renderLivePreview() {
  const preset = getCurrentPreset();
  if (!preset) return;

  const nodes = computeFlowNodes(preset);
  const realSep = (state.separator || "\\n\\n")
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t");

  const now = new Date();
  const weekdays = [
    "星期日",
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
  ];
  const pad = (n) => String(n).padStart(2, "0");
  const dateStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(
    now.getDate()
  )}`;
  const timeStr = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(
    now.getSeconds()
  )}`;

  const sampleVars = {
    "{user_name}": "测试用户",
    "{user_id}": "10001",
    "{group_id}": "888888",
    "{date}": dateStr,
    "{time}": timeStr,
    "{datetime}": `${dateStr} ${timeStr}`,
    "{weekday}": weekdays[now.getDay()],
    "{umo}": "webchat:FriendMessage:preview_user",
  };

  function replaceVars(str) {
    let out = str;
    for (const [k, v] of Object.entries(sampleVars)) {
      out = out.split(k).join(v);
    }
    return out;
  }

  const buckets = {
    before_persona: [],
    after_persona: [],
    before_user: [],
    after_user: [],
  };

  nodes.forEach((n) => {
    if (n.isAnchor) return;
    if (!n.raw.enabled) return;
    if (n.exceeded) return;
    const text = replaceVars((n.raw.content || "").trim());
    if (text) {
      buckets[n.pos].push(text);
    }
  });

  const personaText = `# Persona Instructions\n\n${getSelectedPreviewPersonaText()}`;
  const sysParts = [
    ...buckets.before_persona,
    personaText,
    ...buckets.after_persona,
  ];
  els.previewSystem.textContent = sysParts.join(realSep);

  const userRaw =
    els.previewUserInput.value || "你好！请帮我规划一下今天的学习安排。";
  const userPrefixAndInput = [...buckets.before_user, userRaw].join(realSep);

  let userDisplay = `【主消息 req.prompt】:\n${userPrefixAndInput}`;
  if (buckets.after_user.length > 0) {
    const modeNote = state.saveUserInjectedToHistory
      ? "持久化写入历史"
      : "临时生效 temp，不污染历史";
    userDisplay += `\n\n【附加内容块 req.extra_user_content_parts (${buckets.after_user.length} 块 · ${modeNote})】:\n`;
    userDisplay += buckets.after_user
      .map((t, i) => `--- [后置块 #${i + 1}] ---\n${t}`)
      .join("\n");
  }
  els.previewUser.textContent = userDisplay;
}

// ================================================================
// 排序、移动与位置变更操作
// ================================================================

/**
 * 在排序序列中将 fromIdx 移动到 toIdx（支持拖动提示词，也支持拖动「人格设定」与「用户最新输入」锚点）
 */
function moveSequenceNode(fromIdx, toIdx) {
  const preset = getCurrentPreset();
  if (!preset) return;
  const seq = [...preset.sequence];
  if (toIdx < 0 || toIdx >= seq.length || fromIdx === toIdx) return;

  const [moved] = seq.splice(fromIdx, 1);
  seq.splice(toIdx, 0, moved);

  // 检查两个系统锚点的位置，绝对保证 anchor_persona 在 anchor_user_input 之前
  const pIdx = seq.findIndex(
    (x) => (x.__template_key || x.type) === "anchor_persona"
  );
  const uIdx = seq.findIndex(
    (x) => (x.__template_key || x.type) === "anchor_user_input"
  );
  if (pIdx > uIdx) {
    showToast("「🔒人格设定」锚点必须位于「💬用户最新输入」锚点之前", "error");
    return;
  }

  // 根据新顺序更新每个条目的 position_override
  for (let i = 0; i < seq.length; i++) {
    const it = seq[i];
    const tkey = it.__template_key || it.type || "prompt_item";
    if (tkey === "anchor_persona" || tkey === "anchor_user_input") continue;

    if (i < pIdx) {
      it.position_override = "before_persona";
    } else if (i > uIdx) {
      it.position_override = "after_user";
    } else {
      // 位于两个锚点之间：若从外侧拖入，根据拖入靠近哪个锚点智能设定为 after_persona 或 before_user
      if (
        it.position_override !== "after_persona" &&
        it.position_override !== "before_user"
      ) {
        it.position_override =
          fromIdx > uIdx ? "before_user" : "after_persona";
      }
    }
  }

  preset.sequence = normalizeSequence(seq);
  setDirty(true);
  renderAll();
}

/**
 * 显式修改某条提示词的所属位置（将其移动到对应锚点区间）
 */
function changePromptPosition(itemId, newPos) {
  const preset = getCurrentPreset();
  if (!preset || !POS_META[newPos]) return;

  const item = preset.sequence.find((x) => x.id === itemId);
  if (!item) return;

  item.position_override = newPos;
  preset.sequence = normalizeSequence(preset.sequence);
  setDirty(true);
  renderAll();
}

/**
 * 向指定位置添加一条新提示词
 */
function addPromptAtPosition(posKey) {
  const preset = getCurrentPreset();
  if (!preset) return;

  const meta = POS_META[posKey] || POS_META.after_persona;
  const newItem = {
    __template_key: "prompt_item",
    id: genId("p"),
    name: `${meta.label}提示词`,
    enabled: true,
    position_override: meta.key,
    content: "",
  };

  preset.sequence.push(newItem);
  preset.sequence = normalizeSequence(preset.sequence);
  state.selectedItemId = newItem.id;
  setDirty(true);
  renderAll();
  els.itemContentInput.focus();
}

function deletePromptItem(itemId) {
  const preset = getCurrentPreset();
  if (!preset) return;
  preset.sequence = preset.sequence.filter((x) => x.id !== itemId);
  if (state.selectedItemId === itemId) {
    autoSelectFirstPrompt();
  }
  setDirty(true);
  renderAll();
}

function autoSelectFirstPrompt() {
  const preset = getCurrentPreset();
  if (!preset) {
    state.selectedItemId = "";
    return;
  }
  const first = (preset.sequence || []).find(
    (x) => (x.__template_key || x.type || "prompt_item") === "prompt_item"
  );
  state.selectedItemId = first ? first.id : "";
}

function bindDragEvents(li, idx) {
  li.addEventListener("dragstart", (e) => {
    state.dragSourceIndex = idx;
    li.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(idx));
  });

  li.addEventListener("dragend", () => {
    state.dragSourceIndex = null;
    li.classList.remove("dragging");
    document
      .querySelectorAll(".flow-node")
      .forEach((el) =>
        el.classList.remove("drag-over-top", "drag-over-bottom")
      );
  });

  li.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (state.dragSourceIndex === null || state.dragSourceIndex === idx) return;
    const rect = li.getBoundingClientRect();
    const mid = rect.top + rect.height / 2;
    if (e.clientY < mid) {
      li.classList.add("drag-over-top");
      li.classList.remove("drag-over-bottom");
    } else {
      li.classList.add("drag-over-bottom");
      li.classList.remove("drag-over-top");
    }
  });

  li.addEventListener("dragleave", () => {
    li.classList.remove("drag-over-top", "drag-over-bottom");
  });

  li.addEventListener("drop", (e) => {
    e.preventDefault();
    li.classList.remove("drag-over-top", "drag-over-bottom");
    const fromIdx = state.dragSourceIndex;
    if (fromIdx === null || fromIdx === idx) return;

    const rect = li.getBoundingClientRect();
    const dropAfter = e.clientY >= rect.top + rect.height / 2;
    let targetIdx = idx;
    if (fromIdx < idx && !dropAfter) {
      targetIdx = idx - 1;
    } else if (fromIdx > idx && dropAfter) {
      targetIdx = idx + 1;
    }
    moveSequenceNode(fromIdx, targetIdx);
  });
}

// ================================================================
// 服务端通信与事件绑定
// ================================================================

async function loadStateFromServer() {
  try {
    if (bridge && typeof bridge.ready === "function") {
      await bridge.ready();
    }
    const data = await bridge.apiGet("state");
    applyServerData(data);
    setDirty(false);
  } catch (err) {
    showToast(`加载配置失败: ${err.message || err}`, "error");
  }
}

function applyServerData(data) {
  state.enabled = data.enabled !== false;
  state.activePresetName = data.active_preset_name || "";
  state.activePresetId = data.active_preset_id || "";
  state.maxPromptsPerPosition =
    data.max_prompts_per_position !== undefined
      ? Number(data.max_prompts_per_position)
      : 10;
  state.separator = data.separator || "\\n\\n";
  state.saveUserInjectedToHistory = Boolean(
    data.save_user_injected_to_history
  );
  state.presets = Array.isArray(data.presets) ? data.presets : [];
  state.presets.forEach((p) => {
    p.sequence = normalizeSequence(p.sequence);
  });

  if (Array.isArray(data.personas)) {
    state.personas = data.personas;
    els.previewPersonaSelect.innerHTML =
      '<option value="">-- 示例默认人格设定 --</option>';
    state.personas.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.prompt || "";
      opt.textContent = `人格：${p.name}`;
      els.previewPersonaSelect.appendChild(opt);
    });
  }

  if (Array.isArray(data.variables_help)) {
    state.variablesHelp = data.variables_help;
    renderVariableChips();
  }

  if (
    !state.currentPresetId ||
    !state.presets.some((p) => p.preset_id === state.currentPresetId)
  ) {
    state.currentPresetId =
      state.activePresetId ||
      (state.presets[0] ? state.presets[0].preset_id : "");
  }
  autoSelectFirstPrompt();
  renderAll();
}

function renderVariableChips() {
  els.varChips.innerHTML = "";
  state.variablesHelp.forEach((v) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "var-chip";
    btn.textContent = `${v.var} (${v.desc})`;
    btn.title = `点击在光标处插入 ${v.var}`;
    btn.addEventListener("click", () => {
      insertAtCursor(els.itemContentInput, v.var);
    });
    els.varChips.appendChild(btn);
  });
}

function insertAtCursor(textarea, text) {
  const start = textarea.selectionStart || 0;
  const end = textarea.selectionEnd || 0;
  const val = textarea.value;
  textarea.value = val.slice(0, start) + text + val.slice(end);
  textarea.selectionStart = textarea.selectionEnd = start + text.length;
  textarea.dispatchEvent(new Event("input"));
  textarea.focus();
}

async function saveAllToServer() {
  try {
    els.btnSaveAll.disabled = true;
    els.btnSaveAll.textContent = "⏳ 保存中...";
    const res = await bridge.apiPost("save", {
      enabled: state.enabled,
      active_preset_name: state.activePresetName,
      max_prompts_per_position: Number(state.maxPromptsPerPosition) || 0,
      separator: state.separator,
      save_user_injected_to_history: state.saveUserInjectedToHistory,
      presets: state.presets,
    });
    applyServerData(res);
    setDirty(false);
    showToast("✅ 所有提示词组合与排序设置已保存并立即生效！", "success");
  } catch (err) {
    showToast(`保存失败: ${err.message || err}`, "error");
  } finally {
    els.btnSaveAll.disabled = false;
    els.btnSaveAll.textContent = "💾 保存全部修改";
  }
}

function bindEvents() {
  els.globalEnabled.addEventListener("change", (e) => {
    state.enabled = e.target.checked;
    setDirty(true);
    renderTopbar();
  });

  els.btnRefresh.addEventListener("click", () => {
    loadStateFromServer();
    showToast("已从服务器刷新配置");
  });

  els.btnSaveAll.addEventListener("click", () => {
    saveAllToServer();
  });

  // 组合元信息修改
  els.presetNameInput.addEventListener("input", (e) => {
    const preset = getCurrentPreset();
    if (!preset) return;
    const oldName = preset.preset_name;
    preset.preset_name = e.target.value.trim() || "未命名组合";
    if (
      state.activePresetId === preset.preset_id ||
      state.activePresetName === oldName
    ) {
      state.activePresetName = preset.preset_name;
    }
    setDirty(true);
    renderPresetTabs();
    renderPresetMeta();
  });

  els.presetScopeSelect.addEventListener("change", (e) => {
    const preset = getCurrentPreset();
    if (!preset) return;
    preset.scope = e.target.value;
    setDirty(true);
  });

  els.presetDescInput.addEventListener("input", (e) => {
    const preset = getCurrentPreset();
    if (!preset) return;
    preset.description = e.target.value;
    setDirty(true);
  });

  els.maxPerPosInput.addEventListener("input", (e) => {
    const val = Math.max(0, Math.min(100, Number(e.target.value) || 0));
    state.maxPromptsPerPosition = val;
    setDirty(true);
    renderFlowPanel();
    renderLivePreview();
  });

  // 设为当前生效组合
  els.btnActivateCurrent.addEventListener("click", async () => {
    const preset = getCurrentPreset();
    if (!preset) return;
    preset.enabled = true;
    state.activePresetId = preset.preset_id;
    state.activePresetName = preset.preset_name;
    await saveAllToServer();
    showToast(`⚡ 已切换当前生效组合为：【${preset.preset_name}】`, "success");
  });

  // 新建组合
  els.btnNewPreset.addEventListener("click", () => {
    const newId = genId("preset");
    const newName = `提示词组合 ${state.presets.length + 1}`;
    const newPreset = {
      __template_key: "preset_group",
      preset_id: newId,
      preset_name: newName,
      enabled: true,
      scope: "all",
      description: "",
      sequence: normalizeSequence([
        {
          __template_key: "prompt_item",
          id: genId("p"),
          name: "人格前置设定",
          enabled: true,
          position_override: "before_persona",
          content: "",
        },
      ]),
    };
    state.presets.push(newPreset);
    state.currentPresetId = newId;
    autoSelectFirstPrompt();
    setDirty(true);
    renderAll();
    showToast(`已创建新组合「${newName}」，请点击右上角保存`, "info");
  });

  // 复制当前组合
  els.btnDupPreset.addEventListener("click", () => {
    const curr = getCurrentPreset();
    if (!curr) return;
    const copy = JSON.parse(JSON.stringify(curr));
    copy.preset_id = genId("preset");
    copy.preset_name = `${curr.preset_name} - 副本`;
    copy.sequence.forEach((it) => {
      if ((it.__template_key || it.type) === "prompt_item") {
        it.id = genId("p");
      }
    });
    state.presets.push(copy);
    state.currentPresetId = copy.preset_id;
    autoSelectFirstPrompt();
    setDirty(true);
    renderAll();
    showToast(`已复制组合「${copy.preset_name}」`, "info");
  });

  // 删除当前组合
  els.btnDelPreset.addEventListener("click", () => {
    if (state.presets.length <= 1) {
      showToast("至少需要保留一个提示词组合，无法删除最后一个组合", "error");
      return;
    }
    const curr = getCurrentPreset();
    if (!curr) return;
    state.presets = state.presets.filter(
      (p) => p.preset_id !== curr.preset_id
    );
    state.currentPresetId = state.presets[0].preset_id;
    if (
      state.activePresetId === curr.preset_id ||
      state.activePresetName === curr.preset_name
    ) {
      state.activePresetId = state.presets[0].preset_id;
      state.activePresetName = state.presets[0].preset_name;
    }
    autoSelectFirstPrompt();
    setDirty(true);
    renderAll();
    showToast(`已移除组合「${curr.preset_name}」，点击保存后生效`);
  });

  // 快速添加提示词到 4 个位置
  document.querySelectorAll("[data-add-pos]").forEach((btn) => {
    btn.addEventListener("click", () => {
      addPromptAtPosition(btn.dataset.addPos);
    });
  });

  // 右侧条目编辑器事件
  els.itemNameInput.addEventListener("input", (e) => {
    const preset = getCurrentPreset();
    if (!preset) return;
    const item = preset.sequence.find((x) => x.id === state.selectedItemId);
    if (!item) return;
    item.name = e.target.value || "未命名提示词";
    setDirty(true);
    renderFlowPanel();
  });

  els.itemPosSelect.addEventListener("change", (e) => {
    if (!state.selectedItemId) return;
    changePromptPosition(state.selectedItemId, e.target.value);
  });

  els.itemEnabledInput.addEventListener("change", (e) => {
    const preset = getCurrentPreset();
    if (!preset) return;
    const item = preset.sequence.find((x) => x.id === state.selectedItemId);
    if (!item) return;
    item.enabled = e.target.checked;
    setDirty(true);
    renderAll();
  });

  els.itemContentInput.addEventListener("input", (e) => {
    const preset = getCurrentPreset();
    if (!preset) return;
    const item = preset.sequence.find((x) => x.id === state.selectedItemId);
    if (!item) return;
    item.content = e.target.value;
    els.itemCharCount.textContent = `${item.content.length} 字符`;
    setDirty(true);
    renderFlowPanel();
    renderLivePreview();
  });

  els.btnItemDelete.addEventListener("click", () => {
    if (state.selectedItemId) {
      deletePromptItem(state.selectedItemId);
    }
  });

  els.btnItemUp.addEventListener("click", () => {
    const preset = getCurrentPreset();
    if (!preset) return;
    const idx = preset.sequence.findIndex((x) => x.id === state.selectedItemId);
    if (idx > 0) moveSequenceNode(idx, idx - 1);
  });

  els.btnItemDown.addEventListener("click", () => {
    const preset = getCurrentPreset();
    if (!preset) return;
    const idx = preset.sequence.findIndex((x) => x.id === state.selectedItemId);
    if (idx >= 0 && idx < preset.sequence.length - 1) {
      moveSequenceNode(idx, idx + 1);
    }
  });

  // 高级设置 & 预览控件
  els.sepInput.addEventListener("input", (e) => {
    state.separator = e.target.value;
    setDirty(true);
    renderLivePreview();
  });

  els.saveHistoryInput.addEventListener("change", (e) => {
    state.saveUserInjectedToHistory = e.target.checked;
    setDirty(true);
    renderLivePreview();
  });

  els.previewPersonaSelect.addEventListener("change", () => {
    renderFlowPanel();
    renderLivePreview();
  });

  els.previewUserInput.addEventListener("input", () => {
    renderLivePreview();
  });

  // JSON 导入/导出模态框
  els.btnExportPreset.addEventListener("click", () => {
    const curr = getCurrentPreset();
    if (!curr) return;
    els.jsonModalTextarea.value = JSON.stringify(curr, null, 2);
    els.jsonModal.hidden = false;
  });

  els.btnModalClose.addEventListener("click", () => {
    els.jsonModal.hidden = true;
  });

  els.btnJsonCopy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(els.jsonModalTextarea.value);
      showToast("📋 已复制 JSON 到剪贴板", "success");
    } catch {
      els.jsonModalTextarea.select();
      document.execCommand("copy");
      showToast("📋 已复制 JSON", "success");
    }
  });

  els.btnJsonImportNew.addEventListener("click", () => {
    try {
      const parsed = JSON.parse(els.jsonModalTextarea.value);
      if (!parsed || typeof parsed !== "object") {
        throw new Error("无效的 JSON 对象");
      }
      parsed.preset_id = genId("preset");
      parsed.preset_name = `${parsed.preset_name || "导入组合"} (导入)`;
      parsed.sequence = normalizeSequence(parsed.sequence);
      state.presets.push(parsed);
      state.currentPresetId = parsed.preset_id;
      autoSelectFirstPrompt();
      setDirty(true);
      els.jsonModal.hidden = true;
      renderAll();
      showToast(`已导入为新组合「${parsed.preset_name}」`, "success");
    } catch (err) {
      showToast(`JSON 解析失败: ${err.message}`, "error");
    }
  });

  els.btnJsonImportOverwrite.addEventListener("click", () => {
    try {
      const parsed = JSON.parse(els.jsonModalTextarea.value);
      if (!parsed || typeof parsed !== "object") {
        throw new Error("无效的 JSON 对象");
      }
      const curr = getCurrentPreset();
      if (!curr) return;
      curr.preset_name = parsed.preset_name || curr.preset_name;
      curr.scope = parsed.scope || curr.scope;
      curr.description = parsed.description || "";
      curr.sequence = normalizeSequence(parsed.sequence);
      autoSelectFirstPrompt();
      setDirty(true);
      els.jsonModal.hidden = true;
      renderAll();
      showToast(`已导入并覆盖组合「${curr.preset_name}」`, "success");
    } catch (err) {
      showToast(`JSON 解析失败: ${err.message}`, "error");
    }
  });
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// 初始化启动
bindEvents();
loadStateFromServer();
