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
    titleConfig: null,
    subagentsConfig: null,
    memoryConfig: null,
    summarizationConfig: null,
    configHealth: null,
    scheduledTasks: [],
    schedulerStatus: null,
    threadCleanupConfig: null,
    threadCleanupStatus: null,
    threadCleanupPreview: null,
    threadCleanupPollTimer: null,
    feishu: null,
    customSkills: [],
    evolutionStatus: null,
    evolutionProposals: [],
    evolutionSignals: [],
    selectedEvolutionProposal: null,
    selectedEvolutionSignal: null,
    evolutionSignalTrigger: null,
    currentView: "overview",
    editingModelName: null,
    modelReloadSequence: 0,
  };

  const views = {
    overview: "总览",
    models: "大模型",
    agent: "Agent 配置",
    skills: "Skills",
    mcp: "MCP",
    feishu: "Feishu",
    scheduler: "定时任务",
    storage: "存储维护",
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
    draftClearApiKeyRow: document.getElementById("draftClearApiKeyRow"),
    draftClearApiKey: document.getElementById("draftClearApiKey"),
    draftThinking: document.getElementById("draftThinking"),
    draftReasoningEffort: document.getElementById("draftReasoningEffort"),
    draftVision: document.getElementById("draftVision"),
    draftSetDefault: document.getElementById("draftSetDefault"),
    draftModelAdvanced: document.getElementById("draftModelAdvanced"),
    titleEditForm: document.getElementById("titleEditForm"),
    saveTitleButton: document.getElementById("saveTitleButton"),
    titleEnabled: document.getElementById("titleEnabled"),
    titleModelName: document.getElementById("titleModelName"),
    titleMaxWords: document.getElementById("titleMaxWords"),
    titleMaxChars: document.getElementById("titleMaxChars"),
    titlePromptTemplate: document.getElementById("titlePromptTemplate"),
    titleMessage: document.getElementById("titleMessage"),
    subagentsEditForm: document.getElementById("subagentsEditForm"),
    saveSubagentsButton: document.getElementById("saveSubagentsButton"),
    subagentsEnabled: document.getElementById("subagentsEnabled"),
    subagentsTimeout: document.getElementById("subagentsTimeout"),
    subagentsMaxTurns: document.getElementById("subagentsMaxTurns"),
    subagentsAgentsEditor: document.getElementById("subagentsAgentsEditor"),
    subagentsCustomEditor: document.getElementById("subagentsCustomEditor"),
    subagentsMessage: document.getElementById("subagentsMessage"),
    agentSystemSummary: document.getElementById("agentSystemSummary"),
    memoryEditForm: document.getElementById("memoryEditForm"),
    saveMemoryButton: document.getElementById("saveMemoryButton"),
    memoryEnabled: document.getElementById("memoryEnabled"),
    memoryInjectionEnabled: document.getElementById("memoryInjectionEnabled"),
    memoryModelName: document.getElementById("memoryModelName"),
    memoryDebounce: document.getElementById("memoryDebounce"),
    memoryMaxFacts: document.getElementById("memoryMaxFacts"),
    memoryConfidence: document.getElementById("memoryConfidence"),
    memoryMaxInjectionTokens: document.getElementById("memoryMaxInjectionTokens"),
    memoryStoragePath: document.getElementById("memoryStoragePath"),
    memoryStorageClass: document.getElementById("memoryStorageClass"),
    memoryMessage: document.getElementById("memoryMessage"),
    summarizationEditForm: document.getElementById("summarizationEditForm"),
    saveSummarizationButton: document.getElementById("saveSummarizationButton"),
    summarizationEnabled: document.getElementById("summarizationEnabled"),
    summarizationModelName: document.getElementById("summarizationModelName"),
    summarizationTrigger: document.getElementById("summarizationTrigger"),
    summarizationKeepType: document.getElementById("summarizationKeepType"),
    summarizationKeepValue: document.getElementById("summarizationKeepValue"),
    summarizationTrimTokens: document.getElementById("summarizationTrimTokens"),
    summarizationSkillCount: document.getElementById("summarizationSkillCount"),
    summarizationSkillTokens: document.getElementById("summarizationSkillTokens"),
    summarizationSkillTokensPerSkill: document.getElementById("summarizationSkillTokensPerSkill"),
    summarizationSkillTools: document.getElementById("summarizationSkillTools"),
    summarizationPrompt: document.getElementById("summarizationPrompt"),
    summarizationMessage: document.getElementById("summarizationMessage"),
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
    evolutionStatusSummary: document.getElementById("evolutionStatusSummary"),
    evolutionProposalsTableBody: document.getElementById("evolutionProposalsTableBody"),
    proposalStatusFilter: document.getElementById("proposalStatusFilter"),
    proposalArchiveFilter: document.getElementById("proposalArchiveFilter"),
    refreshEvolutionButton: document.getElementById("refreshEvolutionButton"),
    evolutionActionMessage: document.getElementById("evolutionActionMessage"),
    evolutionProposalPanel: document.getElementById("evolutionProposalPanel"),
    evolutionProposalTitle: document.getElementById("evolutionProposalTitle"),
    evolutionProposalSubtitle: document.getElementById("evolutionProposalSubtitle"),
    evolutionProposalDetails: document.getElementById("evolutionProposalDetails"),
    evolutionProposalDiff: document.getElementById("evolutionProposalDiff"),
    evolutionProposalScans: document.getElementById("evolutionProposalScans"),
    evolutionProposalEvaluation: document.getElementById("evolutionProposalEvaluation"),
    evolutionSignalsTableBody: document.getElementById("evolutionSignalsTableBody"),
    evolutionSignalPanel: document.getElementById("evolutionSignalPanel"),
    evolutionSignalTitle: document.getElementById("evolutionSignalTitle"),
    evolutionSignalSubtitle: document.getElementById("evolutionSignalSubtitle"),
    evolutionSignalDetails: document.getElementById("evolutionSignalDetails"),
    evolutionSignalToolErrors: document.getElementById("evolutionSignalToolErrors"),
    evolutionSignalSummary: document.getElementById("evolutionSignalSummary"),
    evolutionSignalProcessError: document.getElementById("evolutionSignalProcessError"),
    evolutionSignalMessage: document.getElementById("evolutionSignalMessage"),
    closeEvolutionSignalButton: document.getElementById("closeEvolutionSignalButton"),
    evolutionProbationsPreview: document.getElementById("evolutionProbationsPreview"),
    evolutionReviewNote: document.getElementById("evolutionReviewNote"),
    evolutionReviewMessage: document.getElementById("evolutionReviewMessage"),
    approveEvolutionProposalButton: document.getElementById("approveEvolutionProposalButton"),
    rejectEvolutionProposalButton: document.getElementById("rejectEvolutionProposalButton"),
    closeEvolutionProposalButton: document.getElementById("closeEvolutionProposalButton"),
    loadSkillRevisionsButton: document.getElementById("loadSkillRevisionsButton"),
    skillRevisionsPanel: document.getElementById("skillRevisionsPanel"),
    skillRevisionsList: document.getElementById("skillRevisionsList"),
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
    runtimeSchedulerEnabled: document.getElementById("runtimeSchedulerEnabled"),
    runtimeSchedulerPoll: document.getElementById("runtimeSchedulerPoll"),
    runtimeSchedulerTimezone: document.getElementById("runtimeSchedulerTimezone"),
    refreshConfigHealthButton: document.getElementById("refreshConfigHealthButton"),
    configHealthDetails: document.getElementById("configHealthDetails"),
    configHealthWarnings: document.getElementById("configHealthWarnings"),
    configHealthMessage: document.getElementById("configHealthMessage"),
    refreshScheduledTasksButton: document.getElementById("refreshScheduledTasksButton"),
    scheduledTasksSummary: document.getElementById("scheduledTasksSummary"),
    scheduledTasksTableBody: document.getElementById("scheduledTasksTableBody"),
    scheduledTasksMessage: document.getElementById("scheduledTasksMessage"),
    threadCleanupForm: document.getElementById("threadCleanupForm"),
    threadCleanupEnabled: document.getElementById("threadCleanupEnabled"),
    threadCleanupInactiveDays: document.getElementById("threadCleanupInactiveDays"),
    threadCleanupDailyAt: document.getElementById("threadCleanupDailyAt"),
    threadCleanupTimezone: document.getElementById("threadCleanupTimezone"),
    threadCleanupBatchSize: document.getElementById("threadCleanupBatchSize"),
    threadCleanupBatchInterval: document.getElementById("threadCleanupBatchInterval"),
    threadCleanupMaxDeletes: document.getElementById("threadCleanupMaxDeletes"),
    threadCleanupQuietPeriod: document.getElementById("threadCleanupQuietPeriod"),
    threadCleanupPostpone: document.getElementById("threadCleanupPostpone"),
    threadCleanupProtectScheduled: document.getElementById("threadCleanupProtectScheduled"),
    threadCleanupStopOnActivity: document.getElementById("threadCleanupStopOnActivity"),
    saveThreadCleanupButton: document.getElementById("saveThreadCleanupButton"),
    refreshThreadCleanupButton: document.getElementById("refreshThreadCleanupButton"),
    previewThreadCleanupButton: document.getElementById("previewThreadCleanupButton"),
    runThreadCleanupButton: document.getElementById("runThreadCleanupButton"),
    threadCleanupMessage: document.getElementById("threadCleanupMessage"),
    threadCleanupDatabaseDetails: document.getElementById("threadCleanupDatabaseDetails"),
    threadCleanupRunDetails: document.getElementById("threadCleanupRunDetails"),
    threadCleanupCandidatesBody: document.getElementById("threadCleanupCandidatesBody"),
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
    if (!button.dataset.idleText) {
      button.dataset.idleText = button.textContent;
    }
    button.disabled = busy;
    button.classList.toggle("is-busy", busy);
    if (busy) {
      button.setAttribute("aria-busy", "true");
      if (text) button.textContent = text;
    } else {
      button.removeAttribute("aria-busy");
      button.textContent = button.dataset.idleText;
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
    const timeoutMs = Number(options.timeoutMs);
    let timeoutId = null;

    if (Number.isFinite(timeoutMs) && timeoutMs > 0 && window.AbortController) {
      const controller = new AbortController();
      init.signal = controller.signal;
      timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    }

    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }

    try {
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
    } catch (error) {
      if (error.name === "AbortError") {
        const timeoutError = new Error("请求超时");
        timeoutError.name = "TimeoutError";
        throw timeoutError;
      }
      throw error;
    } finally {
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
    }
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
      loadTitleConfig(),
      loadSubagentsConfig(),
      loadMemoryConfig(),
      loadSummarizationConfig(),
      loadConfigHealth(),
      loadScheduledTasks(),
      loadThreadCleanupConfig(),
      loadThreadCleanupStatus(),
      loadHealth(),
      loadModels(),
      loadSkills(),
      loadCustomSkills(),
      loadEvolutionStatus(),
      loadEvolutionProposals(),
      loadEvolutionSignals(),
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
    renderAgentConfig();
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
    renderAgentSystemSummary();
    return data;
  }

  async function loadTitleConfig() {
    const data = await request("/api/admin/title");
    state.titleConfig = data?.config && typeof data.config === "object" ? data.config : {};
    renderTitleConfig();
    return data;
  }

  async function loadSubagentsConfig() {
    const data = await request("/api/admin/subagents");
    state.subagentsConfig = data?.config && typeof data.config === "object" ? data.config : {};
    renderSubagentsConfig();
    return data;
  }

  async function loadMemoryConfig() {
    const data = await request("/api/admin/memory");
    state.memoryConfig = data?.config && typeof data.config === "object" ? data.config : {};
    renderMemoryConfig();
    return data;
  }

  async function loadSummarizationConfig() {
    const data = await request("/api/admin/summarization");
    state.summarizationConfig = data?.config && typeof data.config === "object" ? data.config : {};
    renderSummarizationConfig();
    return data;
  }

  async function loadConfigHealth() {
    if (el.configHealthMessage) el.configHealthMessage.textContent = "";
    const data = await request("/api/admin/config/health");
    state.configHealth = data;
    renderConfigHealth();
    return data;
  }

  async function loadScheduledTasks() {
    const data = await request("/api/admin/scheduled-tasks?include_disabled=true&limit=200");
    state.scheduledTasks = Array.isArray(data?.tasks) ? data.tasks : [];
    state.schedulerStatus = {
      enabled: Boolean(data?.scheduler_enabled),
      storageExists: Boolean(data?.storage_exists),
    };
    renderScheduledTasks();
    return data;
  }

  async function loadThreadCleanupConfig() {
    const data = await request("/api/admin/thread-cleanup/config");
    state.threadCleanupConfig = data?.config && typeof data.config === "object" ? data.config : {};
    renderThreadCleanup();
    return data;
  }

  async function loadThreadCleanupStatus() {
    const data = await request("/api/admin/thread-cleanup/status", { timeoutMs: 120000 });
    state.threadCleanupStatus = data;
    renderThreadCleanup();
    scheduleThreadCleanupPoll();
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
    refreshAgentModelSelectors();
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

  async function loadEvolutionStatus() {
    const data = await request("/api/admin/evolution/status");
    state.evolutionStatus = data;
    renderEvolutionStatus();
    return data;
  }

  async function loadEvolutionProposals() {
    const status = el.proposalStatusFilter?.value;
    const archiveScope = el.proposalArchiveFilter?.value || "current";
    const params = new URLSearchParams();
    if (status && status !== "all") params.set("status", status);
    if (archiveScope === "archived") params.set("archived_only", "true");
    if (archiveScope === "all") params.set("include_archived", "true");
    const query = params.toString();
    const data = await request(`/api/admin/evolution/proposals${query ? `?${query}` : ""}`);
    state.evolutionProposals = Array.isArray(data?.proposals) ? data.proposals : [];
    renderEvolutionProposals();
    return data;
  }

  async function loadEvolutionSignals() {
    const data = await request("/api/admin/evolution/signals?limit=50");
    state.evolutionSignals = Array.isArray(data?.signals) ? data.signals : [];
    renderEvolutionSignals();
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

  function modelOptions(selected, includeDefault = true) {
    const options = [];
    if (includeDefault) options.push(`<option value="">使用默认模型</option>`);
    state.models.forEach((model) => {
      const value = String(model.name || "");
      options.push(`<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(model.display_name || value)}</option>`);
    });
    if (selected && !state.models.some((model) => model.name === selected)) {
      options.push(`<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)}（当前配置）</option>`);
    }
    return options.join("");
  }

  function renderAgentConfig() {
    renderTitleConfig();
    renderSubagentsConfig();
    renderMemoryConfig();
    renderSummarizationConfig();
  }

  function refreshAgentModelSelectors() {
    const fields = [
      [el.titleModelName, state.titleConfig?.model_name],
      [el.memoryModelName, state.memoryConfig?.model_name],
      [el.summarizationModelName, state.summarizationConfig?.model_name],
    ];
    fields.forEach(([field, configured]) => {
      if (!field) return;
      const selected = field.value || configured || "";
      field.innerHTML = modelOptions(selected);
    });
  }

  function renderTitleConfig() {
    const title = state.titleConfig;
    if (title && el.titleEditForm) {
      el.titleEnabled.checked = Boolean(title.enabled);
      el.titleModelName.innerHTML = modelOptions(title.model_name || "");
      el.titleMaxWords.value = title.max_words ?? 6;
      el.titleMaxChars.value = title.max_chars ?? 60;
      el.titlePromptTemplate.value = title.prompt_template || "";
    }
  }

  function renderSubagentsConfig() {
    const subagents = state.subagentsConfig;
    if (subagents && el.subagentsEditForm) {
      el.subagentsEnabled.checked = Boolean(subagents.enabled);
      el.subagentsTimeout.value = subagents.timeout_seconds ?? 900;
      el.subagentsMaxTurns.value = subagents.max_turns ?? "";
      el.subagentsAgentsEditor.value = JSON.stringify(subagents.agents || {}, null, 2);
      el.subagentsCustomEditor.value = JSON.stringify(subagents.custom_agents || {}, null, 2);
    }
  }

  function renderMemoryConfig() {
    const config = state.memoryConfig;
    if (!config || !el.memoryEditForm) return;
    el.memoryEnabled.checked = Boolean(config.enabled);
    el.memoryInjectionEnabled.checked = Boolean(config.injection_enabled);
    el.memoryModelName.innerHTML = modelOptions(config.model_name || "");
    el.memoryDebounce.value = config.debounce_seconds ?? 30;
    el.memoryMaxFacts.value = config.max_facts ?? 100;
    el.memoryConfidence.value = config.fact_confidence_threshold ?? 0.7;
    el.memoryMaxInjectionTokens.value = config.max_injection_tokens ?? 2000;
    el.memoryStoragePath.value = config.storage_path || "";
    el.memoryStorageClass.value = config.storage_class || "";
  }

  function renderSummarizationConfig() {
    const config = state.summarizationConfig;
    if (!config || !el.summarizationEditForm) return;
    const keep = config.keep || { type: "messages", value: 20 };
    el.summarizationEnabled.checked = Boolean(config.enabled);
    el.summarizationModelName.innerHTML = modelOptions(config.model_name || "");
    el.summarizationTrigger.value = JSON.stringify(config.trigger ?? null, null, 2);
    el.summarizationKeepType.value = keep.type || "messages";
    el.summarizationKeepValue.value = keep.value ?? 20;
    el.summarizationTrimTokens.value = config.trim_tokens_to_summarize ?? "";
    el.summarizationSkillCount.value = config.preserve_recent_skill_count ?? 5;
    el.summarizationSkillTokens.value = config.preserve_recent_skill_tokens ?? 25000;
    el.summarizationSkillTokensPerSkill.value = config.preserve_recent_skill_tokens_per_skill ?? 5000;
    el.summarizationSkillTools.value = Array.isArray(config.skill_file_read_tool_names)
      ? config.skill_file_read_tool_names.join(",")
      : "";
    el.summarizationPrompt.value = config.summary_prompt || "";
  }

  function renderConfigHealth() {
    if (!el.configHealthDetails || !el.configHealthWarnings) return;
    const health = state.configHealth;
    if (!health) {
      el.configHealthDetails.innerHTML = `<div><span>状态</span><strong>未检查</strong></div>`;
      el.configHealthWarnings.innerHTML = "";
      return;
    }
    const statusLabels = { ok: "正常", warning: "有警告", error: "配置错误" };
    const rows = [
      ["状态", statusLabels[health.status] || health.status],
      ["配置可解析", health.valid ? "是" : "否"],
      ["配置版本", `${health.current_version} / ${health.latest_version}`],
      ["文件可写", health.writable ? "是" : "否"],
      ["缺失模块", health.missing_sections?.length ? health.missing_sections.join(", ") : "无"],
      ["未知模块", health.unknown_sections?.length ? health.unknown_sections.join(", ") : "无"],
      ["明文凭据", `${health.literal_secrets?.count || 0} 项`],
    ];
    el.configHealthDetails.innerHTML = rows
      .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");
    const warnings = Array.isArray(health.warnings) ? health.warnings : [];
    const errors = Array.isArray(health.validation_errors) ? health.validation_errors : [];
    el.configHealthWarnings.innerHTML = [
      ...errors.map((error) => ({ severity: "error", path: error.path, message: error.message })),
      ...warnings,
    ]
      .map((item) => {
        const label = item.severity === "error" ? "错误" : item.severity === "warning" ? "警告" : "提示";
        const path = item.path ? `<code>${escapeHtml(item.path)}</code>` : "";
        return `<div class="health-alert ${escapeHtml(item.severity || "info")}"><strong>${label}</strong>${path}<span>${escapeHtml(item.message)}</span></div>`;
      })
      .join("");
    el.configHealthMessage.textContent = warnings.length || errors.length ? "检查完成，请评估以上提示。" : "检查完成，未发现配置问题。";
  }

  function renderAgentSystemSummary() {
    if (!el.agentSystemSummary) return;
    const summary = state.adminConfig?.system_summary || {};
    const sandbox = summary.sandbox || {};
    const bridge = summary.stream_bridge || {};
    const acpAgents = Array.isArray(summary.acp_agents) ? summary.acp_agents : [];
    const tools = Array.isArray(summary.tools) ? summary.tools : [];
    const riskyTools = tools.filter((tool) => ["bash", "file:write"].includes(tool.group)).length;
    const autoApproved = acpAgents.filter((agent) => agent.auto_approve_permissions).length;
    const unboundedAcp = acpAgents.filter((agent) => agent.timeout_seconds === null).length;
    const rows = [
      ["Sandbox", sandbox.use || "未配置"],
      ["Sandbox 镜像", sandbox.image || "默认"],
      ["挂载数量", sandbox.mounts_count ?? 0],
      ["Stream Bridge", bridge.type || "memory"],
      ["Redis", bridge.redis_configured ? "已配置" : "未配置"],
      [
        "ACP Agents",
        `${acpAgents.length} 个；自动批准权限 ${autoApproved} 个${unboundedAcp ? `；未设超时 ${unboundedAcp} 个` : ""}`,
      ],
      ["内置 Tools", `${tools.length} 个；写入/执行类 ${riskyTools} 个`],
    ];
    el.agentSystemSummary.innerHTML = rows
      .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");
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
      ["Scheduler", formatConfigValue(api.scheduler_enabled)],
      ["Scheduler 时区", formatConfigValue(api.scheduler_timezone)],
      ["Scheduler 轮询", `${formatConfigValue(api.scheduler_poll_interval_seconds)} 秒`],
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
    el.runtimeSchedulerEnabled.checked = Boolean(api.scheduler_enabled);
    el.runtimeSchedulerPoll.value = api.scheduler_poll_interval_seconds ?? "";
    el.runtimeSchedulerTimezone.value = api.scheduler_timezone || "";
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
        const isDefault = state.adminConfig?.default_model === model.name;
        return `
          <tr>
            <td class="mono">${name} ${isDefault ? badge("默认", "ok") : ""}</td>
            <td>${escapeHtml(model.display_name || model.name)}</td>
            <td>${badge(model.supports_thinking ? "支持" : "不支持", model.supports_thinking ? "ok" : "neutral")}</td>
            <td>${badge(model.supports_vision ? "支持" : "不支持", model.supports_vision ? "ok" : "neutral")}</td>
            <td>
              <div class="row-actions">
                <button class="secondary-button" data-model-action="edit" data-model-name="${name}" type="button">编辑</button>
                ${isDefault ? "" : `<button class="ghost-button" data-model-action="default" data-model-name="${name}" type="button">设为默认</button>`}
                <button class="danger-button" data-model-action="delete" data-model-name="${name}" type="button">删除</button>
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

  function evolutionStatusLabel(status) {
    const labels = {
      generating: "生成中",
      validating: "验证中",
      pending_review: "待审批",
      publishing: "发布中",
      published: "已发布",
      rejected: "已拒绝",
      failed: "失败",
      stale: "已过期",
    };
    return labels[status] || status || "未知";
  }

  function evolutionStatusClass(status) {
    if (status === "published") return "ok";
    if (["failed", "stale", "rejected"].includes(status)) return "danger";
    if (["pending_review", "generating", "validating", "publishing"].includes(status)) return "warn";
    return "neutral";
  }

  function evolutionRiskLabel(risk) {
    if (risk === "low") return "低";
    if (risk === "high") return "高";
    return "中";
  }

  function evolutionRiskClass(risk) {
    if (risk === "low") return "ok";
    if (risk === "high") return "danger";
    return "warn";
  }

  function evolutionOriginLabel(origin) {
    return origin === "automatic" ? "自动发现" : "Agent 提交";
  }

  function evolutionSignalStatusLabel(status) {
    const labels = {
      pending: "等待处理",
      processing: "处理中",
      proposal_created: "已生成 Proposal",
      ignored: "已忽略",
      failed: "失败",
    };
    return labels[status] || status || "未知";
  }

  function renderEvolutionStatus() {
    if (!el.evolutionStatusSummary) return;
    const status = state.evolutionStatus || {};
    const pending = status.proposal_counts?.pending_review || 0;
    const signalPending = (status.signal_counts?.pending || 0) + (status.signal_counts?.processing || 0);
    const probationEntries = Object.values(status.probations || {});
    const probationActive = probationEntries.filter((item) => item?.status === "probation").length;
    const probationAlerts = probationEntries.filter((item) => item?.status === "alert").length;
    const items = [
      ["运行模式", status.enabled ? status.mode || "review" : "已禁用"],
      ["待审批", String(pending)],
      ["Catalog Version", String(status.catalog_version ?? 0)],
      ["自动发现", status.discovery_enabled ? "已启用" : "未启用"],
      ["后台 Worker", status.worker?.running ? `运行中 · 队列 ${status.worker.queue_depth || 0}` : "未运行"],
      ["待处理 Signal", String(signalPending)],
      ["Probation", String(probationActive)],
      ["回归告警", String(probationAlerts)],
    ];
    el.evolutionStatusSummary.innerHTML = items
      .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");
    if (el.evolutionProbationsPreview) {
      el.evolutionProbationsPreview.textContent = probationEntries.length
        ? JSON.stringify(status.probations, null, 2)
        : "当前没有 probation 记录。";
    }
  }

  function renderEvolutionSignals() {
    if (!el.evolutionSignalsTableBody) return;
    const signals = state.evolutionSignals;
    if (!signals.length) {
      el.evolutionSignalsTableBody.innerHTML = `<tr><td colspan="8">当前没有自动发现 Signal。</td></tr>`;
      return;
    }
    el.evolutionSignalsTableBody.innerHTML = signals
      .map((signal) => {
        const id = escapeHtml(signal.id || "");
        const errorCount = Number(signal.tool_error_count || 0);
        const processing = signal.status === "processing";
        return `
          <tr>
            <td class="mono" title="${id}">${escapeHtml(String(signal.id || "").slice(0, 14))}…</td>
            <td>${escapeHtml((signal.trigger_types || []).join(", ") || "--")}</td>
            <td>${escapeHtml((signal.tool_names || []).join(", ") || "--")}<br><small class="${errorCount ? "signal-error-count" : ""}">错误 ${errorCount}</small></td>
            <td>${escapeHtml(String(signal.recurrence_count || 1))}</td>
            <td>${badge(evolutionSignalStatusLabel(signal.status), signal.status === "failed" ? "danger" : signal.status === "proposal_created" ? "ok" : "warn")}</td>
            <td class="mono">${escapeHtml(signal.proposal_id || "--")}</td>
            <td class="mono">${escapeHtml(formatScheduledTime(signal.created_at))}</td>
            <td>
              <div class="row-actions compact-actions">
                <button class="secondary-button" data-signal-action="view" data-signal-id="${id}" type="button">${errorCount ? `查看错误 (${errorCount})` : "详情"}</button>
                <button
                  class="danger-button"
                  data-signal-action="delete"
                  data-signal-id="${id}"
                  type="button"
                  ${processing ? 'disabled aria-disabled="true" title="正在处理的 Signal 不能删除"' : ""}
                >删除</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function closeEvolutionSignalDetail({ restoreFocus = true } = {}) {
    el.evolutionSignalPanel.classList.add("hidden");
    state.selectedEvolutionSignal = null;
    if (restoreFocus && state.evolutionSignalTrigger?.isConnected) {
      state.evolutionSignalTrigger.focus();
    }
    state.evolutionSignalTrigger = null;
  }

  function renderEvolutionSignalErrors(signal) {
    const errors = Array.isArray(signal.tool_errors) ? signal.tool_errors : [];
    if (!errors.length) {
      const text = Number(signal.tool_error_count || 0)
        ? "该 Signal 创建时未保存错误详情，仅保留了错误数量。"
        : "本次 Signal 没有记录到工具错误。";
      el.evolutionSignalToolErrors.innerHTML = `<div class="signal-error-empty">${escapeHtml(text)}</div>`;
      return;
    }
    el.evolutionSignalToolErrors.innerHTML = errors
      .map(
        (error) => `
          <article class="signal-error-item" role="listitem">
            <div class="signal-error-meta">
              <strong>#${escapeHtml(error.sequence)} · ${escapeHtml(error.tool_name || "unknown")}</strong>
              ${badge(error.recovered ? "已恢复" : "未恢复", error.recovered ? "ok" : "danger")}
            </div>
            <p>${escapeHtml(error.message || "未提供错误信息")}</p>
          </article>
        `,
      )
      .join("");
  }

  async function openEvolutionSignal(signalId, trigger) {
    state.evolutionSignalTrigger = trigger || null;
    setBusy(trigger, true, "读取中");
    try {
      const signal = await request(`/api/admin/evolution/signals/${encodeURIComponent(signalId)}`);
      state.selectedEvolutionSignal = signal;
      el.evolutionSignalTitle.textContent = `${signal.id} · ${evolutionSignalStatusLabel(signal.status)}`;
      el.evolutionSignalSubtitle.textContent = (signal.trigger_types || []).join(", ") || "未记录触发原因";
      const details = [
        ["工具调用", signal.tool_count || 0],
        ["工具错误", signal.tool_error_count || 0],
        ["已恢复 / 未恢复", `${signal.recovered_error_count || 0} / ${signal.unresolved_error_count || 0}`],
        ["重复次数", signal.recurrence_count || 1],
        ["Proposal", signal.proposal_id || "--"],
        ["Thread / Run", [signal.thread_id, signal.run_id].filter(Boolean).join(" / ") || "--"],
        ["创建时间", formatScheduledTime(signal.created_at)],
        ["更新时间", formatScheduledTime(signal.updated_at)],
      ];
      el.evolutionSignalDetails.innerHTML = details
        .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
        .join("");
      renderEvolutionSignalErrors(signal);
      el.evolutionSignalSummary.textContent = `用户摘要：${signal.user_summary || "--"}\n\n助手摘要：${signal.assistant_summary || "--"}`;
      el.evolutionSignalProcessError.textContent = signal.error || "没有 Signal 处理错误。";
      const storedErrors = Array.isArray(signal.tool_errors) ? signal.tool_errors.length : 0;
      el.evolutionSignalMessage.textContent = Number(signal.tool_error_count || 0) > storedErrors && storedErrors > 0
        ? `为控制存储大小，仅展示前 ${storedErrors} 条脱敏错误。`
        : "错误信息已经过脱敏和长度限制。";
      el.evolutionSignalPanel.classList.remove("hidden");
      el.evolutionSignalPanel.focus({ preventScroll: true });
      el.evolutionSignalPanel.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      });
    } catch (error) {
      showToast(`读取 Signal 失败：${error.message}`);
    } finally {
      setBusy(trigger, false);
    }
  }

  async function deleteEvolutionSignal(signalId, button) {
    const signal = state.evolutionSignals.find((item) => item.id === signalId);
    if (signal?.status === "processing") {
      showToast("正在处理的 Signal 不能删除，请稍后重试。");
      return;
    }
    const proposalNote = signal?.proposal_id ? `\n关联 Proposal ${signal.proposal_id} 将被保留。` : "";
    if (!window.confirm(`确认删除 Signal ${signalId}？${proposalNote}\n该操作不会重置重复次数或自动发现冷却时间。`)) return;
    setBusy(button, true, "删除中");
    try {
      await request(`/api/admin/evolution/signals/${encodeURIComponent(signalId)}`, { method: "DELETE" });
      if (state.selectedEvolutionSignal?.id === signalId) {
        closeEvolutionSignalDetail({ restoreFocus: false });
      }
      await Promise.all([loadEvolutionSignals(), loadEvolutionStatus()]);
      showToast(`Signal ${signalId} 已删除。`);
    } catch (error) {
      showToast(`删除 Signal 失败：${error.message}`);
    } finally {
      setBusy(button, false);
    }
  }

  function renderEvolutionProposals() {
    if (!el.evolutionProposalsTableBody) return;
    const proposals = state.evolutionProposals;
    if (!proposals.length) {
      el.evolutionProposalsTableBody.innerHTML = `<tr><td colspan="8">当前没有符合条件的 Proposal。</td></tr>`;
      return;
    }
    el.evolutionProposalsTableBody.innerHTML = proposals
      .map((proposal) => {
        const id = escapeHtml(proposal.id);
        const archived = Boolean(proposal.archived_at);
        const terminal = ["published", "rejected", "failed", "stale"].includes(proposal.status);
        const mainLabel = proposal.status === "pending_review"
          ? "审核"
          : ["failed", "stale"].includes(proposal.status)
            ? "查看错误"
            : "查看";
        const menuActions = [
          proposal.status === "published" && proposal.published_revision != null
            ? `<button class="ghost-button" data-proposal-action="revision" data-proposal-id="${id}" type="button">查看 Revision</button>`
            : "",
          archived
            ? `<button class="secondary-button" data-proposal-action="restore" data-proposal-id="${id}" type="button">恢复</button>`
            : terminal
              ? `<button class="ghost-button" data-proposal-action="archive" data-proposal-id="${id}" type="button">归档</button>`
              : "",
          `<button class="ghost-button" data-proposal-action="copy" data-proposal-id="${id}" type="button">复制 ID</button>`,
        ].filter(Boolean);
        return `
          <tr class="${archived ? "archived-proposal-row" : ""}">
            <td class="mono" title="${id}">${id.slice(0, 14)}…</td>
            <td class="mono">${escapeHtml(proposal.skill_name)}</td>
            <td>${escapeHtml(proposal.action)}${proposal.file_path ? `<br><small>${escapeHtml(proposal.file_path)}</small>` : ""}</td>
            <td>${escapeHtml(evolutionOriginLabel(proposal.origin))}</td>
            <td>${badge(evolutionRiskLabel(proposal.risk), evolutionRiskClass(proposal.risk))}</td>
            <td>
              <div class="proposal-status-stack">
                ${badge(evolutionStatusLabel(proposal.status), evolutionStatusClass(proposal.status))}
                ${archived ? badge("已归档", "neutral") : ""}
              </div>
            </td>
            <td class="mono">${escapeHtml(formatScheduledTime(proposal.created_at))}</td>
            <td>
              <div class="row-actions compact-actions">
                <button class="secondary-button" data-proposal-action="view" data-proposal-id="${id}" type="button">${mainLabel}</button>
                <details class="proposal-action-menu">
                  <summary class="ghost-button">更多</summary>
                  <div class="proposal-action-menu-panel">
                    ${menuActions.join("")}
                  </div>
                </details>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  async function openEvolutionProposal(proposalId) {
    el.evolutionActionMessage.textContent = "读取 Proposal 中...";
    try {
      const proposal = await request(`/api/admin/evolution/proposals/${encodeURIComponent(proposalId)}`);
      state.selectedEvolutionProposal = proposal;
      el.evolutionProposalTitle.textContent = `${proposal.skill_name} · ${proposal.action}`;
      el.evolutionProposalSubtitle.textContent = proposal.reason || proposal.trigger?.summary || "未提供改进原因";
      const details = [
        ["Proposal", proposal.id],
        ["状态", evolutionStatusLabel(proposal.status)],
        ["来源", evolutionOriginLabel(proposal.origin)],
        ["风险", evolutionRiskLabel(proposal.risk)],
        ["基础 Revision", proposal.base_revision ?? "新建"],
        ["发布 Revision", proposal.published_revision ?? "--"],
        ["触发 Thread", proposal.trigger?.thread_id || "--"],
        ["变更文件", (proposal.changed_files || []).join(", ") || "--"],
        ["归档状态", proposal.archived_at ? `${formatScheduledTime(proposal.archived_at)} · ${proposal.archived_by || "--"}` : "未归档"],
      ];
      el.evolutionProposalDetails.innerHTML = details
        .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
        .join("");
      el.evolutionProposalDiff.textContent = proposal.diff || "没有可显示的文本 Diff。";
      el.evolutionProposalScans.textContent = JSON.stringify(proposal.scans || [], null, 2);
      el.evolutionProposalEvaluation.textContent = Object.keys(proposal.evaluation || {}).length
        ? JSON.stringify(proposal.evaluation, null, 2)
        : "该 Proposal 未进入 Auto Patch 评估。";
      el.evolutionReviewNote.value = proposal.review_note || "";
      el.evolutionReviewMessage.textContent = proposal.error || "";
      const pending = proposal.status === "pending_review";
      el.approveEvolutionProposalButton.classList.toggle("hidden", !pending);
      el.rejectEvolutionProposalButton.classList.toggle("hidden", !pending);
      el.evolutionProposalPanel.classList.remove("hidden");
      el.evolutionActionMessage.textContent = "";
      el.evolutionProposalPanel.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      });
    } catch (error) {
      el.evolutionActionMessage.textContent = `读取失败：${error.message}`;
    }
  }

  async function reviewEvolutionProposal(approve) {
    const proposal = state.selectedEvolutionProposal;
    if (!proposal) return;
    const button = approve ? el.approveEvolutionProposalButton : el.rejectEvolutionProposalButton;
    setBusy(button, true, approve ? "发布中" : "拒绝中");
    el.evolutionReviewMessage.textContent = "";
    try {
      const suffix = approve ? "approve" : "reject";
      await request(`/api/admin/evolution/proposals/${encodeURIComponent(proposal.id)}/${suffix}`, {
        method: "POST",
        timeoutMs: 120000,
        body: {
          expected_base_sha256: proposal.base_sha256,
          note: el.evolutionReviewNote.value.trim() || null,
        },
      });
      showToast(approve ? "Proposal 已批准并发布。" : "Proposal 已拒绝。");
      await Promise.all([loadEvolutionStatus(), loadEvolutionProposals(), loadEvolutionSignals(), loadSkills(), loadCustomSkills()]);
      await openEvolutionProposal(proposal.id);
    } catch (error) {
      el.evolutionReviewMessage.textContent = `${approve ? "发布" : "拒绝"}失败：${error.message}`;
    } finally {
      setBusy(button, false);
    }
  }

  async function setEvolutionProposalArchived(proposalId, restore, button) {
    const selected = state.selectedEvolutionProposal?.id === proposalId ? state.selectedEvolutionProposal : null;
    const proposal = state.evolutionProposals.find((item) => item.id === proposalId) || selected;
    if (!restore) {
      const status = proposal ? evolutionStatusLabel(proposal.status) : "未知";
      const skill = proposal?.skill_name || "未知 Skill";
      const confirmed = window.confirm(
        `确认归档 Proposal ${proposalId}？\nSkill：${skill}\n当前状态：${status}\n\n归档仅从默认列表隐藏记录，不会删除 Skill、Revision、Signal、Diff 或审计记录。`,
      );
      if (!confirmed) return;
    }
    button.closest("details")?.removeAttribute("open");
    setBusy(button, true, restore ? "恢复中" : "归档中");
    try {
      const action = restore ? "restore" : "archive";
      await request(`/api/admin/evolution/proposals/${encodeURIComponent(proposalId)}/${action}`, { method: "POST" });
      await Promise.all([loadEvolutionProposals(), loadEvolutionStatus()]);
      if (state.selectedEvolutionProposal?.id === proposalId) {
        if (state.evolutionProposals.some((item) => item.id === proposalId)) {
          await openEvolutionProposal(proposalId);
        } else {
          state.selectedEvolutionProposal = null;
          el.evolutionProposalPanel.classList.add("hidden");
        }
      }
      showToast(`Proposal ${proposalId} 已${restore ? "恢复" : "归档"}。`);
    } catch (error) {
      showToast(`${restore ? "恢复" : "归档"} Proposal 失败：${error.message}`);
    } finally {
      setBusy(button, false);
    }
  }

  async function openProposalRevision(proposalId, button) {
    const selected = state.selectedEvolutionProposal?.id === proposalId ? state.selectedEvolutionProposal : null;
    const proposal = state.evolutionProposals.find((item) => item.id === proposalId) || selected;
    if (!proposal?.skill_name || proposal.published_revision == null) {
      showToast("该 Proposal 没有关联的已发布 Revision。");
      return;
    }
    button.closest("details")?.removeAttribute("open");
    setBusy(button, true, "读取中");
    try {
      const name = proposal.skill_name;
      const version = proposal.published_revision;
      const revision = await request(
        `/api/admin/skills/custom/${encodeURIComponent(name)}/revisions/${encodeURIComponent(version)}`,
      );
      setView("skills");
      el.skillDraftPanel.classList.remove("hidden");
      const activeSkillExists = state.customSkills.some((skill) => skill.name === name);
      if (activeSkillExists) {
        el.customSkillSelect.value = name;
        await loadSelectedCustomSkill();
      } else {
        el.customSkillSelect.value = "";
        el.draftSkillName.value = name;
        el.draftSkillDescription.value = `历史 Revision v${version}`;
        el.draftSkillEnabled.checked = false;
        el.skillMarkdownEditor.value = revision.content || "该 Revision 表示 Skill 删除，没有 SKILL.md 快照。";
      }
      await loadSkillRevisions(version);
      el.skillDraftMessage.textContent = `Proposal ${proposalId} 对应 Revision v${version}。`;
      el.skillRevisionsPanel.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      });
    } catch (error) {
      showToast(`读取 Revision 失败：${error.message}`);
    } finally {
      setBusy(button, false);
    }
  }

  function formatScheduledTime(value) {
    if (!value) return "--";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function scheduledTaskExpression(task) {
    const expression = task.schedule_expr && typeof task.schedule_expr === "object" ? task.schedule_expr : {};
    if (task.schedule_type === "once") return `一次：${formatScheduledTime(expression.run_at)}`;
    if (task.schedule_type === "interval") {
      const start = expression.start_at ? `，开始于 ${formatScheduledTime(expression.start_at)}` : "";
      return `每 ${expression.every_seconds || "--"} 秒${start}`;
    }
    if (task.schedule_type === "daily") return `每天 ${expression.time_of_day || "--"}（${task.timezone || "默认时区"}）`;
    return `${task.schedule_type || "未知"}：${JSON.stringify(expression)}`;
  }

  function renderScheduledTasks() {
    if (!el.scheduledTasksTableBody || !el.scheduledTasksSummary) return;
    const tasks = state.scheduledTasks;
    const status = state.schedulerStatus;
    const serviceText = status?.enabled ? "Scheduler 已启用" : "Scheduler 已停用";
    const storageText = status?.storageExists ? "存储已存在" : "尚未创建任务存储";
    el.scheduledTasksSummary.textContent = `${serviceText}；${storageText}；共 ${tasks.length} 个任务。`;
    if (!tasks.length) {
      el.scheduledTasksTableBody.innerHTML = `<tr><td colspan="6">当前没有定时任务。</td></tr>`;
      return;
    }
    el.scheduledTasksTableBody.innerHTML = tasks
      .map((task) => {
        const id = escapeHtml(task.id);
        return `
          <tr>
            <td>
              <div class="mono task-id">${id}</div>
              <div class="description-cell task-prompt">${escapeHtml(task.prompt || "")}</div>
            </td>
            <td class="schedule-cell">${escapeHtml(scheduledTaskExpression(task))}</td>
            <td>${badge(task.enabled ? "已启用" : "已停用", task.enabled ? "ok" : "neutral")}</td>
            <td class="mono">${escapeHtml(formatScheduledTime(task.next_run_at))}</td>
            <td class="mono">${escapeHtml(task.thread_id || "--")}</td>
            <td><button class="danger-button" data-scheduled-task-action="delete" data-task-id="${id}" type="button">删除</button></td>
          </tr>
        `;
      })
      .join("");
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / (1024 ** index)).toFixed(index >= 3 ? 2 : 1)} ${units[index]}`;
  }

  function renderThreadCleanup() {
    if (!el.threadCleanupDatabaseDetails) return;
    const config = state.threadCleanupConfig || state.threadCleanupStatus?.config || {};
    if (!el.threadCleanupForm?.contains(document.activeElement)) {
      el.threadCleanupEnabled.checked = config.enabled !== false;
      el.threadCleanupInactiveDays.value = config.inactive_days ?? 30;
      el.threadCleanupDailyAt.value = config.run_daily_at || "03:00";
      el.threadCleanupTimezone.value = config.timezone || "Asia/Shanghai";
      el.threadCleanupBatchSize.value = config.batch_size ?? 20;
      el.threadCleanupBatchInterval.value = config.batch_interval_seconds ?? 1;
      el.threadCleanupMaxDeletes.value = config.max_deletions_per_run ?? 200;
      el.threadCleanupQuietPeriod.value = config.quiet_period_minutes ?? 10;
      el.threadCleanupPostpone.value = config.postpone_minutes ?? 10;
      el.threadCleanupProtectScheduled.checked = config.protect_scheduled_threads !== false;
      el.threadCleanupStopOnActivity.checked = config.stop_on_new_activity !== false;
    }

    const status = state.threadCleanupStatus || {};
    const database = status.database || {};
    const databaseRows = [
      ["数据库文件", formatBytes(database.database_bytes)],
      ["有效数据估算", formatBytes(database.estimated_live_bytes)],
      ["内部可复用空间", formatBytes(database.reusable_bytes)],
      ["WAL", formatBytes(database.wal_bytes)],
      [
        "Checkpoint / Writes",
        database.row_counts_exact === false
          ? "大库状态页省略精确统计"
          : `${database.checkpoint_rows ?? "--"} / ${database.write_rows ?? "--"}`,
      ],
      ["已索引会话", `${database.indexed_threads ?? "--"}（保护 ${database.protected_threads ?? "--"}）`],
    ];
    el.threadCleanupDatabaseDetails.innerHTML = databaseRows
      .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");

    const run = status.running_job || status.last_run;
    const runRows = [
      ["自动清理", config.enabled === false ? "已停用" : "已启用"],
      ["下次执行", formatScheduledTime(status.next_run_at)],
      ["任务状态", run?.status || "尚未执行"],
      ["开始 / 完成", `${formatScheduledTime(run?.started_at)} / ${formatScheduledTime(run?.completed_at)}`],
      ["扫描 / 删除", `${run?.scanned ?? 0} / ${run?.deleted ?? 0}`],
      ["跳过 / 失败", `${run?.skipped ?? 0} / ${run?.failed ?? 0}`],
      ["逻辑释放估算", formatBytes(run?.estimated_reclaimed_bytes)],
      ["当前线程", run?.current_thread_id || "--"],
    ];
    el.threadCleanupRunDetails.innerHTML = runRows
      .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");

    const candidates = state.threadCleanupPreview?.candidates;
    if (!Array.isArray(candidates)) return;
    el.threadCleanupCandidatesBody.innerHTML = candidates.length
      ? candidates.map((candidate) => {
          const stateText = candidate.running ? "运行中" : candidate.scheduled ? "定时任务保护" : "可清理";
          const tone = candidate.running || candidate.scheduled ? "warn" : "ok";
          return `
            <tr>
              <td class="mono">${escapeHtml(candidate.thread_id)}</td>
              <td class="mono">${escapeHtml(formatScheduledTime(candidate.last_activity_at))}</td>
              <td>${escapeHtml(candidate.inactive_days)}</td>
              <td>${escapeHtml(candidate.checkpoint_rows)} / ${escapeHtml(candidate.write_rows)}</td>
              <td>${escapeHtml(formatBytes(candidate.estimated_bytes))}</td>
              <td>${badge(stateText, tone)}</td>
            </tr>
          `;
        }).join("")
      : `<tr><td colspan="6">当前没有符合保留策略的候选会话。</td></tr>`;
  }

  function scheduleThreadCleanupPoll() {
    if (state.threadCleanupPollTimer) {
      window.clearTimeout(state.threadCleanupPollTimer);
      state.threadCleanupPollTimer = null;
    }
    if (!state.threadCleanupStatus?.running_job) return;
    state.threadCleanupPollTimer = window.setTimeout(async () => {
      try {
        await loadThreadCleanupStatus();
      } catch (error) {
        el.threadCleanupMessage.textContent = `任务状态读取失败：${error.message}`;
      }
    }, 2000);
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
      await Promise.all([loadSkills(), loadEvolutionStatus()]);
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
      scheduler_enabled: el.runtimeSchedulerEnabled.checked,
      scheduler_timezone: el.runtimeSchedulerTimezone.value.trim() || "Asia/Shanghai",
      reload: true,
    };
    addNumberField(body, "max_concurrent_subagents", el.runtimeMaxSubagents.value);
    addNumberField(body, "chat_request_timeout", el.runtimeChatTimeout.value);
    addNumberField(body, "max_upload_size_mb", el.runtimeMaxUploadSize.value);
    addNumberField(body, "max_uploads_per_request", el.runtimeMaxUploads.value);
    addNumberField(body, "scheduler_poll_interval_seconds", el.runtimeSchedulerPoll.value);
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

  async function saveThreadCleanupConfig() {
    el.threadCleanupMessage.textContent = "";
    if (!el.threadCleanupForm.checkValidity()) {
      el.threadCleanupForm.reportValidity();
      el.threadCleanupMessage.textContent = "请修正超出允许范围的自动清理配置。";
      return;
    }
    const config = {
      enabled: el.threadCleanupEnabled.checked,
      inactive_days: Number(el.threadCleanupInactiveDays.value),
      run_daily_at: el.threadCleanupDailyAt.value || "03:00",
      timezone: el.threadCleanupTimezone.value.trim() || "Asia/Shanghai",
      batch_size: Number(el.threadCleanupBatchSize.value),
      batch_interval_seconds: Number(el.threadCleanupBatchInterval.value),
      max_deletions_per_run: Number(el.threadCleanupMaxDeletes.value),
      quiet_period_minutes: Number(el.threadCleanupQuietPeriod.value),
      postpone_minutes: Number(el.threadCleanupPostpone.value),
      protect_scheduled_threads: el.threadCleanupProtectScheduled.checked,
      stop_on_new_activity: el.threadCleanupStopOnActivity.checked,
    };
    setBusy(el.saveThreadCleanupButton, true, "保存中");
    try {
      const data = await request("/api/admin/thread-cleanup/config", {
        method: "PUT",
        body: { config },
      });
      state.threadCleanupConfig = data.config;
      el.threadCleanupMessage.textContent = "配置已保存并热更新。";
      await loadThreadCleanupStatus();
      showToast("自动清理配置已保存。");
    } catch (error) {
      el.threadCleanupMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveThreadCleanupButton, false);
    }
  }

  async function previewThreadCleanup() {
    el.threadCleanupMessage.textContent = "";
    setBusy(el.previewThreadCleanupButton, true, "扫描中");
    try {
      const data = await request("/api/admin/thread-cleanup/preview?limit=100", { timeoutMs: 120000 });
      state.threadCleanupPreview = data;
      renderThreadCleanup();
      el.threadCleanupMessage.textContent = `找到 ${data.candidates?.length || 0} 个候选，其中 ${data.eligible_count || 0} 个可清理，估算 ${formatBytes(data.estimated_reclaimable_bytes)}。`;
    } catch (error) {
      el.threadCleanupMessage.textContent = `预览失败：${error.message}`;
    } finally {
      setBusy(el.previewThreadCleanupButton, false);
    }
  }

  async function runThreadCleanup() {
    const days = state.threadCleanupConfig?.inactive_days ?? 30;
    if (!window.confirm(`确认后台删除最后活跃超过 ${days} 天的完整会话？该操作不可撤销。`)) return;
    el.threadCleanupMessage.textContent = "";
    setBusy(el.runThreadCleanupButton, true, "启动中");
    try {
      const data = await request("/api/admin/thread-cleanup/runs", {
        method: "POST",
        body: { dry_run: false },
      });
      el.threadCleanupMessage.textContent = data.already_running
        ? `已有任务正在执行：${data.job_id}`
        : `清理任务已启动：${data.job_id}`;
      await loadThreadCleanupStatus();
    } catch (error) {
      el.threadCleanupMessage.textContent = `启动失败：${error.message}`;
    } finally {
      setBusy(el.runThreadCleanupButton, false);
    }
  }

  async function refreshThreadCleanup() {
    setBusy(el.refreshThreadCleanupButton, true, "刷新中");
    try {
      await Promise.all([loadThreadCleanupConfig(), loadThreadCleanupStatus()]);
    } catch (error) {
      el.threadCleanupMessage.textContent = `刷新失败：${error.message}`;
    } finally {
      setBusy(el.refreshThreadCleanupButton, false);
    }
  }

  function addNumberField(target, key, value) {
    const trimmed = String(value || "").trim();
    if (!trimmed) return;
    const number = Number(trimmed);
    if (Number.isFinite(number)) target[key] = number;
  }

  async function saveTitleConfig() {
    el.titleMessage.textContent = "";
    const config = {
      enabled: el.titleEnabled.checked,
      model_name: el.titleModelName.value || null,
      max_words: Number(el.titleMaxWords.value || 6),
      max_chars: Number(el.titleMaxChars.value || 60),
      prompt_template: el.titlePromptTemplate.value,
    };
    setBusy(el.saveTitleButton, true, "保存中");
    try {
      const data = await request("/api/admin/title", {
        method: "PUT",
        body: { config, reload: true },
        timeoutMs: 20000,
      });
      state.titleConfig = data.config;
      renderTitleConfig();
      const active = data.reload?.active_threads ? `；当前活动线程 ${data.reload.active_threads} 个保持原状态` : "";
      el.titleMessage.textContent = `Title 配置已保存${active}。`;
      showToast("Title 配置已保存。");
    } catch (error) {
      el.titleMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveTitleButton, false);
    }
  }

  async function saveSubagentsConfig() {
    el.subagentsMessage.textContent = "";
    let agents;
    let customAgents;
    try {
      agents = parseJsonObject(el.subagentsAgentsEditor.value, "agents");
      customAgents = parseJsonObject(el.subagentsCustomEditor.value, "custom_agents");
    } catch (error) {
      el.subagentsMessage.textContent = error.message;
      return;
    }
    const maxTurns = String(el.subagentsMaxTurns.value || "").trim();
    const config = {
      enabled: el.subagentsEnabled.checked,
      timeout_seconds: Number(el.subagentsTimeout.value || 900),
      max_turns: maxTurns ? Number(maxTurns) : null,
      agents,
      custom_agents: customAgents,
    };
    setBusy(el.saveSubagentsButton, true, "保存中");
    try {
      const data = await request("/api/admin/subagents", {
        method: "PUT",
        body: { config, reload: true },
        timeoutMs: 20000,
      });
      state.subagentsConfig = data.config;
      renderSubagentsConfig();
      const active = data.reload?.active_threads ? `；当前活动线程 ${data.reload.active_threads} 个保持原状态` : "";
      el.subagentsMessage.textContent = `Subagents 配置已保存${active}。`;
      showToast("Subagents 配置已保存。");
    } catch (error) {
      el.subagentsMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveSubagentsButton, false);
    }
  }

  async function saveMemoryConfig() {
    el.memoryMessage.textContent = "";
    if (!el.memoryEditForm.checkValidity()) {
      el.memoryEditForm.reportValidity();
      el.memoryMessage.textContent = "请修正超出允许范围的 Memory 配置。";
      return;
    }
    const storageClass = el.memoryStorageClass.value.trim();
    if (!storageClass) {
      el.memoryMessage.textContent = "storage_class 不能为空。";
      el.memoryStorageClass.focus();
      return;
    }
    const config = {
      enabled: el.memoryEnabled.checked,
      injection_enabled: el.memoryInjectionEnabled.checked,
      model_name: el.memoryModelName.value || null,
      debounce_seconds: Number(el.memoryDebounce.value || 30),
      max_facts: Number(el.memoryMaxFacts.value || 100),
      fact_confidence_threshold: Number(el.memoryConfidence.value || 0.7),
      max_injection_tokens: Number(el.memoryMaxInjectionTokens.value || 2000),
      storage_path: el.memoryStoragePath.value.trim(),
      storage_class: storageClass,
    };
    setBusy(el.saveMemoryButton, true, "保存中");
    try {
      const data = await request("/api/admin/memory", {
        method: "PUT",
        body: { config, reload: true },
        timeoutMs: 20000,
      });
      state.memoryConfig = data.config;
      renderMemoryConfig();
      const active = data.reload?.active_threads ? `；当前活动线程 ${data.reload.active_threads} 个保持原状态` : "";
      el.memoryMessage.textContent = `Memory 配置已保存${active}。`;
      await loadConfigHealth();
      showToast("Memory 配置已保存。");
    } catch (error) {
      el.memoryMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveMemoryButton, false);
    }
  }

  function parseSummarizationTrigger() {
    const raw = el.summarizationTrigger.value.trim();
    if (!raw) return null;
    let trigger;
    try {
      trigger = JSON.parse(raw);
    } catch (error) {
      throw new Error(`trigger 不是有效 JSON：${error.message}`);
    }
    if (trigger !== null && (typeof trigger !== "object" || Array.isArray(trigger) && trigger.some((item) => !item || typeof item !== "object" || Array.isArray(item)))) {
      throw new Error("trigger 必须是 JSON object、object 数组或 null。");
    }
    return trigger;
  }

  function summarizationKeepConfig() {
    const type = el.summarizationKeepType.value;
    const raw = el.summarizationKeepValue.value.trim();
    const value = Number(raw);
    if (!raw || !Number.isFinite(value)) {
      throw new Error("keep.value 必须是数字。");
    }
    if (type === "fraction") {
      if (value <= 0 || value > 1) throw new Error("fraction 类型的 keep.value 必须大于 0 且不超过 1。");
    } else if (value < 1 || !Number.isInteger(value)) {
      throw new Error(`${type} 类型的 keep.value 必须是正整数。`);
    }
    return { type, value };
  }

  async function saveSummarizationConfig() {
    el.summarizationMessage.textContent = "";
    if (!el.summarizationEditForm.checkValidity()) {
      el.summarizationEditForm.reportValidity();
      el.summarizationMessage.textContent = "请修正超出允许范围的 Summarization 配置。";
      return;
    }
    let trigger;
    let keep;
    try {
      trigger = parseSummarizationTrigger();
      keep = summarizationKeepConfig();
    } catch (error) {
      el.summarizationMessage.textContent = error.message;
      return;
    }
    const trimTokens = el.summarizationTrimTokens.value.trim();
    const config = {
      enabled: el.summarizationEnabled.checked,
      model_name: el.summarizationModelName.value || null,
      trigger,
      keep,
      trim_tokens_to_summarize: trimTokens ? Number(trimTokens) : null,
      preserve_recent_skill_count: Number(el.summarizationSkillCount.value || 0),
      preserve_recent_skill_tokens: Number(el.summarizationSkillTokens.value || 0),
      preserve_recent_skill_tokens_per_skill: Number(el.summarizationSkillTokensPerSkill.value || 0),
      skill_file_read_tool_names: [...new Set(el.summarizationSkillTools.value.split(",").map((item) => item.trim()).filter(Boolean))],
      summary_prompt: el.summarizationPrompt.value.trim() || null,
    };
    setBusy(el.saveSummarizationButton, true, "保存中");
    try {
      const data = await request("/api/admin/summarization", {
        method: "PUT",
        body: { config, reload: true },
        timeoutMs: 20000,
      });
      state.summarizationConfig = data.config;
      renderSummarizationConfig();
      const active = data.reload?.active_threads ? `；当前活动线程 ${data.reload.active_threads} 个保持原状态` : "";
      el.summarizationMessage.textContent = `Summarization 配置已保存${active}。`;
      await loadConfigHealth();
      showToast("Summarization 配置已保存。");
    } catch (error) {
      el.summarizationMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveSummarizationButton, false);
    }
  }

  async function refreshConfigHealth() {
    setBusy(el.refreshConfigHealthButton, true, "检查中");
    try {
      await loadConfigHealth();
    } catch (error) {
      el.configHealthMessage.textContent = `检查失败：${error.message}`;
    } finally {
      setBusy(el.refreshConfigHealthButton, false);
    }
  }

  async function refreshScheduledTasks() {
    el.scheduledTasksMessage.textContent = "";
    setBusy(el.refreshScheduledTasksButton, true, "刷新中");
    try {
      await loadScheduledTasks();
      el.scheduledTasksMessage.textContent = "定时任务已刷新。";
    } catch (error) {
      el.scheduledTasksMessage.textContent = `刷新失败：${error.message}`;
    } finally {
      setBusy(el.refreshScheduledTasksButton, false);
    }
  }

  async function deleteScheduledTask(taskId, button) {
    const task = state.scheduledTasks.find((item) => item.id === taskId);
    const prompt = task?.prompt ? `\n\n${task.prompt.slice(0, 120)}` : "";
    if (!window.confirm(`确定删除定时任务 ${taskId}？删除后不会再触发后续执行，已经开始的执行不会被取消。${prompt}`)) return;
    el.scheduledTasksMessage.textContent = "";
    setBusy(button, true, "删除中");
    try {
      await request(`/api/admin/scheduled-tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
      await loadScheduledTasks();
      el.scheduledTasksMessage.textContent = `${taskId} 已删除。`;
      showToast("定时任务已删除。");
    } catch (error) {
      el.scheduledTasksMessage.textContent = `删除失败：${error.message}`;
    } finally {
      setBusy(button, false);
    }
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
        el.draftClearApiKey.checked
          ? "  # api_key 将被明确清除"
          : "  # api_key 未提交，由后端保留现有值",
      );
    } else {
      lines.push("  # api_key 未配置；如 Provider 需要凭据请填写");
    }
    if (baseUrl) lines.push(`  base_url: ${yamlValue(baseUrl)}`);
    lines.push(`  supports_thinking: ${el.draftThinking.checked ? "true" : "false"}`);
    lines.push(`  supports_reasoning_effort: ${el.draftReasoningEffort.checked ? "true" : "false"}`);
    lines.push(`  supports_vision: ${el.draftVision.checked ? "true" : "false"}`);
    if (el.draftModelAdvanced.value.trim()) lines.push("  # 高级参数将按 JSON 内容合并");
    el.modelYamlPreview.textContent = lines.join("\n");
  }

  function getEditableModel(name) {
    if (!name || !Array.isArray(state.adminConfig?.models)) return null;
    return state.adminConfig.models.find((model) => model && model.name === name) || null;
  }

  function isConfiguredSecret(value) {
    return Boolean(value && typeof value === "object" && value.configured !== false && "redacted" in value);
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
    el.draftClearApiKey.checked = false;
    el.draftApiKey.disabled = false;
    el.draftClearApiKeyRow.classList.add("hidden");
    el.draftSetDefault.checked = false;
    el.draftModelAdvanced.value = "{}";
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
    el.draftApiKey.value = "";
    el.draftApiKey.disabled = false;
    el.draftClearApiKey.checked = false;
    el.draftClearApiKeyRow.classList.remove("hidden");
    el.draftThinking.checked = Boolean(model.supports_thinking);
    el.draftReasoningEffort.checked = Boolean(model.supports_reasoning_effort);
    el.draftVision.checked = Boolean(model.supports_vision);
    el.draftSetDefault.checked = state.adminConfig?.default_model === model.name;
    el.draftModelAdvanced.value = JSON.stringify(modelAdvancedConfig(model), null, 2);
    el.draftApiKey.placeholder = isConfiguredSecret(model.api_key) ? "留空保留现有 api_key" : "$DASHSCOPE_API_KEY";
    el.modelDraftMessage.textContent = `正在编辑 ${model.name}。api_key 留空会保留原值。`;
    el.modelDraftPanel.classList.remove("hidden");
    updateModelDraft();
    el.modelDraftPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const basicModelFields = new Set([
    "name",
    "display_name",
    "use",
    "model",
    "api_key",
    "base_url",
    "supports_thinking",
    "supports_reasoning_effort",
    "supports_vision",
  ]);

  function modelAdvancedConfig(model) {
    if (!model || typeof model !== "object") return {};
    return Object.fromEntries(Object.entries(model).filter(([key]) => !basicModelFields.has(key)));
  }

  function parseJsonObject(value, label) {
    let parsed;
    try {
      parsed = JSON.parse(String(value || "{}").trim() || "{}");
    } catch (error) {
      throw new Error(`${label} 不是有效 JSON：${error.message}`);
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error(`${label} 必须是 JSON object。`);
    }
    return parsed;
  }

  function modelDraftPayload(editing) {
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
    const advanced = parseJsonObject(el.draftModelAdvanced.value, "高级参数");
    const payload = {
      ...advanced,
      name,
      display_name: displayName,
      use,
      model,
      supports_thinking: el.draftThinking.checked,
      supports_reasoning_effort: el.draftReasoningEffort.checked,
      supports_vision: el.draftVision.checked,
    };
    if (baseUrl || editing) payload.base_url = baseUrl || null;
    if (apiKey) {
      payload.api_key = apiKey;
    }
    return payload;
  }

  async function reloadModelsAfterSave(modelName, sequence) {
    try {
      const reload = await request("/api/admin/config/reload", {
        method: "POST",
        body: { include_extensions: true, reset_clients: true },
        timeoutMs: 15000,
      });
      await Promise.allSettled([loadAdminConfig(), loadModels()]);
      renderOverview();
      if (sequence !== state.modelReloadSequence) return;
      const active = reload?.active_threads ? `，当前运行线程 ${reload.active_threads} 个` : "";
      el.modelDraftMessage.textContent = `${modelName} 已保存并重新加载配置${active}。`;
    } catch (error) {
      if (sequence !== state.modelReloadSequence) return;
      el.modelDraftMessage.textContent = `${modelName} 已保存；重新加载未完成：${error.message}。可稍后手动刷新。`;
    }
  }

  async function saveModelDraft() {
    el.modelDraftMessage.textContent = "";
    if (!state.adminConfig) {
      await loadAdminConfig();
    }

    const editing = Boolean(state.editingModelName);
    let payload;
    try {
      payload = modelDraftPayload(editing);
    } catch (error) {
      el.modelDraftMessage.textContent = error.message;
      return;
    }

    const reloadSequence = state.modelReloadSequence + 1;
    state.modelReloadSequence = reloadSequence;
    setBusy(el.saveModelButton, true, "保存中");
    try {
      const path = editing ? `/api/admin/models/${encodeURIComponent(state.editingModelName)}` : "/api/admin/models";
      await request(path, {
        method: editing ? "PATCH" : "POST",
        body: editing
          ? {
              changes: payload,
              clear_api_key: el.draftClearApiKey.checked,
              set_default: el.draftSetDefault.checked,
              reload: false,
            }
          : { model: payload, set_default: el.draftSetDefault.checked, reload: false },
        timeoutMs: 15000,
      });
      state.editingModelName = payload.name;
      await Promise.all([loadAdminConfig(), loadModels()]);
      el.modelDraftMessage.textContent = `${payload.name} 已保存，正在重新加载配置。`;
      showToast("模型配置已保存。");
      reloadModelsAfterSave(payload.name, reloadSequence);
    } catch (error) {
      el.modelDraftMessage.textContent = `保存失败：${error.message}`;
    } finally {
      setBusy(el.saveModelButton, false);
    }
  }

  async function setDefaultModel(name) {
    try {
      await request(`/api/admin/models/${encodeURIComponent(name)}`, {
        method: "PATCH",
        body: { changes: {}, set_default: true, reload: false },
        timeoutMs: 15000,
      });
      await Promise.all([loadAdminConfig(), loadModels()]);
      showToast(`${name} 已设为默认模型。`);
      reloadModelsAfterSave(name, ++state.modelReloadSequence);
    } catch (error) {
      showToast(`设置默认模型失败：${error.message}`);
    }
  }

  async function deleteModel(name) {
    if (!window.confirm(`确定删除模型 ${name}？该操作不会删除 Provider 侧的模型。`)) return;
    try {
      await request(`/api/admin/models/${encodeURIComponent(name)}?reload=false`, {
        method: "DELETE",
        timeoutMs: 15000,
      });
      if (state.editingModelName === name) resetModelDraft();
      await Promise.all([loadAdminConfig(), loadModels()]);
      showToast(`${name} 已删除。`);
      reloadModelsAfterSave(name, ++state.modelReloadSequence);
    } catch (error) {
      showToast(`删除模型失败：${error.message}`);
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
    el.skillRevisionsPanel.classList.add("hidden");
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
      await Promise.all([loadSkills(), loadCustomSkills(), loadEvolutionStatus()]);
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
      await Promise.all([loadSkills(), loadCustomSkills(), loadEvolutionStatus()]);
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

  async function loadSkillRevisions(targetVersion = null) {
    const name = slugify(el.draftSkillName.value.trim());
    if (!name) {
      el.skillDraftMessage.textContent = "请先选择一个自定义 Skill。";
      return;
    }
    try {
      const data = await request(`/api/admin/skills/custom/${encodeURIComponent(name)}/revisions`);
      const active = Number(data.active_revision);
      const revisions = Array.isArray(data.revisions) ? data.revisions : [];
      el.skillRevisionsList.innerHTML = revisions.length
        ? revisions
            .map((revision) => {
              const isActive = Number(revision.version) === active;
              const isTarget = targetVersion != null && Number(revision.version) === Number(targetVersion);
              const note = revision.note || revision.action || "published";
              return `
                <div class="revision-item ${isTarget ? "proposal-target-revision" : ""}" ${isTarget ? 'aria-current="true"' : ""}>
                  <strong>
                    v${escapeHtml(revision.version)}
                    ${isActive ? badge("当前", "ok") : ""}
                    ${isTarget ? badge("当前 Proposal", "warn") : ""}
                  </strong>
                  <span><small>${escapeHtml(formatScheduledTime(revision.created_at))} · ${escapeHtml(note)}</small></span>
                  ${isActive ? "" : `<button class="ghost-button" data-revision-action="rollback" data-revision-version="${escapeHtml(revision.version)}" type="button">回滚到此版本</button>`}
                </div>
              `;
            })
            .join("")
        : `<div class="revision-item"><span>尚无 Revision。</span></div>`;
      el.skillRevisionsPanel.classList.remove("hidden");
      el.skillHistoryPreview.textContent = "";
    } catch (error) {
      el.skillDraftMessage.textContent = `版本读取失败：${error.message}`;
    }
  }

  async function rollbackSkillRevision(version, button) {
    const name = slugify(el.draftSkillName.value.trim());
    if (!name || !window.confirm(`确认将 ${name} 回滚到 v${version}？系统会创建一个新的 Revision。`)) return;
    setBusy(button, true, "回滚中");
    try {
      await request(`/api/admin/skills/custom/${encodeURIComponent(name)}/rollback/${encodeURIComponent(version)}`, {
        method: "POST",
        body: { note: `Admin UI rollback to v${version}` },
      });
      showToast(`${name} 已回滚到 v${version} 的内容。`);
      await Promise.all([loadSkills(), loadCustomSkills(), loadEvolutionStatus()]);
      if (state.customSkills.some((skill) => skill.name === name)) {
        el.customSkillSelect.value = name;
        await loadSelectedCustomSkill();
        await loadSkillRevisions();
      } else {
        el.customSkillSelect.value = "";
        updateSkillDraft();
        el.skillRevisionsPanel.classList.add("hidden");
      }
    } catch (error) {
      el.skillDraftMessage.textContent = `回滚失败：${error.message}`;
    } finally {
      setBusy(button, false);
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
      await Promise.all([loadSelectedCustomSkill(), loadEvolutionStatus()]);
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
      await Promise.all([loadSelectedCustomSkill(), loadEvolutionStatus()]);
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
    el.proposalStatusFilter.addEventListener("change", loadEvolutionProposals);
    el.proposalArchiveFilter.addEventListener("change", loadEvolutionProposals);
    el.refreshEvolutionButton.addEventListener("click", async () => {
      setBusy(el.refreshEvolutionButton, true, "刷新中");
      try {
        await Promise.all([loadEvolutionStatus(), loadEvolutionProposals(), loadEvolutionSignals()]);
      } finally {
        setBusy(el.refreshEvolutionButton, false);
      }
    });
    el.closeEvolutionProposalButton.addEventListener("click", () => el.evolutionProposalPanel.classList.add("hidden"));
    el.approveEvolutionProposalButton.addEventListener("click", () => reviewEvolutionProposal(true));
    el.rejectEvolutionProposalButton.addEventListener("click", () => reviewEvolutionProposal(false));
    el.evolutionProposalsTableBody.addEventListener("click", (event) => {
      const button = event.target.closest("[data-proposal-action]");
      if (!button) return;
      const action = button.dataset.proposalAction;
      if (action === "view") {
        openEvolutionProposal(button.dataset.proposalId);
      } else if (action === "archive" || action === "restore") {
        setEvolutionProposalArchived(button.dataset.proposalId, action === "restore", button);
      } else if (action === "revision") {
        openProposalRevision(button.dataset.proposalId, button);
      } else if (action === "copy") {
        button.closest("details")?.removeAttribute("open");
        copyText(button.dataset.proposalId, "Proposal ID 已复制。");
      }
    });
    el.evolutionSignalsTableBody.addEventListener("click", (event) => {
      const button = event.target.closest("[data-signal-action]");
      if (!button) return;
      if (button.dataset.signalAction === "view") {
        openEvolutionSignal(button.dataset.signalId, button);
      } else if (button.dataset.signalAction === "delete") {
        deleteEvolutionSignal(button.dataset.signalId, button);
      }
    });
    el.closeEvolutionSignalButton.addEventListener("click", () => closeEvolutionSignalDetail());
    el.evolutionSignalPanel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeEvolutionSignalDetail();
    });
    el.saveMcpButton.addEventListener("click", saveMcp);
    el.enableMcpButton.addEventListener("click", () => setMcpEnabled(true));
    el.disableMcpButton.addEventListener("click", () => setMcpEnabled(false));
    el.testMcpButton.addEventListener("click", testMcpServer);
    el.reloadConfigButton.addEventListener("click", reloadConfig);
    el.saveRuntimeButton.addEventListener("click", saveRuntimeConfig);
    el.saveTitleButton.addEventListener("click", saveTitleConfig);
    el.saveSubagentsButton.addEventListener("click", saveSubagentsConfig);
    el.saveMemoryButton.addEventListener("click", saveMemoryConfig);
    el.saveSummarizationButton.addEventListener("click", saveSummarizationConfig);
    el.refreshConfigHealthButton.addEventListener("click", refreshConfigHealth);
    el.refreshScheduledTasksButton.addEventListener("click", refreshScheduledTasks);
    el.saveThreadCleanupButton.addEventListener("click", saveThreadCleanupConfig);
    el.refreshThreadCleanupButton.addEventListener("click", refreshThreadCleanup);
    el.previewThreadCleanupButton.addEventListener("click", previewThreadCleanup);
    el.runThreadCleanupButton.addEventListener("click", runThreadCleanup);
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
      } else if (button.dataset.modelAction === "default") {
        setDefaultModel(button.dataset.modelName);
      } else if (button.dataset.modelAction === "delete") {
        deleteModel(button.dataset.modelName);
      }
    });

    el.scheduledTasksTableBody.addEventListener("click", (event) => {
      const button = event.target.closest("[data-scheduled-task-action]");
      if (!button || button.dataset.scheduledTaskAction !== "delete") return;
      deleteScheduledTask(button.dataset.taskId, button);
    });

    el.openModelDraftButton.addEventListener("click", () => {
      el.modelDraftPanel.classList.remove("hidden");
      resetModelDraft();
    });
    el.closeModelDraftButton.addEventListener("click", () => el.modelDraftPanel.classList.add("hidden"));
    el.modelDraftForm.addEventListener("input", updateModelDraft);
    el.draftClearApiKey.addEventListener("change", () => {
      el.draftApiKey.disabled = el.draftClearApiKey.checked;
      if (el.draftClearApiKey.checked) el.draftApiKey.value = "";
      updateModelDraft();
    });
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
    el.loadSkillRevisionsButton.addEventListener("click", () => loadSkillRevisions());
    el.skillRevisionsList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-revision-action='rollback']");
      if (!button) return;
      rollbackSkillRevision(button.dataset.revisionVersion, button);
    });
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
