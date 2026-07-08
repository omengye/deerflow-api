(function () {
  const storage = {
    token: "deerflow.admin.token",
    loggedIn: "deerflow.admin.loggedIn",
  };
  const defaultModelUse = "langchain_openai:ChatOpenAI";
  const customModelUseValue = "__custom__";
  const modelUseGroups = [
    {
      label: "LangChain 标准类",
      options: [
        { label: "OpenAI 兼容 - langchain_openai:ChatOpenAI", value: "langchain_openai:ChatOpenAI" },
        { label: "Azure OpenAI - langchain_openai:AzureChatOpenAI", value: "langchain_openai:AzureChatOpenAI" },
        { label: "Anthropic - langchain_anthropic:ChatAnthropic", value: "langchain_anthropic:ChatAnthropic" },
        { label: "DeepSeek - langchain_deepseek:ChatDeepSeek", value: "langchain_deepseek:ChatDeepSeek" },
        { label: "Google Gemini - langchain_google_genai:ChatGoogleGenerativeAI", value: "langchain_google_genai:ChatGoogleGenerativeAI" },
      ],
    },
    {
      label: "DeerFlow 内置适配类",
      options: [
        { label: "DashScope reasoning - deerflow.models.patched_dashscope:PatchedDashScopeChatOpenAI", value: "deerflow.models.patched_dashscope:PatchedDashScopeChatOpenAI" },
        { label: "DeepSeek reasoning - deerflow.models.patched_deepseek:PatchedChatDeepSeek", value: "deerflow.models.patched_deepseek:PatchedChatDeepSeek" },
        { label: "MiniMax reasoning - deerflow.models.patched_minimax:PatchedChatMiniMax", value: "deerflow.models.patched_minimax:PatchedChatMiniMax" },
        { label: "Gemini OpenAI gateway - deerflow.models.patched_openai:PatchedChatOpenAI", value: "deerflow.models.patched_openai:PatchedChatOpenAI" },
        { label: "vLLM OpenAI-compatible - deerflow.models.vllm_provider:VllmChatModel", value: "deerflow.models.vllm_provider:VllmChatModel" },
        { label: "MindIE - deerflow.models.mindie_provider:MindIEChatModel", value: "deerflow.models.mindie_provider:MindIEChatModel" },
        { label: "Claude OAuth/cache - deerflow.models.claude_provider:ClaudeChatModel", value: "deerflow.models.claude_provider:ClaudeChatModel" },
        { label: "ChatGPT Codex Responses - deerflow.models.openai_codex_provider:CodexChatModel", value: "deerflow.models.openai_codex_provider:CodexChatModel" },
      ],
    },
  ];

  const state = {
    baseUrl: "",
    token: "",
    models: [],
    skills: [],
    mcpServers: {},
    health: null,
    adminMe: null,
    adminConfig: null,
    feishu: null,
    customSkills: [],
    currentView: "overview",
    editingModelName: null,
  };

  const views = {
    overview: "总览",
    models: "大模型",
    skills: "Skills",
    mcp: "MCP",
    feishu: "Feishu",
    runtime: "运行配置",
  };

  const el = {
    loginView: document.getElementById("loginView"),
    appView: document.getElementById("appView"),
    loginForm: document.getElementById("loginForm"),
    loginButton: document.getElementById("loginButton"),
    loginMessage: document.getElementById("loginMessage"),
    tokenInput: document.getElementById("tokenInput"),
    logoutButton: document.getElementById("logoutButton"),
    refreshButton: document.getElementById("refreshButton"),
    sidebarBaseUrl: document.getElementById("sidebarBaseUrl"),
    connectionBadge: document.getElementById("connectionBadge"),
    viewTitle: document.getElementById("viewTitle"),
    toast: document.getElementById("toast"),
    modelsCount: document.getElementById("modelsCount"),
    skillsCount: document.getElementById("skillsCount"),
    mcpCount: document.getElementById("mcpCount"),
    readyStatus: document.getElementById("readyStatus"),
    healthDetails: document.getElementById("healthDetails"),
    modelsTableBody: document.getElementById("modelsTableBody"),
    skillsTableBody: document.getElementById("skillsTableBody"),
    enabledOnlyToggle: document.getElementById("enabledOnlyToggle"),
    skillCategoryFilter: document.getElementById("skillCategoryFilter"),
    skillActionMessage: document.getElementById("skillActionMessage"),
    mcpEditor: document.getElementById("mcpEditor"),
    mcpMessage: document.getElementById("mcpMessage"),
    saveMcpButton: document.getElementById("saveMcpButton"),
    mcpServerSelect: document.getElementById("mcpServerSelect"),
    enableMcpButton: document.getElementById("enableMcpButton"),
    disableMcpButton: document.getElementById("disableMcpButton"),
    testMcpButton: document.getElementById("testMcpButton"),
    feishuStatusDetails: document.getElementById("feishuStatusDetails"),
    feishuMessage: document.getElementById("feishuMessage"),
    feishuEditForm: document.getElementById("feishuEditForm"),
    saveFeishuButton: document.getElementById("saveFeishuButton"),
    restartFeishuButton: document.getElementById("restartFeishuButton"),
    feishuEnabled: document.getElementById("feishuEnabled"),
    feishuAppId: document.getElementById("feishuAppId"),
    feishuAppSecret: document.getElementById("feishuAppSecret"),
    feishuVerificationToken: document.getElementById("feishuVerificationToken"),
    feishuRestartOnSave: document.getElementById("feishuRestartOnSave"),
    modelDraftPanel: document.getElementById("modelDraftPanel"),
    openModelDraftButton: document.getElementById("openModelDraftButton"),
    closeModelDraftButton: document.getElementById("closeModelDraftButton"),
    modelDraftForm: document.getElementById("modelDraftForm"),
    modelYamlPreview: document.getElementById("modelYamlPreview"),
    copyModelYamlButton: document.getElementById("copyModelYamlButton"),
    saveModelButton: document.getElementById("saveModelButton"),
    modelDraftMessage: document.getElementById("modelDraftMessage"),
    draftModelName: document.getElementById("draftModelName"),
    draftDisplayName: document.getElementById("draftDisplayName"),
    draftUse: document.getElementById("draftUse"),
    draftUseCustomRow: document.getElementById("draftUseCustomRow"),
    draftUseCustom: document.getElementById("draftUseCustom"),
    draftModelId: document.getElementById("draftModelId"),
    draftBaseUrl: document.getElementById("draftBaseUrl"),
    draftApiKey: document.getElementById("draftApiKey"),
    draftThinking: document.getElementById("draftThinking"),
    draftVision: document.getElementById("draftVision"),
    skillDraftPanel: document.getElementById("skillDraftPanel"),
    openSkillDraftButton: document.getElementById("openSkillDraftButton"),
    closeSkillDraftButton: document.getElementById("closeSkillDraftButton"),
    skillDraftForm: document.getElementById("skillDraftForm"),
    copySkillMarkdownButton: document.getElementById("copySkillMarkdownButton"),
    saveSkillButton: document.getElementById("saveSkillButton"),
    deleteSkillButton: document.getElementById("deleteSkillButton"),
    refreshCustomSkillsButton: document.getElementById("refreshCustomSkillsButton"),
    loadSkillHistoryButton: document.getElementById("loadSkillHistoryButton"),
    draftSkillName: document.getElementById("draftSkillName"),
    draftSkillDescription: document.getElementById("draftSkillDescription"),
    draftSkillEnabled: document.getElementById("draftSkillEnabled"),
    customSkillSelect: document.getElementById("customSkillSelect"),
    skillMarkdownEditor: document.getElementById("skillMarkdownEditor"),
    skillDraftMessage: document.getElementById("skillDraftMessage"),
    skillHistoryPreview: document.getElementById("skillHistoryPreview"),
    supportFilePath: document.getElementById("supportFilePath"),
    supportFileContent: document.getElementById("supportFileContent"),
    writeSupportFileButton: document.getElementById("writeSupportFileButton"),
    deleteSupportFileButton: document.getElementById("deleteSupportFileButton"),
    runtimeConfigDetails: document.getElementById("runtimeConfigDetails"),
    reloadConfigButton: document.getElementById("reloadConfigButton"),
    runtimeMessage: document.getElementById("runtimeMessage"),
    runtimeEditForm: document.getElementById("runtimeEditForm"),
    saveRuntimeButton: document.getElementById("saveRuntimeButton"),
    runtimeModelName: document.getElementById("runtimeModelName"),
    runtimeMaxSubagents: document.getElementById("runtimeMaxSubagents"),
    runtimeChatTimeout: document.getElementById("runtimeChatTimeout"),
    runtimeMaxUploadSize: document.getElementById("runtimeMaxUploadSize"),
    runtimeMaxUploads: document.getElementById("runtimeMaxUploads"),
    runtimeAllowedExtensions: document.getElementById("runtimeAllowedExtensions"),
    runtimeThinking: document.getElementById("runtimeThinking"),
    runtimeSubagent: document.getElementById("runtimeSubagent"),
    runtimePlanMode: document.getElementById("runtimePlanMode"),
  };

  function defaultBaseUrl() {
    if (window.location.protocol === "file:") {
      return "http://localhost:8000";
    }
    return window.location.origin;
  }

  function normalizeBaseUrl(value) {
    return String(value || "").trim().replace(/\/+$/, "");
  }

  function setBusy(button, busy, text) {
    if (!button) return;
    button.disabled = busy;
    if (text) {
      button.dataset.idleText = button.dataset.idleText || button.textContent;
      button.textContent = busy ? text : button.dataset.idleText;
    }
  }

  function setConnection(status, text) {
    el.connectionBadge.className = `status-badge ${status}`;
    el.connectionBadge.textContent = text;
  }

  function showToast(message) {
    el.toast.textContent = message;
    el.toast.classList.remove("hidden");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => el.toast.classList.add("hidden"), 4000);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function authHeaders() {
    const headers = {};
    if (state.token) {
      headers.Authorization = `Bearer ${state.token}`;
    }
    return headers;
  }

  async function request(path, options = {}) {
    const method = options.method || "GET";
    const headers = {
      Accept: "application/json",
      ...authHeaders(),
      ...(options.headers || {}),
    };
    const init = { method, headers };

    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }

    const response = await fetch(`${state.baseUrl}${path}`, init);
    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (_err) {
        payload = { detail: text };
      }
    }

    if (!response.ok && !options.allowHttpError) {
      const detail = payload && payload.detail ? payload.detail : response.statusText;
      const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      error.status = response.status;
      error.payload = payload;
      throw error;
    }

    if (payload && typeof payload === "object" && !Array.isArray(payload)) {
      payload.__ok = response.ok;
      payload.__status = response.status;
    }
    return payload;
  }

  async function verifyLogin(event) {
    if (event) event.preventDefault();
    state.baseUrl = normalizeBaseUrl(defaultBaseUrl());
    state.token = el.tokenInput.value.trim();
    el.loginMessage.textContent = "";

    if (!state.baseUrl) {
      el.loginMessage.textContent = "无法确定服务地址。";
      return;
    }

    setBusy(el.loginButton, true, "校验中");
    try {
      await request("/api/models");
      await loadAdminMe();
      sessionStorage.setItem(storage.token, state.token);
      sessionStorage.setItem(storage.loggedIn, "1");
      showApp();
      await loadAll();
    } catch (error) {
      if (error.status === 401) {
        el.loginMessage.textContent = "Token 未通过后端校验。";
      } else {
        el.loginMessage.textContent = `连接失败：${error.message}`;
      }
    } finally {
      setBusy(el.loginButton, false);
    }
  }

  function showApp() {
    el.loginView.classList.add("hidden");
    el.appView.classList.remove("hidden");
    el.sidebarBaseUrl.textContent = state.baseUrl;
  }

  function showLogin() {
    el.appView.classList.add("hidden");
    el.loginView.classList.remove("hidden");
    setConnection("neutral", "未连接");
  }

  function logout() {
    sessionStorage.removeItem(storage.token);
    sessionStorage.removeItem(storage.loggedIn);
    state.token = "";
    el.tokenInput.value = "";
    showLogin();
  }

  async function loadAll() {
    setConnection("neutral", "刷新中");
    setBusy(el.refreshButton, true);

    const tasks = await Promise.allSettled([
      loadAdminMe(),
      loadAdminConfig(),
      loadHealth(),
      loadModels(),
      loadSkills(),
      loadCustomSkills(),
      loadMcp(),
      loadFeishu(),
    ]);

    const failed = tasks.filter((task) => task.status === "rejected");
    if (failed.length) {
      const first = failed[0].reason;
      setConnection(first.status === 401 ? "danger" : "warn", first.status === 401 ? "未授权" : "部分失败");
      showToast(`刷新未完全成功：${first.message || first}`);
    } else {
      setConnection("ok", "已连接");
    }

    renderOverview();
    setBusy(el.refreshButton, false);
  }

  async function loadAdminMe() {
    const data = await request("/api/admin/me");
    state.adminMe = data;
    return data;
  }

  async function loadAdminConfig() {
    const data = await request("/api/admin/config");
    state.adminConfig = data;
    renderRuntimeConfig();
    return data;
  }

  async function loadHealth() {
    const health = await request("/health/ready", { allowHttpError: true });
    state.health = health;
    return health;
  }

  async function loadModels() {
    const data = await request("/api/models");
    state.models = Array.isArray(data?.models) ? data.models : [];
    renderModels();
  }

  async function loadSkills() {
    const query = el.enabledOnlyToggle.checked ? "?enabled_only=true" : "";
    const data = await request(`/api/skills${query}`);
    state.skills = Array.isArray(data?.skills) ? data.skills : [];
    renderSkills();
  }

  async function loadCustomSkills() {
    const data = await request("/api/admin/skills/custom");
    state.customSkills = Array.isArray(data?.skills) ? data.skills : [];
    renderCustomSkillSelect();
    return data;
  }

  async function loadMcp() {
    const data = await request("/api/mcp/config");
    state.mcpServers = data?.mcp_servers && typeof data.mcp_servers === "object" ? data.mcp_servers : {};
    el.mcpEditor.value = JSON.stringify(state.mcpServers, null, 2);
    renderMcpServerSelect();
  }

  async function loadFeishu() {
    const data = await request("/api/admin/feishu");
    state.feishu = data;
    renderFeishu();
    return data;
  }

  function renderOverview() {
    el.modelsCount.textContent = String(state.models.length);
    el.skillsCount.textContent = String(filteredSkills().length);
    el.mcpCount.textContent = String(Object.keys(state.mcpServers).length);
    el.readyStatus.textContent = state.health?.status || (state.health?.__ok ? "ok" : "error");
    renderHealthDetails();
  }

  function renderHealthDetails() {
    const checks = state.health?.checks;
    if (!checks || typeof checks !== "object") {
      el.healthDetails.innerHTML = `<div class="detail-row"><span>ready</span><strong>${escapeHtml(state.health?.detail || "无详情")}</strong></div>`;
      return;
    }

    el.healthDetails.innerHTML = Object.entries(checks)
      .map(([name, value]) => {
        const ok = value && value.ok;
        const label = ok ? "ok" : "error";
        const text = value?.type || value?.path || value?.error || label;
        return `
          <div class="detail-row">
            <span>${escapeHtml(name)}</span>
            <strong>${escapeHtml(text)}</strong>
          </div>
        `;
      })
      .join("");
  }

  function renderRuntimeConfig() {
    if (!el.runtimeConfigDetails) return;
    const config = state.adminConfig;
    if (!config || typeof config !== "object") {
      el.runtimeConfigDetails.innerHTML = `<div><span>配置源</span><strong>未读取</strong></div>`;
      return;
    }

    const api = config.api && typeof config.api === "object" ? config.api : {};
    const rows = [
      ["配置源", config.config_path || "config.yaml"],
      ["配置版本", config.config_version ?? "--"],
      ["更新时间", config.mtime || "--"],
      ["鉴权", api.auth_enabled ? "Bearer token 已启用" : "未启用"],
      ["默认模型", config.default_model || api.model_name || "首个模型"],
      ["Thinking", formatConfigValue(api.thinking_enabled)],
      ["Subagent", formatConfigValue(api.subagent_enabled)],
      ["Plan mode", formatConfigValue(api.plan_mode)],
      ["并发子任务", formatConfigValue(api.max_concurrent_subagents)],
      ["请求超时", `${formatConfigValue(api.chat_request_timeout)} 秒`],
      ["数据目录", config.paths?.data_dir || api.data_dir || "--"],
      ["Skills", config.paths?.skills_root || "--"],
      ["Extensions", config.paths?.extensions_config || "--"],
    ];

    el.runtimeConfigDetails.innerHTML = rows
      .map(([label, value]) => {
        return `
          <div>
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value)}</strong>
          </div>
        `;
      })
      .join("");
    populateRuntimeForm(api);
  }

  function populateRuntimeForm(api) {
    if (!el.runtimeEditForm) return;
    el.runtimeModelName.value = api.model_name || "";
    el.runtimeMaxSubagents.value = api.max_concurrent_subagents ?? "";
    el.runtimeChatTimeout.value = api.chat_request_timeout ?? "";
    el.runtimeMaxUploadSize.value = api.max_upload_size_mb ?? "";
    el.runtimeMaxUploads.value = api.max_uploads_per_request ?? "";
    el.runtimeAllowedExtensions.value = Array.isArray(api.allowed_upload_extensions) ? api.allowed_upload_extensions.join(",") : "";
    el.runtimeThinking.checked = Boolean(api.thinking_enabled);
    el.runtimeSubagent.checked = Boolean(api.subagent_enabled);
    el.runtimePlanMode.checked = Boolean(api.plan_mode);
  }

  function renderFeishu() {
    if (!el.feishuStatusDetails) return;
    const data = state.feishu || {};
    const config = data.config && typeof data.config === "object" ? data.config : {};
    const runtime = data.runtime && typeof data.runtime === "object" ? data.runtime : {};
    const rows = [
      ["运行状态", runtime.running ? "运行中" : "未运行"],
      ["启用配置", runtime.enabled ? "启用" : "禁用"],
      ["配置完整", runtime.configured ? "已配置" : "未配置"],
      ["App ID", runtime.app_id || config.app_id || "--"],
    ];

    el.feishuStatusDetails.innerHTML = rows
      .map(([label, value]) => {
        return `
          <div>
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value)}</strong>
          </div>
        `;
      })
      .join("");
    populateFeishuForm(config);
  }

  function populateFeishuForm(config) {
    if (!el.feishuEditForm) return;
    el.feishuEnabled.checked = Boolean(config.enabled);
    el.feishuAppId.value = config.app_id || "";
    el.feishuAppSecret.value = secretInputValue(config.app_secret);
    el.feishuVerificationToken.value = secretInputValue(config.verification_token);
    el.feishuAppSecret.placeholder =
      config.app_secret && typeof config.app_secret === "object" && config.app_secret.configured
        ? "留空保留现有 app_secret"
        : "$FEISHU_APP_SECRET";
    el.feishuVerificationToken.placeholder =
      config.verification_token && typeof config.verification_token === "object" && config.verification_token.configured
        ? "留空保留现有 verification_token"
        : "$FEISHU_VERIFICATION_TOKEN；WS 模式可留空";
  }

  function secretPayload(value, existing) {
    const trimmed = String(value || "").trim();
    if (trimmed) return trimmed;
    if (existing !== undefined) return existing;
    return "";
  }

  function formatConfigValue(value) {
    if (value && typeof value === "object") {
      if (value.redacted) return "已配置（已脱敏）";
      if (value.source === "env_ref" && value.value) return value.value;
      if (value.configured === false) return "未配置";
      return JSON.stringify(value);
    }
    if (value === true) return "启用";
    if (value === false) return "禁用";
    if (value === null || value === undefined || value === "") return "--";
    return String(value);
  }

  function renderModels() {
    if (!state.models.length) {
      el.modelsTableBody.innerHTML = `<tr><td colspan="5">没有读取到模型配置。</td></tr>`;
      return;
    }

    el.modelsTableBody.innerHTML = state.models
      .map((model) => {
        const name = escapeHtml(model.name);
        return `
          <tr>
            <td class="mono">${name}</td>
            <td>${escapeHtml(model.display_name || model.name)}</td>
            <td>${badge(model.supports_thinking ? "支持" : "不支持", model.supports_thinking ? "ok" : "neutral")}</td>
            <td>${badge(model.supports_vision ? "支持" : "不支持", model.supports_vision ? "ok" : "neutral")}</td>
            <td>
              <div class="row-actions">
                <button class="secondary-button" data-model-action="edit" data-model-name="${name}" type="button">编辑</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function renderSkills() {
    const skills = filteredSkills();

    if (!skills.length) {
      const emptyText = state.skills.length ? "没有符合筛选条件的 skill。" : "没有读取到 skill。";
      el.skillsTableBody.innerHTML = `<tr><td colspan="5">${emptyText}</td></tr>`;
      return;
    }

    el.skillsTableBody.innerHTML = skills
      .map((skill) => {
        const enabled = Boolean(skill.enabled);
        const action = enabled ? "禁用" : "启用";
        const next = enabled ? "disable" : "enable";
        const category = skill.category || "public";
        return `
          <tr>
            <td class="mono">${escapeHtml(skill.name)}</td>
            <td class="description-cell">${escapeHtml(skill.description || "")}</td>
            <td>${badge(skillCategoryLabel(category), category === "custom" ? "ok" : "neutral")}</td>
            <td>${badge(enabled ? "已启用" : "已禁用", enabled ? "ok" : "neutral")}</td>
            <td>
              <div class="row-actions">
                <button class="${enabled ? "danger-button" : "secondary-button"}" data-skill-action="${next}" data-skill-name="${escapeHtml(skill.name)}" type="button">${action}</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function renderCustomSkillSelect() {
    if (!el.customSkillSelect) return;
    const current = el.customSkillSelect.value;
    el.customSkillSelect.innerHTML = [
      `<option value="">新建</option>`,
      ...state.customSkills.map((skill) => `<option value="${escapeHtml(skill.name)}">${escapeHtml(skill.name)}</option>`),
    ].join("");
    if (current && state.customSkills.some((skill) => skill.name === current)) {
      el.customSkillSelect.value = current;
    }
  }

  function renderMcpServerSelect() {
    if (!el.mcpServerSelect) return;
    const names = Object.keys(state.mcpServers || {}).sort();
    const current = el.mcpServerSelect.value;
    el.mcpServerSelect.innerHTML = names.length
      ? names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")
      : `<option value="">无 MCP server</option>`;
    if (current && names.includes(current)) {
      el.mcpServerSelect.value = current;
    }
  }

  function filteredSkills() {
    const category = el.skillCategoryFilter.value;
    if (!category || category === "all") {
      return state.skills;
    }
    return state.skills.filter((skill) => skill.category === category);
  }

  function skillCategoryLabel(category) {
    if (category === "custom") return "自定义";
    if (category === "public") return "内置";
    return "未知";
  }

  function badge(text, status) {
    return `<span class="status-badge ${status}">${escapeHtml(text)}</span>`;
  }

  async function toggleSkill(name, action) {
    const enabled = action === "enable";
    el.skillActionMessage.textContent = `${enabled ? "启用" : "禁用"} ${name} 中...`;
    try {
      await request(`/api/skills/${encodeURIComponent(name)}/${action}`, { method: "POST" });
      el.skillActionMessage.textContent = `${name} 已${enabled ? "启用" : "禁用"}。`;
      await loadSkills();
      renderOverview();
    } catch (error) {
      el.skillActionMessage.textContent = `操作失败：${error.message}`;
    }
  }

  async function saveMcp() {
    el.mcpMessage.textContent = "";
    let parsed;
    try {
      parsed = JSON.parse(el.mcpEditor.value || "{}");
    } catch (error) {
      el.mcpMessage.textContent = `JSON 格式错误：${error.message}`;
      return;
    }

    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      el.mcpMessage.textContent = "MCP 配置必须是 JSON 对象。";
      return;
    }

    setBusy(el.saveMcpButton, true, "保存中");
    try {
      const data = await request("/api/mcp/config", {
        method: "PUT",
        body: { mcp_servers: parsed },
      });
      state.mcpServers = data?.mcp_servers || parsed;
      el.mcpEditor.value = JSON.stringify(state.mcpServers, null, 2);
      el.mcpMessage.textContent = "MCP 配置已保存。";
      renderOverview();
    } catch (error) {
      el.mcpMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveMcpButton, false);
    }
  }

  async function reloadConfig() {
    el.runtimeMessage.textContent = "";
    setBusy(el.reloadConfigButton, true, "加载中");
    try {
      const data = await request("/api/admin/config/reload", {
        method: "POST",
        body: { include_extensions: true, reset_clients: true },
      });
      const active = data?.active_threads ? `，当前运行线程 ${data.active_threads} 个` : "";
      el.runtimeMessage.textContent = `配置已重新加载${active}。`;
      await loadAll();
    } catch (error) {
      el.runtimeMessage.textContent = `重新加载失败：${error.message}`;
    } finally {
      setBusy(el.reloadConfigButton, false);
    }
  }

  async function saveFeishuConfig() {
    el.feishuMessage.textContent = "";
    const current = state.feishu?.config && typeof state.feishu.config === "object" ? state.feishu.config : {};
    const body = {
      enabled: el.feishuEnabled.checked,
      app_id: el.feishuAppId.value.trim(),
      app_secret: secretPayload(el.feishuAppSecret.value, current.app_secret),
      verification_token: secretPayload(el.feishuVerificationToken.value, current.verification_token),
      restart: el.feishuRestartOnSave.checked,
    };
    setBusy(el.saveFeishuButton, true, "保存中");
    try {
      const data = await request("/api/admin/feishu", { method: "PUT", body });
      state.feishu = data;
      renderFeishu();
      const status = data.restart?.status ? `，channel 状态：${data.restart.status}` : "";
      el.feishuMessage.textContent = `Feishu 配置已保存${status}。`;
      await loadAdminConfig();
      showToast("Feishu 配置已保存。");
    } catch (error) {
      el.feishuMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveFeishuButton, false);
    }
  }

  async function restartFeishuChannel() {
    el.feishuMessage.textContent = "";
    setBusy(el.restartFeishuButton, true, "重启中");
    try {
      const data = await request("/api/admin/feishu/restart", { method: "POST" });
      state.feishu = data;
      renderFeishu();
      el.feishuMessage.textContent = `Feishu channel 已处理：${data.restart?.status || "ok"}。`;
      showToast("Feishu channel 已重启。");
    } catch (error) {
      el.feishuMessage.textContent = `重启失败：${error.message}`;
    } finally {
      setBusy(el.restartFeishuButton, false);
    }
  }

  async function saveRuntimeConfig() {
    el.runtimeMessage.textContent = "";
    const body = {
      model_name: el.runtimeModelName.value.trim() || null,
      allowed_upload_extensions: el.runtimeAllowedExtensions.value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
      thinking_enabled: el.runtimeThinking.checked,
      subagent_enabled: el.runtimeSubagent.checked,
      plan_mode: el.runtimePlanMode.checked,
      reload: true,
    };
    addNumberField(body, "max_concurrent_subagents", el.runtimeMaxSubagents.value);
    addNumberField(body, "chat_request_timeout", el.runtimeChatTimeout.value);
    addNumberField(body, "max_upload_size_mb", el.runtimeMaxUploadSize.value);
    addNumberField(body, "max_uploads_per_request", el.runtimeMaxUploads.value);
    setBusy(el.saveRuntimeButton, true, "保存中");
    try {
      const data = await request("/api/admin/runtime", { method: "PATCH", body });
      const restartFields = Object.entries(data.effects || {})
        .filter(([, effect]) => effect === "requires_restart")
        .map(([field]) => field);
      el.runtimeMessage.textContent = restartFields.length
        ? `已保存；${restartFields.join(", ")} 需要重启生效。`
        : "运行配置已保存并应用到新请求。";
      await loadAdminConfig();
    } catch (error) {
      el.runtimeMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveRuntimeButton, false);
    }
  }

  function addNumberField(target, key, value) {
    const trimmed = String(value || "").trim();
    if (!trimmed) return;
    const number = Number(trimmed);
    if (Number.isFinite(number)) target[key] = number;
  }

  async function setMcpEnabled(enabled) {
    const name = el.mcpServerSelect.value;
    if (!name) {
      el.mcpMessage.textContent = "请选择 MCP server。";
      return;
    }
    const button = enabled ? el.enableMcpButton : el.disableMcpButton;
    setBusy(button, true, enabled ? "启用中" : "禁用中");
    try {
      await request(`/api/admin/mcp/${encodeURIComponent(name)}/${enabled ? "enable" : "disable"}`, { method: "POST" });
      el.mcpMessage.textContent = `${name} 已${enabled ? "启用" : "禁用"}。`;
      await loadMcp();
      renderOverview();
    } catch (error) {
      el.mcpMessage.textContent = `操作失败：${error.message}`;
    } finally {
      setBusy(button, false);
    }
  }

  async function testMcpServer() {
    const name = el.mcpServerSelect.value;
    if (!name) {
      el.mcpMessage.textContent = "请选择 MCP server。";
      return;
    }
    setBusy(el.testMcpButton, true, "测试中");
    try {
      const data = await request(`/api/admin/mcp/${encodeURIComponent(name)}/test`, {
        method: "POST",
        body: { timeout_seconds: 5 },
      });
      const result = data.result || {};
      el.mcpMessage.textContent = `${name} 测试结果：${result.status || (data.success ? "ok" : "failed")}`;
    } catch (error) {
      el.mcpMessage.textContent = `测试失败：${error.message}`;
    } finally {
      setBusy(el.testMcpButton, false);
    }
  }

  function setView(viewName) {
    if (!views[viewName]) return;
    state.currentView = viewName;
    document.querySelectorAll("[data-view-target]").forEach((button) => {
      button.classList.toggle("active", button.dataset.viewTarget === viewName);
    });
    document.querySelectorAll(".view-section").forEach((section) => {
      section.classList.toggle("active", section.id === `${viewName}View`);
    });
    el.viewTitle.textContent = views[viewName];
  }

  function yamlValue(value) {
    const trimmed = String(value ?? "").trim();
    if (!trimmed) return "null";
    if (trimmed.startsWith("$")) return trimmed;
    return JSON.stringify(trimmed);
  }

  function populateModelUseSelect() {
    el.draftUse.innerHTML = [
      ...modelUseGroups.map((group) => {
        const options = group.options
          .map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`)
          .join("");
        return `<optgroup label="${escapeHtml(group.label)}">${options}</optgroup>`;
      }),
      `<option value="${customModelUseValue}">自定义类路径</option>`,
    ].join("");
  }

  function knownModelUse(value) {
    return modelUseGroups.some((group) => group.options.some((option) => option.value === value));
  }

  function updateModelUseCustomVisibility() {
    const custom = el.draftUse.value === customModelUseValue;
    el.draftUseCustomRow.classList.toggle("hidden", !custom);
    el.draftUseCustom.required = custom;
  }

  function setModelUse(value) {
    const use = String(value || defaultModelUse).trim();
    if (knownModelUse(use)) {
      el.draftUse.value = use;
      el.draftUseCustom.value = "";
    } else {
      el.draftUse.value = customModelUseValue;
      el.draftUseCustom.value = use;
    }
    updateModelUseCustomVisibility();
  }

  function modelUseValue() {
    if (el.draftUse.value === customModelUseValue) {
      return el.draftUseCustom.value.trim();
    }
    return el.draftUse.value.trim();
  }

  function updateModelDraft() {
    const name = el.draftModelName.value.trim() || "new-model";
    const displayName = el.draftDisplayName.value.trim() || name;
    const use = modelUseValue() || (el.draftUse.value === customModelUseValue ? "package.module:ClassName" : defaultModelUse);
    const model = el.draftModelId.value.trim() || name;
    const baseUrl = el.draftBaseUrl.value.trim();
    const editingModel = getEditableModel(state.editingModelName);
    const apiKey = el.draftApiKey.value.trim();
    const lines = [
      `- name: ${yamlValue(name)}`,
      `  display_name: ${yamlValue(displayName)}`,
      `  use: ${yamlValue(use)}`,
      `  model: ${yamlValue(model)}`,
    ];
    if (apiKey) {
      lines.push(`  api_key: ${yamlValue(apiKey)}`);
    } else if (state.editingModelName) {
      lines.push(
        isConfiguredSecret(editingModel?.api_key)
          ? "  # api_key 留空保存时会保留现有值"
          : "  # api_key 未配置；填写后会写入",
      );
    } else {
      lines.push(`  api_key: ${yamlValue("$MODEL_API_KEY")}`);
    }
    if (baseUrl) lines.push(`  base_url: ${yamlValue(baseUrl)}`);
    lines.push(`  supports_thinking: ${el.draftThinking.checked ? "true" : "false"}`);
    lines.push(`  supports_vision: ${el.draftVision.checked ? "true" : "false"}`);
    el.modelYamlPreview.textContent = lines.join("\n");
  }

  function getEditableModel(name) {
    if (!name || !Array.isArray(state.adminConfig?.models)) return null;
    return state.adminConfig.models.find((model) => model && model.name === name) || null;
  }

  function isConfiguredSecret(value) {
    return Boolean(value && typeof value === "object" && value.configured !== false && "redacted" in value);
  }

  function hasConfigField(config, field) {
    return Boolean(config && Object.prototype.hasOwnProperty.call(config, field));
  }

  function secretInputValue(value) {
    if (typeof value === "string") return value;
    if (value && typeof value === "object" && value.source === "env_ref" && value.value) {
      return String(value.value);
    }
    return "";
  }

  function resetModelDraft() {
    state.editingModelName = null;
    el.modelDraftForm.reset();
    setModelUse(defaultModelUse);
    el.draftApiKey.placeholder = "$DASHSCOPE_API_KEY；编辑时留空保留原值";
    el.modelDraftMessage.textContent = "";
    updateModelDraft();
  }

  async function openModelEditor(name) {
    if (!state.adminConfig) {
      await loadAdminConfig();
    }
    const model = getEditableModel(name);
    if (!model) {
      showToast(`未找到模型配置：${name}`);
      return;
    }

    state.editingModelName = model.name;
    el.draftModelName.value = model.name || "";
    el.draftDisplayName.value = model.display_name || model.name || "";
    setModelUse(model.use || defaultModelUse);
    el.draftModelId.value = model.model || model.name || "";
    el.draftBaseUrl.value = model.base_url || "";
    el.draftApiKey.value = secretInputValue(model.api_key);
    el.draftThinking.checked = Boolean(model.supports_thinking);
    el.draftVision.checked = Boolean(model.supports_vision);
    el.draftApiKey.placeholder = isConfiguredSecret(model.api_key) ? "留空保留现有 api_key" : "$DASHSCOPE_API_KEY";
    el.modelDraftMessage.textContent = `正在编辑 ${model.name}。api_key 留空会保留原值。`;
    el.modelDraftPanel.classList.remove("hidden");
    updateModelDraft();
    el.modelDraftPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function modelDraftPayload(existingModel) {
    const name = el.draftModelName.value.trim();
    if (!name) {
      throw new Error("name 不能为空。");
    }
    const displayName = el.draftDisplayName.value.trim() || name;
    const use = modelUseValue();
    if (!use) {
      throw new Error("use 不能为空。");
    }
    const model = el.draftModelId.value.trim() || name;
    const baseUrl = el.draftBaseUrl.value.trim();
    const apiKey = el.draftApiKey.value.trim();
    const payload = {
      name,
      display_name: displayName,
      use,
      model,
      supports_thinking: el.draftThinking.checked,
      supports_vision: el.draftVision.checked,
    };
    if (baseUrl) payload.base_url = baseUrl;
    if (apiKey) {
      payload.api_key = apiKey;
    } else if (hasConfigField(existingModel, "api_key")) {
      payload.api_key = existingModel.api_key;
    } else if (!existingModel) {
      payload.api_key = "$MODEL_API_KEY";
    }
    return payload;
  }

  async function saveModelDraft() {
    el.modelDraftMessage.textContent = "";
    if (!state.adminConfig) {
      await loadAdminConfig();
    }

    const currentModels = Array.isArray(state.adminConfig?.models) ? [...state.adminConfig.models] : [];
    const name = el.draftModelName.value.trim();
    const originalName = state.editingModelName || name;
    const index = currentModels.findIndex((model) => model && model.name === originalName);
    let payload;
    try {
      payload = modelDraftPayload(index >= 0 ? currentModels[index] : null);
    } catch (error) {
      el.modelDraftMessage.textContent = error.message;
      return;
    }

    if (index >= 0) {
      currentModels[index] = { ...currentModels[index], ...payload };
    } else {
      currentModels.push(payload);
    }

    const modelNames = currentModels.map((model) => model?.name).filter(Boolean);
    let defaultModel = state.adminConfig?.default_model || currentModels[0]?.name || payload.name;
    if (state.editingModelName && defaultModel === state.editingModelName) {
      defaultModel = payload.name;
    } else if (!modelNames.includes(defaultModel)) {
      defaultModel = currentModels[0]?.name || payload.name;
    }
    setBusy(el.saveModelButton, true, "保存中");
    try {
      await request("/api/admin/models", {
        method: "PUT",
        body: {
          models: currentModels,
          default_model: defaultModel,
          reload: true,
        },
      });
      el.modelDraftMessage.textContent = `${payload.name} 已保存并重新加载配置。`;
      await Promise.all([loadAdminConfig(), loadModels()]);
      state.editingModelName = payload.name;
      renderOverview();
      showToast("模型配置已保存。");
    } catch (error) {
      el.modelDraftMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveModelButton, false);
    }
  }

  function updateSkillDraft() {
    const name = slugify(el.draftSkillName.value.trim() || "new-skill");
    const description = el.draftSkillDescription.value.trim() || "描述这个 skill 适合处理的任务。";
    el.skillMarkdownEditor.value = [
      "---",
      `name: ${name}`,
      `description: ${description}`,
      "---",
      "",
      `Use this skill when the user needs ${description}`,
      "",
      "## Workflow",
      "- Clarify the user's target outcome when requirements are ambiguous.",
      "- Gather the relevant context and source material.",
      "- Produce the result in the format requested by the user.",
    ].join("\n");
  }

  async function loadSelectedCustomSkill() {
    const name = el.customSkillSelect.value;
    el.skillDraftMessage.textContent = "";
    el.skillHistoryPreview.textContent = "";
    if (!name) {
      el.draftSkillName.value = "";
      el.draftSkillDescription.value = "";
      el.draftSkillEnabled.checked = true;
      updateSkillDraft();
      return;
    }
    try {
      const data = await request(`/api/admin/skills/custom/${encodeURIComponent(name)}`);
      el.draftSkillName.value = data.name || name;
      el.draftSkillDescription.value = data.description || "";
      el.draftSkillEnabled.checked = Boolean(data.enabled);
      el.skillMarkdownEditor.value = data.content || "";
      el.skillDraftMessage.textContent = Array.isArray(data.files) && data.files.length ? `支持文件：${data.files.join(", ")}` : "";
    } catch (error) {
      el.skillDraftMessage.textContent = `读取失败：${error.message}`;
    }
  }

  async function saveSkillDraft() {
    const name = slugify(el.draftSkillName.value.trim());
    const content = el.skillMarkdownEditor.value;
    if (!name) {
      el.skillDraftMessage.textContent = "name 不能为空。";
      return;
    }
    setBusy(el.saveSkillButton, true, "保存中");
    try {
      await request(`/api/admin/skills/custom/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: {
          content,
          enabled: el.draftSkillEnabled.checked,
          reload: true,
        },
      });
      el.skillDraftMessage.textContent = `${name} 已保存。`;
      await Promise.all([loadSkills(), loadCustomSkills()]);
      el.customSkillSelect.value = name;
      renderOverview();
      showToast("自定义 skill 已保存。");
    } catch (error) {
      el.skillDraftMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveSkillButton, false);
    }
  }

  async function deleteSkillDraft() {
    const name = slugify(el.draftSkillName.value.trim());
    if (!name) {
      el.skillDraftMessage.textContent = "name 不能为空。";
      return;
    }
    setBusy(el.deleteSkillButton, true, "删除中");
    try {
      await request(`/api/admin/skills/custom/${encodeURIComponent(name)}`, { method: "DELETE" });
      el.skillDraftMessage.textContent = `${name} 已删除。`;
      el.customSkillSelect.value = "";
      updateSkillDraft();
      await Promise.all([loadSkills(), loadCustomSkills()]);
      renderOverview();
    } catch (error) {
      el.skillDraftMessage.textContent = `删除失败：${error.message}`;
    } finally {
      setBusy(el.deleteSkillButton, false);
    }
  }

  async function loadSkillHistory() {
    const name = slugify(el.draftSkillName.value.trim());
    if (!name) {
      el.skillDraftMessage.textContent = "name 不能为空。";
      return;
    }
    try {
      const data = await request(`/api/admin/skills/custom/${encodeURIComponent(name)}/history`);
      el.skillHistoryPreview.textContent = JSON.stringify(data.history || [], null, 2);
    } catch (error) {
      el.skillDraftMessage.textContent = `历史读取失败：${error.message}`;
    }
  }

  async function writeSupportFile() {
    const name = slugify(el.draftSkillName.value.trim());
    const path = el.supportFilePath.value.trim();
    if (!name || !path) {
      el.skillDraftMessage.textContent = "name 和支持文件路径不能为空。";
      return;
    }
    setBusy(el.writeSupportFileButton, true, "写入中");
    try {
      await request(`/api/admin/skills/custom/${encodeURIComponent(name)}/files/${encodePath(path)}`, {
        method: "PUT",
        body: { content: el.supportFileContent.value, reload: false },
      });
      el.skillDraftMessage.textContent = `${path} 已写入。`;
      await loadSelectedCustomSkill();
    } catch (error) {
      el.skillDraftMessage.textContent = `写入失败：${error.message}`;
    } finally {
      setBusy(el.writeSupportFileButton, false);
    }
  }

  async function deleteSupportFile() {
    const name = slugify(el.draftSkillName.value.trim());
    const path = el.supportFilePath.value.trim();
    if (!name || !path) {
      el.skillDraftMessage.textContent = "name 和支持文件路径不能为空。";
      return;
    }
    setBusy(el.deleteSupportFileButton, true, "删除中");
    try {
      await request(`/api/admin/skills/custom/${encodeURIComponent(name)}/files/${encodePath(path)}`, { method: "DELETE" });
      el.skillDraftMessage.textContent = `${path} 已删除。`;
      await loadSelectedCustomSkill();
    } catch (error) {
      el.skillDraftMessage.textContent = `删除失败：${error.message}`;
    } finally {
      setBusy(el.deleteSupportFileButton, false);
    }
  }

  function encodePath(path) {
    return path
      .split("/")
      .map((part) => encodeURIComponent(part))
      .join("/");
  }

  function slugify(value) {
    return String(value || "new-skill")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 64) || "new-skill";
  }

  async function copyText(text, successMessage) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        textarea.remove();
      }
      showToast(successMessage);
    } catch (error) {
      showToast(`复制失败：${error.message}`);
    }
  }

  function wireEvents() {
    el.loginForm.addEventListener("submit", verifyLogin);
    el.logoutButton.addEventListener("click", logout);
    el.refreshButton.addEventListener("click", loadAll);
    el.enabledOnlyToggle.addEventListener("change", loadSkills);
    el.skillCategoryFilter.addEventListener("change", () => {
      renderSkills();
      renderOverview();
    });
    el.saveMcpButton.addEventListener("click", saveMcp);
    el.enableMcpButton.addEventListener("click", () => setMcpEnabled(true));
    el.disableMcpButton.addEventListener("click", () => setMcpEnabled(false));
    el.testMcpButton.addEventListener("click", testMcpServer);
    el.reloadConfigButton.addEventListener("click", reloadConfig);
    el.saveRuntimeButton.addEventListener("click", saveRuntimeConfig);
    el.saveFeishuButton.addEventListener("click", saveFeishuConfig);
    el.restartFeishuButton.addEventListener("click", restartFeishuChannel);

    document.querySelectorAll("[data-view-target]").forEach((button) => {
      button.addEventListener("click", () => setView(button.dataset.viewTarget));
    });

    el.skillsTableBody.addEventListener("click", (event) => {
      const button = event.target.closest("[data-skill-action]");
      if (!button) return;
      toggleSkill(button.dataset.skillName, button.dataset.skillAction);
    });

    el.modelsTableBody.addEventListener("click", (event) => {
      const button = event.target.closest("[data-model-action]");
      if (!button) return;
      if (button.dataset.modelAction === "edit") {
        openModelEditor(button.dataset.modelName).catch((error) => {
          showToast(`读取模型配置失败：${error.message}`);
        });
      }
    });

    el.openModelDraftButton.addEventListener("click", () => {
      el.modelDraftPanel.classList.remove("hidden");
      resetModelDraft();
    });
    el.closeModelDraftButton.addEventListener("click", () => el.modelDraftPanel.classList.add("hidden"));
    el.modelDraftForm.addEventListener("input", updateModelDraft);
    el.draftUse.addEventListener("change", () => {
      updateModelUseCustomVisibility();
      updateModelDraft();
      if (el.draftUse.value === customModelUseValue) {
        el.draftUseCustom.focus();
      }
    });
    el.saveModelButton.addEventListener("click", saveModelDraft);
    el.copyModelYamlButton.addEventListener("click", () => copyText(el.modelYamlPreview.textContent, "模型配置片段已复制。"));

    el.openSkillDraftButton.addEventListener("click", () => {
      el.skillDraftPanel.classList.remove("hidden");
      updateSkillDraft();
    });
    el.closeSkillDraftButton.addEventListener("click", () => el.skillDraftPanel.classList.add("hidden"));
    [el.draftSkillName, el.draftSkillDescription].forEach((input) => {
      input.addEventListener("input", () => {
        if (!el.customSkillSelect.value) updateSkillDraft();
      });
    });
    el.customSkillSelect.addEventListener("change", loadSelectedCustomSkill);
    el.saveSkillButton.addEventListener("click", saveSkillDraft);
    el.deleteSkillButton.addEventListener("click", deleteSkillDraft);
    el.refreshCustomSkillsButton.addEventListener("click", loadCustomSkills);
    el.loadSkillHistoryButton.addEventListener("click", loadSkillHistory);
    el.writeSupportFileButton.addEventListener("click", writeSupportFile);
    el.deleteSupportFileButton.addEventListener("click", deleteSupportFile);
    el.copySkillMarkdownButton.addEventListener("click", () => copyText(el.skillMarkdownEditor.value, "Skill 草稿已复制。"));
  }

  function boot() {
    const savedToken = sessionStorage.getItem(storage.token) || "";
    state.baseUrl = normalizeBaseUrl(defaultBaseUrl());
    state.token = savedToken;
    el.tokenInput.value = savedToken;
    populateModelUseSelect();
    setModelUse(defaultModelUse);
    updateModelDraft();
    updateSkillDraft();
    renderRuntimeConfig();
    wireEvents();

    if (sessionStorage.getItem(storage.loggedIn) === "1") {
      showApp();
      loadAll();
    } else {
      showLogin();
    }
  }

  boot();
})();
