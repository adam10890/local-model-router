(() => {
  "use strict";
  const metaContent = name => document.querySelector(`meta[name="${name}"]`)?.getAttribute("content") || "";
  const SETUP_TOKEN = metaContent("imperium-setup-token");
  const IMPERIUM_VERSION = metaContent("imperium-version");
  const dictionary = window.IMPERIUM_DICTIONARY || { en: {}, he: {} };
  function loadApiKey() {
    try {
      const current = sessionStorage.getItem("imperium.apiKey");
      if (current) return current;
      const legacy = localStorage.getItem("imperium.apiKey");
      if (legacy) {
        sessionStorage.setItem("imperium.apiKey", legacy);
        localStorage.removeItem("imperium.apiKey");
        return legacy;
      }
    } catch (_) {}
    return "";
  }
  function saveApiKey(value) {
    try {
      sessionStorage.setItem("imperium.apiKey", value);
      localStorage.removeItem("imperium.apiKey");
    } catch (_) {}
  }
  const ICON = name => `/ui/icons/${name}.svg`;
  const I = (name, cls="") => `<img class="${cls}" src="${ICON(name)}" alt="">`;
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
  const textValue = value => typeof value === "object" && value ? (value[state.lang] || value.en || "") : (value || "");
  const navSimple = [
    ["home","house","home"],["chat","chat-circle-dots","chat"],["models","cube","models"],["connections","plugs-connected","connections"]
  ];
  const navAdvanced = [
    ["advanced/fleet","hard-drives","fleet"],["advanced/routing","git-branch","routing"],["advanced/providers","graphics-card","computeProviders"],["advanced/configuration","file-code","configuration"],["advanced/diagnostics","heartbeat","diagnostics"]
  ];
  function loadChat() {
    try { const messages=JSON.parse(localStorage.getItem("imperium.chat")||"[]"); return Array.isArray(messages)?messages.filter(message=>["user","assistant"].includes(message?.role)&&typeof message.content==="string").slice(-100):[]; }
    catch (_) { return []; }
  }
  function saveChat() { try { localStorage.setItem("imperium.chat",JSON.stringify(state.messages.slice(-100))); } catch (_) {} }
  function loadSetup() {
    try {
      const saved=JSON.parse(localStorage.getItem("imperium.setup")||"{}");
      return {step:Math.min(6,Math.max(1,Number(saved.step)||1)),wizard:saved.wizard&&typeof saved.wizard==="object"?saved.wizard:{},plan:saved.plan&&typeof saved.plan==="object"?saved.plan:null};
    } catch (_) { return {step:1,wizard:{},plan:null}; }
  }
  const savedSetup=loadSetup();
  let drawerReturnFocus=null;
  const setupRoutes=["setup","setup/system","setup/runtime","setup/model","setup/plan","setup/install"];
  const setupStepByRoute={"setup":1,"setup/system":2,"setup/hardware":2,"setup/runtime":3,"setup/launch":3,"setup/backend":3,"setup/model":4,"setup/plan":5,"setup/install":6};
  const simpleModelAliases=["auto","chat","fast","coder"];
  const state = {
    lang: localStorage.getItem("imperium.lang") || "en",
    theme: localStorage.getItem("imperium.theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
    mode: localStorage.getItem("imperium.mode") || "simple",
    route: "home", status: null, setup: null, fleet: null, modelsList: [], harnesses: [], configWritesEnabled: false, cookbook: null, evaluations: null, config: null, budget: null, diagnostics: null, diagnosticsBusy: false,
    modelTab: "recommended", messages: loadChat(), chatDraft: "", renderedMsgCount: 0, chatModel: localStorage.getItem("imperium.chatModel") || "auto", busy: false, aborter: null,
    wizardStep: savedSetup.step, wizard: {runtime:"native", launch:"background", backend:"", channel:"recommended", modelId:"", existingUrl:"", confirmDownloads:false, confirmConfig:false, preferLocal:false, scanned:false,...savedSetup.wizard}, setupPlan: savedSetup.plan, setupFailure: null, setupRunning: false, events: [], eventCursor: 0, progress: 0,
    apiKey: loadApiKey(), fleetQuery: "", routingRole: "chat", routingStrategy: "balanced_local", routingDecision: null, error: "", statusSeq: 0, appliedStatusSeq: 0, appliedServerGen: 0, lastStatusOkAt: 0, statusStale: false
  };
  function saveSetup() { try { localStorage.setItem("imperium.setup",JSON.stringify({step:state.wizardStep,wizard:state.wizard,plan:state.setupPlan})); } catch (_) {} }
  function setWizardStep(step) {
    state.wizardStep=Math.min(6,Math.max(1,step));
    state.route=setupRoutes[state.wizardStep-1];
    history.replaceState(null,"",`#/${state.route}`);
    saveSetup();
  }
  const t = key => (dictionary[state.lang] && dictionary[state.lang][key]) || dictionary.en[key] || key;
  function applyPreferences() {
    document.documentElement.dataset.theme = state.theme;
    document.documentElement.lang = state.lang;
    document.documentElement.dir = state.lang === "he" ? "rtl" : "ltr";
    document.getElementById("theme-value").textContent = t(state.theme);
    document.getElementById("language-value").textContent = state.lang === "he" ? "עברית" : "English";
    document.querySelectorAll("[data-i18n]").forEach(node => node.textContent = t(node.dataset.i18n));
  }
  function request(path, options={}) {
    const headers = {Accept:"application/json", ...(options.headers || {})};
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    if (state.apiKey) headers.Authorization = `Bearer ${state.apiKey}`;
    if (path.startsWith("/setup/")) headers["X-Setup-Token"] = SETUP_TOKEN;
    return fetch(path, {...options, headers}).then(async response => {
      const type = response.headers.get("content-type") || "";
      const payload = type.includes("json") ? await response.json() : await response.text();
      if (!response.ok) { const message=payload?.error?.message||payload?.detail||(typeof payload?.error==="string"?payload.error:"")||(typeof payload==="string"?payload:"")||response.statusText; throw Object.assign(new Error(message),{status:response.status,payload,code:payload?.error?.code,retryAfter:Number(response.headers.get("retry-after"))||null}); }
      return payload;
    });
  }
  function isStatusStale() {
    if (state.error || state.statusStale) return true;
    if (!state.lastStatusOkAt) return false;
    const ttlMs = Math.max(5, Number(state.status?.freshness?.stale_after_seconds || 30)) * 1000;
    return (Date.now() - state.lastStatusOkAt) > ttlMs;
  }
  async function fetchStatus() {
    const seq = ++state.statusSeq;
    try {
      const status = await request("/ui/status");
      if (seq < state.appliedStatusSeq) return { applied: false, status: state.status };
      const serverGen = Number(status?.freshness?.generation || 0);
      if (serverGen > 0 && state.appliedServerGen > 0 && serverGen < state.appliedServerGen && seq <= state.appliedStatusSeq) return { applied: false, status: state.status };
      const prevChanged = gpuRenderKey(status) !== gpuRenderKey(state.status) || Boolean(state.error) || state.statusStale || status?.overall !== state.status?.overall;
      state.appliedStatusSeq = seq;
      if (serverGen > 0) state.appliedServerGen = serverGen;
      state.status = status;
      state.setup = status?.setup || state.setup;
      state.lastStatusOkAt = Date.now();
      state.statusStale = false;
      state.error = "";
      return { applied: true, changed: prevChanged, status };
    } catch (error) {
      if (seq < state.appliedStatusSeq) return { applied: false, error };
      const hadError = Boolean(state.error);
      state.appliedStatusSeq = seq;
      state.error = error.message || t("statusUnavailable");
      state.statusStale = state.lastStatusOkAt > 0;
      return { applied: true, changed: !hadError || state.statusStale, error };
    }
  }
  async function loadData() {
    state.configWritesEnabled=false;
    const statusPromise = fetchStatus();
    const jobs = [
      ["modelsList",()=>request("/v1/models").then(x=>x.data||[])],
      ["harnesses",()=>request("/harnesses").then(x=>{state.configWritesEnabled=x.config_writes_enabled===true;return x.harnesses||x.items||[];})], ["fleet",()=>request("/fleet/status")],
      ["config",()=>request("/config/preview")], ["cookbook",()=>request("/cookbook")],
      ["budget",()=>request("/compute/budget").catch(()=>null)],
      ["evaluations",()=>request("/routing/evaluations")]
    ];
    const loadMeta={};
    await Promise.all([statusPromise, ...jobs.map(async ([key,job]) => { try { state[key] = await job(); loadMeta[key]="ok"; } catch (error) { loadMeta[key]=`fail:${error.status||""}:${error.message}`; } })]);
    state.setup=state.status?.setup||state.setup;
    if (!state.wizard.backend && state.setup?.recommended_backend) state.wizard.backend = state.setup.recommended_backend;
    if (state.setup?.platform_support?.status !== "supported" && state.wizard.runtime === "native") state.wizard.runtime = "existing";
    if (!state.wizard.modelId && state.setup?.recommendation?.id && (!state.wizard.preferLocal || state.setup.recommendation.local)) state.wizard.modelId = state.setup.recommendation.id;
    if (!state.wizard.existingUrl && state.setup?.discovery?.servers?.[0]?.url) state.wizard.existingUrl = state.setup.discovery.servers[0].url;
    if (!chatModelOptions().some(row=>row.id===state.chatModel)) { state.chatModel="auto"; localStorage.setItem("imperium.chatModel","auto"); }
    if (state.status?.setup_api_active===false&&state.setup?.setup_complete&&state.route.startsWith("setup")) { state.route=state.status?.overall==="ready"?"chat":"models"; history.replaceState(null,"",`#/${state.route}`); }
    if (state.wizardStep===5&&state.status?.setup_api_active!==false) { try { state.setupPlan=await request("/setup/plan",{method:"POST",body:JSON.stringify(setupPayload())}); } catch (_) {} }
    if (state.wizardStep===6 && state.setup?.setup_complete) state.progress=1;
    else if (state.wizardStep===6&&state.status?.setup_api_active!==false) { try { const payload=await request("/setup/events?after=0");state.events=(payload.events||[]).slice(-50);state.eventCursor=Number(payload.cursor??state.events.at(-1)?.id??0);const progress=state.events.filter(event=>typeof event.progress==="number").at(-1)?.progress;if(typeof progress==="number")state.progress=Math.min(.99,progress); } catch (_) {} }
    if (state.route.startsWith("setup")) saveSetup();
    render();
  }
  let gpuPollBusy=false;
  function gpuRenderKey(status) {
    const gpu=status?.hermes_gpu;if(!gpu)return "";
    return JSON.stringify([gpu.desired,gpu.loaded,gpu.phase,gpu.attempt,gpu.next_attempt_at,gpu.failure_code,gpu.warning,gpu.effective_n_ctx,gpu.context_source,gpuActionActive(gpu)?Math.floor((gpu.duration_ms||0)/5000):0]);
  }
  async function pollGpuStatus(force=false) {
    if(gpuPollBusy&&!force)return;
    gpuPollBusy=true;
    try { const outcome=await fetchStatus(); if(outcome.applied&&(outcome.changed||force))render(); }
    finally { gpuPollBusy=false; }
  }
  function toast(message) {
    const node = document.createElement("div"); node.className="toast"; node.textContent=message;
    document.getElementById("toasts").appendChild(node); setTimeout(()=>node.remove(),3200);
  }
  function announce(message) { document.getElementById("live").textContent=message; }
  function statusLabel() {
    if (state.error) return state.statusStale ? t("staleStatus") : t("offline");
    if (!state.status) return t("loading");
    if (isStatusStale()) return t("staleStatus");
    return state.status.overall === "ready" ? t("ready") : state.status.overall === "needs_attention" ? t("needsAttention") : t("setupRequired");
  }
  function navButton([route,icon,label]) {
    const active = state.route === route || (route === "models" && state.route.startsWith("models/"));
    return `<button class="nav-button ${active?"active":""}" data-route="${route}" ${active?'aria-current="page"':""}>${I(icon)}<span>${t(label)}</span></button>`;
  }
  function renderNav() {
    document.getElementById("simple-nav").innerHTML = navSimple.map(navButton).join("");
    document.getElementById("advanced-nav").innerHTML = navAdvanced.map(navButton).join("");
    const isAdvanced = state.route.startsWith("advanced/");
    document.getElementById("advanced-nav").classList.toggle("open", isAdvanced);
    document.getElementById("advanced-toggle").setAttribute("aria-expanded", String(isAdvanced));
  }
  function pageHeader(title,copy,actions="",eyebrow="") { return `<header class="page-header"><div>${eyebrow?`<div class="eyebrow">${esc(eyebrow)}</div>`:""}<h1>${esc(title)}</h1><p>${esc(copy)}</p></div>${actions}</header>`; }
  function localized(value) { return esc(textValue(value)); }
  function statusIssues() {
    const issues=[...(state.status?.blocking_issues||[]),...(state.status?.optional_issues||[])];
    if(state.error)issues.unshift({code:"status_unavailable",category:"system",severity:"blocking",message:t("statusUnavailable"),action:{code:"refresh_status",label:t("refresh")}});
    return issues;
  }
  function issueRows(issues) {
    return issues.map(issue=>{const refresh=issue.action?.code==="refresh_status";const destination=(issue.action?.href||"#/advanced/diagnostics").replace(/^#\//,"");return `<div class="issue-row"><div><span class="badge ${issue.severity==="blocking"?"warning":""}">${esc(t(issue.severity))}</span> ${localized(issue.message)}<div class="muted mono">${esc(issue.code)}</div></div><button class="button small" ${refresh?'data-action="refresh-alerts"':`data-route="${esc(destination)}"`}>${localized(issue.action?.label)}</button></div>`;}).join("");
  }
  function issuesHtml(issues=statusIssues()) {
    if(!issues.length)return `<div class="status-banner success">${I("check-circle")}<div>${t("noAlerts")}</div></div>`;
    return ["configuration","system"].map(category=>{const rows=issues.filter(issue=>(issue.category||"system")===category);return rows.length?`<section class="alert-group"><div class="alert-group-title"><h3>${t(`${category}Issues`)}</h3><span class="badge">${rows.length}</span></div>${issueRows(rows)}</section>`:"";}).join("");
  }
  function renderAlerts() {
    const count=statusIssues().length;const button=document.getElementById("alerts-button");const badge=document.getElementById("alerts-count");const label=count?`${t("alerts")}: ${count}`:`${t("alerts")}: ${t("noAlerts")}`;
    button.classList.toggle("has-alerts",count>0);button.setAttribute("aria-label",label);button.title=label;badge.hidden=count===0;badge.textContent=count>99?"99+":String(count);
  }
  function readinessHtml() {
    const stages = state.status?.stages || [
      {code:"system",complete:true,label:{en:"System checked",he:"המערכת נבדקה"}}, {code:"runtime",complete:false,label:{en:"llama.cpp",he:"llama.cpp"}}, {code:"model",complete:false,label:{en:"Model",he:"מודל"}}, {code:"server",complete:false,label:{en:"Ready",he:"מוכן"}}
    ];
    const firstIncomplete = stages.findIndex(stage=>!stage.complete);
    return `<div class="readiness">${stages.map((stage,index)=>{
      const current = index === firstIncomplete; const icon=stage.complete?"check-circle":current?"warning-circle":"clock";
      const detail = stage.code === "system" ? (state.setup?.hardware?.system?.os || t("system")) : stage.code === "runtime" ? (stage.complete?t("installed"):t("setupRequired")) : stage.code === "model" ? (stage.complete?(state.status?.active?.model||t("installed")):t("setupRequired")) : (stage.complete?t("ready"):t("router"));
      return `<div class="stage ${stage.complete?"complete":current?"current":""}"><div class="stage-head"><span class="stage-icon">${I(icon)}</span><span class="stage-title">${localized(stage.label)}</span></div><div class="stage-detail">${esc(detail)}</div></div>`;
    }).join("")}</div>`;
  }
  function isGenericModel(id) { const key=String(id||"").trim().toLowerCase(); return !key || key==="local" || key==="auto" || key==="default"; }
  function modelRowMatches(row, id) {
    const needle=String(id||"").toLowerCase();
    const file=String(row.path||"").split(/[\\/]/).at(-1)||"";
    const stem=file.replace(/\.gguf$/i,"");
    return [row.id, row.name, stem, file].some(value => String(value||"").toLowerCase()===needle);
  }
  function runningModelDetails() {
    const id = String(state.status?.active?.model || "").trim();
    if (isGenericModel(id)) return null;
    const slot = (state.fleet?.slots || []).find(row => row.id === state.status?.active?.slot_id || row.model_id === id) || {};
    const local = (state.setup?.discovery?.local_models || []).find(row => modelRowMatches(row, id));
    const catalog = (state.setup?.models || []).find(row => modelRowMatches(row, id));
    const base = local ? localModelDetails(local) : catalog ? {...catalog} : {id, name: id, installed: true};
    const caps = (base.capabilities && base.capabilities.length ? base.capabilities : [slot.role, state.status?.active?.backend]).filter(Boolean);
    return {...base, name: id, capabilities: caps};
  }
  function gpuPhaseLabel(phase) {
    const key={idle:"phaseIdle",draining:"phaseDraining",waiting_for_requests:"phaseWaitingForRequests",stopping:"phaseStopping",loading:"phaseLoading",restoring:"phaseRestoring",backoff:"phaseBackoff",failed:"phaseFailed",stopped:"phaseStopped",busy:"phaseBusy",starting:"phaseLoading",auto_restarting:"phaseLoading"}[phase];
    return key?t(key):(phase||t("unknown"));
  }
  function gpuProfileForModel(model) {
    const id=String(model||"").toLowerCase();
    if(id==="local"||id==="ornith")return "ornith";
    if(id==="uncensored"||id==="qwen38_27b")return "qwen38_27b";
    return state.modelsList.find(row=>row.id===model)?.meta?.hermes_profile||null;
  }
  function hermesSelectionLabel(profile) { return profile==="qwen38_27b"?"uncensored":profile||t("unknown"); }
  function gpuActionActive(gpu=state.status?.hermes_gpu) { return !!gpu&&["draining","waiting_for_requests","stopping","loading","restoring","backoff","busy","starting","auto_restarting"].includes(gpu.phase); }
  function chatUsesGpu() {
    const gpu=state.status?.hermes_gpu;if(!gpu)return false;
    const selected=state.chatModel||"auto";
    const row=state.modelsList.find(item=>item.id===selected);
    const slot=row?.meta?.slot_id||row?.meta?.live?.slot_id;
    if(slot)return slot===gpu.slot_id;
    if(String(selected).includes("/"))return false;
    return state.status?.active?.slot_id===gpu.slot_id||!!gpuProfileForModel(selected);
  }
  function gpuChatBlocked() {
    const gpu=state.status?.hermes_gpu;if(!gpu||!chatUsesGpu())return false;
    if(gpuActionActive(gpu)||!gpu.loaded)return true;
    const requested=gpuProfileForModel(state.chatModel);
    return !!requested&&requested!==gpu.loaded;
  }
  function hermesGpuPanel() {
    const gpu=state.status?.hermes_gpu;if(!gpu)return "";
    const active=state.status?.active||{};
    const loadedGguf=active.slot_id===gpu.slot_id?(active.model||gpu.loaded):gpu.loaded;
    const context=gpu.effective_n_ctx?`${new Intl.NumberFormat(state.lang==="he"?"he-IL":"en-US").format(gpu.effective_n_ctx)} tokens · ${gpu.context_source||t("unknown")}`:t("unknown");
    const working=gpuActionActive(gpu);
    const failed=["failed","stopped"].includes(gpu.phase)||!!gpu.failure_code||(!gpu.loaded&&!working);
    const title=failed?t("gpuFailed"):working?t("gpuWorking"):t("gpuReady");
    const attempt=gpu.attempt?`<span>${t("loadAttempt")} ${esc(gpu.attempt)}/${esc(gpu.max_attempts||3)}</span>`:"";
    const retry=gpu.retry_after_seconds?`<span>${t("retryIn")} ${esc(gpu.retry_after_seconds)} ${t("seconds")}</span>`:"";
    const elapsed=working?`<span>${t("elapsed")}: ${esc(Math.max(0,Math.round((gpu.duration_ms||0)/1000)))} ${t("seconds")}</span>`:"";
    const code=gpu.failure_code?`<span class="mono">${esc(gpu.failure_code)}</span>`:"";
    return `<section class="status-banner ${failed||working?"warning":"success"}" data-hermes-gpu-phase="${esc(gpu.phase||"idle")}">${I(failed?"warning-circle":working?"arrow-clockwise":"check-circle")}<div style="flex:1"><strong>${t("hermesGpuStatus")} · ${title}</strong><div class="meta-line" style="margin-top:8px"><span>${t("hermesSelection")}: <b>${esc(hermesSelectionLabel(gpu.desired))}</b></span><span>${t("connectionPin")}: <b>${esc(hermesConnection()?.model||t("unknown"))}</b></span><span>${t("loadedGguf")}: <b>${esc(loadedGguf||t("unknown"))}</b></span><span>${t("swapPhase")}: ${esc(gpuPhaseLabel(gpu.phase))}</span><span>${t("measuredContext")}: ${esc(context)}</span>${attempt}${retry}${elapsed}${code}</div>${gpuChatBlocked()?`<div class="muted" style="margin-top:8px">${t("sendBlockedLoading")}</div>`:""}</div></section>`;
  }
  function modelCardHome() {
    const model = runningModelDetails();
    if (!model) return `<section><div class="section-title"><h2>${t("runningNow")}</h2><p>${t("runningNowCopy")}</p></div><div class="status-banner warning">${I("warning-circle")}<div><strong>${t("noRunningModel")}</strong><div class="muted">${t("noRunningModelCopy")}</div></div></div></section>`;
    const size = model.size_gb === 0 || model.size_gb ? `${esc(model.size_gb)} GB` : "";
    const fit = textValue(model.fit_reason_i18n) || model.fit_reason || model.quant || "";
    const caps = (model.capabilities || []).slice(0, 3).join(" · ");
    return `<section><div class="section-title"><h2>${t("runningNow")}</h2><p>${t("runningNowCopy")}</p></div><div class="model-recommendation"><div class="model-row"><span class="model-symbol">${I("cube")}</span><div><div class="model-name">${esc(model.name)} <span class="badge primary">${t("runningBadge")}</span></div><div class="meta-line">${size?`<span class="meta-item">${I("database")} ${size}</span>`:""}${fit?`<span class="meta-item">${I("memory")} ${esc(fit)}</span>`:""}${caps?`<span class="meta-item">${I("check-circle")} ${esc(caps)}</span>`:""}</div></div><button class="button" data-route="chat">${t("startChat")} ${I("arrow-right")}</button></div><div class="why">${I("info")}<div>${t("whyRunning")}</div></div></div></section>`;
  }
  function factsHtml() {
    const hw=state.setup?.hardware||{}; const gpu=hw.gpus?.[0]||{}; const ram=hw.ram||{}; const system=hw.system||{};
    const facts=[["desktop","system",`${system.os||t("unknown")} ${system.release||""}`],["graphics-card","gpu",gpu.name||t("unknown")],["memory","memory",ram.total_mb?`${Math.round(ram.total_mb/1024)} GB RAM`:t("unknown")],["code","runtime",state.setup?.discovery?.runtime_installed?t("installed"):t("setupRequired")]];
    return `<div class="facts">${facts.map(([icon,label,value])=>`<div class="fact">${I(icon)}<div><strong>${t(label)}</strong><span title="${esc(value)}">${esc(value)}</span></div></div>`).join("")}</div>`;
  }
  function homePage() {
    const stale=isStatusStale();
    const overall=state.status?.overall||"setup_required"; const ready=!stale&&overall==="ready"; const attention=stale||overall==="needs_attention";
    const title=ready?t("readyTitle"):attention?t("attentionTitle"):t("setupTitle"); const copy=ready?t("readySubtitle"):stale?t("statusUnavailable"):attention?t("attentionSubtitle"):t("setupSubtitle");
    const next=state.status?.next_action||{href:"#/setup",label:{en:"Continue setup",he:"המשך ההגדרה"}};
    const primaryBtn=stale?`<button class="button primary" data-action="refresh">${I("arrow-clockwise")} ${t("refresh")}</button>`:`<button class="button primary" data-route="${esc((next.href||"#/setup").replace(/^#\//,""))}">${localized(next.label)} ${I("arrow-right")}</button>`;
    const staleBanner=stale?`<div class="status-banner warning" data-status-freshness="${state.error?"unavailable":"stale"}">${I("warning-circle")}<div><strong>${esc(statusLabel())}</strong><div class="muted">${esc(t("statusUnavailable"))}${state.lastStatusOkAt?` · ${t("lastVerifiedAt")}: ${esc(new Intl.DateTimeFormat(state.lang==="he"?"he-IL":"en-US",{timeStyle:"medium"}).format(new Date(state.lastStatusOkAt)))}`:""}</div></div></div>`:"";
    const optional=state.status?.optional_issues||[];
    return `<div class="page">${pageHeader(title,copy,primaryBtn,statusLabel())}${staleBanner}${readinessHtml()}${hermesGpuPanel()}${modelCardHome()}${factsHtml()}<div class="quick-actions">${(state.status?.quick_actions||[]).map(action=>`<button class="button small" data-route="${esc((action.href||"").replace(/^#\//,""))}">${localized(action.label)}</button>`).join("")}</div>${optional.length?`<section class="additional"><h3>${t("additional")}</h3>${optional.map(issue=>`<div class="issue-row"><span>${localized(issue.message)}</span><button class="button small" data-route="${esc((issue.action?.href||"advanced/diagnostics").replace(/^#\//,""))}">${localized(issue.action?.label)}</button></div>`).join("")}</section>`:""}</div>`;
  }
  function localModelDetails(row) { const file=(row.path||"").split(/[\\/]/).at(-1);const report=(state.cookbook?.models||[]).find(item=>item.path===row.path||item.file===file)||{};return {...row,...report,id:row.id,name:row.name||report.file,installed:true,quant:report.quant||"GGUF",context:report.max_ctx_fit||report.n_ctx_train||"—",license:row.license||"—",fit:report.fit||"local"}; }
  function modelDetails(id) { const local=(state.setup?.discovery?.local_models||[]).find(row=>row.id===id);return local?localModelDetails(local):(state.setup?.models||[]).find(row=>row.id===id); }
  function modelRows(tab) {
    let rows=tab==="installed"?(state.setup?.discovery?.local_models||[]).map(localModelDetails):(state.setup?.models||[]);
    if (tab==="installed") rows=rows.filter(row=>row.installed);
    if (tab==="recommended") rows=rows.filter(row=>row.fit!=="incompatible");
    if (!rows.length) return `<div class="empty">${I("cube")}<div><h3>${tab==="downloads"?t("noDownloads"):t("noInstalled")}</h3></div></div>`;
    return `<div class="cards">${rows.map(model=>`<article class="card"><div class="card-icon">${I("cube")}</div><div class="model-name" style="font-size:18px">${esc(model.name)}</div><div class="meta-line"><span>${esc(model.quant)}</span><span>${esc(model.size_gb)} GB</span><span>${esc(model.context||"")} ctx</span></div><p class="muted" style="margin-top:14px">${esc(textValue(model.fit_reason_i18n)||model.fit_reason||model.path||"")}</p><div class="meta-line"><span class="badge ${model.fit==="incompatible"?"warning":"success"}">${t(model.fit==="incompatible"?"incompatible":model.installed?"installed":"available")}</span><span>${esc(model.license)}</span></div><div class="card-footer"><button class="button small" data-action="model-details" data-model="${esc(model.id)}">${t("details")}</button><button class="button primary small" data-action="${model.fit==="local"?"choose-chat-model":"select-model"}" data-model="${esc(model.id)}" ${model.fit==="incompatible"?"disabled":""}>${model.installed?t("activate"):t("download")}</button></div></article>`).join("")}</div>`;
  }
  function modelsPage() { const experiments=state.mode==="advanced"&&state.modelTab==="recommended"?(state.setup?.experimental_candidates||[]):[]; const folder=state.setup?.discovery?.models_dir||""; return `<div class="page">${pageHeader(t("modelPage"),t("modelPageCopy"),`<button class="button" data-action="models-folder">${I("folder-open")} ${t("changeFolder")}</button>`)}<div class="status-banner"><div><strong>${t("modelsFolder")}</strong><div class="muted">${t("modelsFolderCopy")}</div><div class="mono">${esc(folder)}</div></div></div><div class="tabs">${[["installed","installedTab"],["recommended","recommendedTab"],["downloads","downloadsTab"]].map(([id,label])=>`<button class="tab ${state.modelTab===id?"active":""}" data-model-tab="${id}">${t(label)}</button>`).join("")}</div>${state.modelTab==="downloads"?downloadsView():modelRows(state.modelTab)}${experiments.length?`<section style="margin-top:42px"><div class="section-title"><h2>${t("experimentalCandidates")}</h2><p>${t("experimentalCopy")}</p></div><div class="cards">${experiments.map(model=>`<article class="card"><div class="card-icon">${I("wrench")}</div><div class="model-name" style="font-size:18px">${esc(model.name)}</div><div class="meta-line"><span>${esc(Math.round(model.size_gb*1000))} MB</span><span>${esc(model.runtime)}</span><span>${esc(model.license)}</span></div><p class="muted" style="margin-top:14px">${esc((model.blockers||[])[0]||"")}</p><span class="badge warning">${t("experimentalDisabled")}</span></article>`).join("")}</div></section>`:""}</div>`; }
  function downloadsView() { return state.events.length?`<div class="table-wrap"><table><thead><tr><th>${t("status")||"Status"}</th><th>${t("details")}</th></tr></thead><tbody>${state.events.map(event=>`<tr><td><span class="badge">${esc(event.status)}</span></td><td>${esc(event.detail)}</td></tr>`).join("")}</tbody></table></div>`:`<div class="empty">${I("download-simple")}<div><h3>${t("noDownloads")}</h3></div></div>`; }
  function chatModelOptions() {
    const all=state.modelsList.length?state.modelsList:[{id:"auto"}];
    const visible=all.filter(row=>simpleModelAliases.includes(row.id)||row.meta?.hermes_gpu===true);
    for(const row of state.setup?.discovery?.local_models||[]) if(!visible.some(item=>item.id===row.id)) visible.push(row);
    return visible;
  }
  function chatModelLabel(id) { return ({auto:t("automatic"),chat:t("generalChat"),fast:t("fastModel"),coder:t("codingModel")})[id]||id; }
  function chatPage() {
    const options=chatModelOptions();
    const blocked=gpuChatBlocked();
    const canSend=Boolean((state.chatDraft||"").trim())&&!state.busy&&!blocked;
    const messages=state.messages.length?state.messages.map(message=>`<div class="message ${message.role}">${esc(message.content)}${message.truncated?`<div class="status-banner warning" style="margin:10px 0 0">${I("warning-circle")}<div>${t("outputTruncated")}</div></div>`:""}${message.routing?`<details class="routing-detail"><summary>${t("routing")}</summary>${esc(message.routing)}</details>`:""}${message.error?`<div style="margin-top:10px"><button class="button small" data-action="retry-chat">${I("arrow-clockwise")} ${t("retry")}</button></div>`:""}</div>`).join(""):`<div class="empty">${I("chat-circle-dots")}<div><h3>${t("emptyChat")}</h3><p>${t("emptyChatCopy")}</p><div class="prompt-chips">${["prompt1","prompt2","prompt3"].map(key=>`<button class="prompt-chip" data-prompt="${esc(t(key))}" ${blocked?"disabled":""}>${t(key)}</button>`).join("")}</div></div></div>`;
    return `<div class="page chat-layout"><div><div class="chat-toolbar"><div><h2>${t("chatPage")}</h2><span class="muted">${t("chatPageCopy")}</span></div><div style="display:flex;gap:8px"><label class="sr-only" for="chat-model">${t("chooseModel")}</label><select class="select" id="chat-model" style="width:auto">${options.map(row=>`<option value="${esc(row.id)}" ${state.chatModel===row.id?"selected":""}>${esc(state.mode==="advanced"?row.id:chatModelLabel(row.id))}</option>`).join("")}</select><button class="button small" data-action="clear-chat">${I("trash")} ${t("clear")}</button></div></div>${hermesGpuPanel()}</div><div class="chat-messages" id="chat-messages">${messages}</div><div class="composer"><form class="composer-box" id="chat-form"><label class="sr-only" for="chat-input">${t("chatPage")}</label><textarea id="chat-input" placeholder="${esc(blocked?t("sendBlockedLoading"):t("emptyChat"))}" ${state.busy||blocked?"disabled":""}>${esc(state.chatDraft||"")}</textarea><button class="icon-button" id="chat-send" type="${state.busy?"button":"submit"}" ${state.busy?'data-action="stop-chat"':canSend?"":"disabled"} aria-label="${state.busy?t("stopGeneration"):t("send")}">${I(state.busy?"stop":"paper-plane-tilt")}</button></form></div></div>`;
  }
  const integrations=[
    {id:"openai",name:"OpenAI SDK",icon:"code",model:"auto",copy:{en:"Python and JavaScript clients using a custom base URL.",he:"לקוחות Python ו־JavaScript עם כתובת בסיס מותאמת."}},
    {id:"open-webui",name:"Open WebUI",icon:"chat-circle-dots",model:"auto",copy:{en:"A friendly local chat interface connected through OpenAI compatibility.",he:"ממשק צ׳אט מקומי שמתחבר דרך תאימות OpenAI."}},
    {id:"claude-code",name:"Claude Code MCP",icon:"terminal-window",model:"coder",copy:{en:"Route coding and tool tasks through the local gateway.",he:"ניתוב משימות קוד וכלים דרך השער המקומי."}},
    {id:"aider",name:"Aider",icon:"git-branch",model:"coder",copy:{en:"Use a local coding model in your existing repository.",he:"שימוש במודל קוד מקומי בתוך המאגר הקיים."}},
    {id:"hermes",name:"Hermes",icon:"circles-four",model:"auto",copy:{en:"Connect Hermes to the Imperium harness path.",he:"חיבור Hermes לנתיב ה־harness של Imperium."}},
    {id:"vercel",name:"Vercel AI SDK",icon:"file-code",model:"auto",copy:{en:"Use the OpenAI-compatible provider from a web app.",he:"שימוש בספק תואם OpenAI מאפליקציית Web."}},
    {id:"dify",name:"Dify",icon:"tree-structure",model:"auto",copy:{en:"Connect Dify workflows to a private local model.",he:"חיבור תהליכי Dify למודל מקומי ופרטי."}},
    {id:"generic",name:"Generic API",icon:"plugs-connected",model:"auto",copy:{en:"For any OpenAI-compatible application.",he:"לכל אפליקציה שתואמת ל־OpenAI."}}
  ];
  function laneCards() { const lanes=Object.entries(state.fleet?.queues||{});return lanes.length?`<div class="cards">${lanes.map(([name,lane])=>`<article class="card"><h3 class="mono">${esc(name)}</h3><div class="meta-line"><span>${esc(lane.mode)}</span><span>${lane.active??"—"} active</span><span>${lane.queued??"—"} queued</span></div><p class="muted">limit ${lane.max_active??"delegated"} / queue ${lane.max_queue??"delegated"}</p></article>`).join("")}</div>`:""; }
  function harnessRows() { return state.harnesses.flatMap(item=>(item.connections||[]).map(connection=>{const lane=connection.model?.includes("/")?`upstream:${connection.model.split("/")[0]}`:"local";const capacity=state.fleet?.queues?.[lane];const evidence=connection.readiness?.evidence_level||connection.verification?.state||item.verification?.state||"not_seen";return [`${item.display_name||item.harness_id} · ${connection.name}`,connection.model,lane,capacity?.mode||"unknown",evidence];})); }
  function hermesHarness() { return state.harnesses.find(item=>(item.kind||item.harness_id)==="hermes"); }
  function hermesConnection() { const rows=hermesHarness()?.connections||[];return rows.find(item=>item.name==="default")||rows[0]; }
  function hermesPinTargets() {
    const targets=new Set();const current=hermesConnection()?.model;if(current)targets.add(current);
    for(const slot of state.fleet?.slots||[])if(slot.enabled!==false){const model=slot.model_id||slot.id;if(model)targets.add(model);}
    for(const row of state.modelsList||[])if(row.meta?.kind==="slot_model")targets.add(row.id);
    return [...targets];
  }
  function hermesPinPanel() {
    if(!state.configWritesEnabled||!hermesHarness()||!hermesConnection())return "";
    const current=hermesConnection().model;const targets=hermesPinTargets();
    if(!targets.length)return `<section style="margin-top:38px"><h2>${t("pinHermes")}</h2><p class="muted">${t("pinHermesUnavailable")}</p></section>`;
    return `<section style="margin-top:38px"><h2>${t("pinHermes")}</h2><div class="field"><label for="hermes-pin-model">${t("pinnedModel")}</label><select class="select" id="hermes-pin-model">${targets.map(model=>`<option value="${esc(model)}" ${model===current?"selected":""}>${esc(model)}</option>`).join("")}</select></div><button class="button primary" data-action="pin-hermes">${t("pinHermesSave")}</button></section>`;
  }
  function connectionsPage() { const rows=harnessRows();const pin=hermesConnection()?.model;return `<div class="page">${pageHeader(t("connectionsPage"),t("connectionsCopy"))}<div class="cards">${integrations.map(app=>`<article class="card"><div class="card-icon">${I(app.icon)}</div><h3>${esc(app.name)}</h3><p class="muted">${localized(app.copy)}</p><div class="card-footer"><span class="badge">${esc(app.id==="hermes"&&pin?pin:app.model)}</span><button class="button small" data-action="integration-details" data-integration="${app.id}">${t("connectApp")}</button></div></article>`).join("")}</div>${hermesPinPanel()}<section style="margin-top:38px"><h2>${t("activeConnections")}</h2>${table([t("connections"),t("model"),"Lane","Capacity",t("health")],rows)}</section></div>`; }
  function fleetPage() { const query=state.fleetQuery.toLowerCase();const rows=(state.fleet?.slots||[]).filter(row=>!query||Object.values(row).some(value=>String(value??"").toLowerCase().includes(query)));const control=state.fleet?.fleet_control||{};const controls=control.enabled&&control.supports_start_stop?`<div style="display:flex;gap:8px"><button class="button" data-action="fleet-start">${I("play")} ${t("startAll")}</button><button class="button danger" data-action="fleet-stop">${I("stop")} ${t("stopAll")}</button></div>`:"";const guidance=control.backend==="remote"?`<div class="status-banner warning">${I("info")}<div><strong>${t("externalFleet")}</strong><div class="muted">${t("externalFleetCopy")}</div></div></div>`:"";return `<div class="page">${pageHeader(t("fleetPage"),t("fleetCopy"),controls)}${guidance}${laneCards()}<div class="toolbar"><input class="input search" id="fleet-search" value="${esc(state.fleetQuery)}" placeholder="${t("search")||"Search"}"><button class="button small" data-action="refresh">${I("arrow-clockwise")} ${t("refresh")}</button></div>${table([t("slot"),t("role"),t("model"),t("backend"),t("health"),t("diagnostics")],rows.map(row=>{const runtime=row.runtime;const health=runtime?(runtime.healthy?"healthy":runtime.running?"running":"unhealthy"):row.health||"unknown";const diagnostics=runtime?.failure_code?`${runtime.failure_code}${runtime.exit_code==null?"":` (${runtime.exit_code})`}`:"—";return [row.id,row.role,row.model_id,row.backend_type,health,diagnostics];}))}</div>`; }
  function routingPage() { const aliases=state.modelsList.filter(row=>row.meta?.kind==="alias");const evaluated=state.evaluations?.payload?.models||[];const ranked=state.routingDecision?.ranked_candidates||[];return `<div class="page">${pageHeader(t("routingPage"),t("routingCopy"),`<button class="button" data-action="route-test">${I("git-branch")} ${t("testRoute")}</button>`)}${laneCards()}<div class="toolbar"><select class="select" id="routing-role">${["chat","utility","scribe","embed"].map(role=>`<option ${state.routingRole===role?"selected":""}>${role}</option>`).join("")}</select><select class="select" id="routing-strategy">${["balanced_local","fastest","quality","economy"].map(strategy=>`<option ${state.routingStrategy===strategy?"selected":""}>${strategy}</option>`).join("")}</select></div>${ranked.length?`<section><h2>Ranked candidates</h2>${table([t("model"),"Source","Score","Reasons"],ranked.map(row=>[row.model_id,row.source,row.score,(row.reason_codes||[]).join(", ")]))}</section>`:""}${table([t("alias"),t("mapsTo"),t("liveModel")],aliases.map(row=>[row.id,row.meta?.maps_to_role,row.meta?.live?.model||"—"]))}${evaluated.length?`<section style="margin-top:32px"><h2>Evaluated models</h2>${table([t("model"),t("role"),"Quality","Latency","Reliability"],evaluated.flatMap(model=>Object.entries(model.roles||{}).map(([role,score])=>[model.model_id,role,score.pass_rate,score.median_latency_ms,score.reliability])))}</section>`:""}</div>`; }
  function configurationPage() { return `<div class="page">${pageHeader(t("configurationPage"),t("configurationCopy"))}<div class="cards"><section class="card"><h3>${t("apiKey")}</h3><p class="muted">${t("savedBrowser")}</p><div class="field"><label for="api-key">${t("apiKey")}</label><input class="input mono" id="api-key" type="password" value="${esc(state.apiKey)}" autocomplete="off"></div><button class="button primary" data-action="save-api-key">${t("saveLocal")}</button></section><section class="card"><h3>${t("rawPreview")}</h3><p class="muted">${t("redactedPreview")}</p><button class="button" data-action="show-config">${I("file-code")} ${t("rawPreview")}</button></section><section class="card"><h3>${t("customHarnesses")}</h3><p class="muted">${t("customHarnessCopy")}</p><button class="button" data-action="open-harness">${I("plus")} ${t("addHarness")}</button></section></div></div>`; }
  function diagnosticsChecks() { const report=state.diagnostics;if(!report)return `<div class="empty">${I("heartbeat")}<div><h3>${t("checksNotRun")}</h3></div></div>`;const checks=report.doctor?.checks||[];const errors=report.collection_errors||[];return `${table(["Status","Check","Detail","Recovery"],checks.map(check=>[check.status,check.label,check.detail,check.remediation]))}${errors.length?`<div style="margin-top:18px">${table(["Component","Code"],errors.map(error=>[error.component,error.code]))}</div>`:""}`; }
  function diagnosticsPage() { const status=state.status||{};const issues=statusIssues();const disabled=state.diagnosticsBusy?"disabled":"";const actions=`<div style="display:flex;gap:8px;flex-wrap:wrap"><button class="button" data-action="run-diagnostics" ${disabled}>${I("heartbeat")} ${t("runDoctor")}</button><button class="button primary" data-action="export-diagnostics" ${disabled}>${I("download-simple")} ${t("exportDiagnostics")}</button></div>`;return `<div class="page">${pageHeader(t("diagnosticsPage"),t("diagnosticsCopy"),actions)}<div class="cards"><article class="card"><div class="card-icon">${I("heartbeat")}</div><h3>${statusLabel()}</h3><p class="muted">${(status.blocking_issues||[]).length} blocking · ${(status.optional_issues||[]).length} optional</p></article><article class="card"><div class="card-icon">${I("code")}</div><h3>Imperium v${esc(IMPERIUM_VERSION)}</h3><p class="muted mono">${esc(location.origin)}</p></article><article class="card"><div class="card-icon">${I("graphics-card")}</div><h3>${esc(status.hardware?.gpu||t("unknown"))}</h3><p class="muted">${status.hardware?.vram_mb?Math.round(status.hardware.vram_mb/1024)+" GB VRAM":t("unknown")}</p></article></div>${laneCards()}<section style="margin-top:32px"><h2>${t("checkResults")}</h2>${diagnosticsChecks()}</section><section style="margin-top:32px"><h2>${t("needsAttention")}</h2>${issuesHtml(issues)}</section></div>`; }
  function table(headers,rows) { return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.length?rows.map(row=>`<tr>${row.map(cell=>`<td>${esc(cell??"—")}</td>`).join("")}</tr>`).join(""):`<tr><td colspan="${headers.length}">${t("noRows")}</td></tr>`}</tbody></table></div>`; }
  function providerStatusBadge(status) {
    const cls={ok:"success",warn:"warning",exhausted:"danger",tracked:"primary"}[status]||"";
    const label={ok:t("statusOk"),warn:t("statusWarn"),exhausted:t("statusExhausted"),unknown:t("statusUnknown"),tracked:t("statusTracked")}[status]||status;
    return `<span class="badge ${cls}">${esc(label)}</span>`;
  }
  function providerSourceBadge(source) {
    const label={live:t("sourceLive"),declared_estimate:t("sourceEstimated"),ledger:t("sourceTracked"),none:t("sourceNone")}[source]||source||t("sourceNone");
    return `<span class="badge">${esc(label)}</span>`;
  }
  function resetLabel(resetsAt) {
    if (resetsAt===null||resetsAt===undefined||resetsAt==="") return t("unknown");
    let ms=null; const asNumber=typeof resetsAt==="number"?resetsAt:Number(resetsAt);
    if (!Number.isNaN(asNumber)&&asNumber>0) ms=asNumber>1e12?asNumber:asNumber*1000;
    else { const parsed=Date.parse(resetsAt); if (!Number.isNaN(parsed)) ms=parsed; }
    if (ms===null) return String(resetsAt);
    const diffMin=Math.round((ms-Date.now())/60000);
    if (diffMin<=0) return t("resetsIn");
    if (diffMin<60) return `${diffMin}m`;
    if (diffMin<2880) return `${Math.round(diffMin/60)}h`;
    return `${Math.round(diffMin/1440)}d`;
  }
  function providerWindowHtml(w) {
    if (w.source==="live") {
      const pct=Math.max(0,Math.min(100,Math.round(w.used_percent||0)));
      return `<div style="margin-top:14px"><div class="meta-line" style="justify-content:space-between"><span>${esc(w.label||"")}</span><span class="muted">${pct}% · ${t("resetsIn")} ${esc(resetLabel(w.resets_at))}</span></div><div class="progress-bar"><span style="width:${pct}%"></span></div></div>`;
    }
    const pct=Math.max(0,Math.min(100,Math.round(w.pct||0)));
    const usedLabel=w.max_tokens?`${esc(w.used_tokens??0)} / ${esc(w.max_tokens)} ${t("tokens")}`:w.max_requests?`${esc(w.used_requests??0)} / ${esc(w.max_requests)} ${t("requests")}`:`${pct}%`;
    const exhausts=typeof w.exhausts_in_hours==="number"?` · ~${Math.round(w.exhausts_in_hours)}h ${t("left")}`:"";
    return `<div style="margin-top:14px"><div class="meta-line" style="justify-content:space-between"><span>${esc(w.window||"")}</span><span class="muted">${usedLabel} · ${pct}%${exhausts}</span></div><div class="progress-bar"><span style="width:${pct}%"></span></div></div>`;
  }
  function providerCard(p) {
    const windows=(p.windows||[]).map(providerWindowHtml).join("");
    return `<article class="card"><div class="card-icon">${I("graphics-card")}</div><div class="model-name" style="font-size:18px">${esc(p.provider)}${p.enabled===false?`<span class="badge">${t("disabled")}</span>`:""}</div><div class="meta-line"><span>${esc(p.kind||"")}</span><span class="mono">${esc(p.invoke||"")}</span>${p.plan_type?`<span>${esc(p.plan_type)}</span>`:""}</div><div class="meta-line" style="margin-top:10px">${providerStatusBadge(p.status)}${providerSourceBadge(p.source)}</div>${p.status==="unknown"&&p.reason?`<p class="muted" style="margin-top:10px">${esc(p.reason)}</p>`:""}${windows||`<p class="muted" style="margin-top:14px">${t("noRows")}</p>`}</article>`;
  }
  function localCapacityHtml(local) {
    if (!local||local.available===false||!Object.keys(local).length) return "";
    const gpus=local.gpus||[]; const ram=local.ram||{}; const cpu=local.cpu||{};
    const gpuCards=gpus.length?gpus.map(gpu=>{
      const pct=gpu.total_vram_mb?Math.round((gpu.used_vram_mb/gpu.total_vram_mb)*100):0;
      return `<article class="card"><div class="card-icon">${I("graphics-card")}</div><h3>${esc(gpu.name||t("gpu"))}</h3><p class="muted">${Math.round((gpu.used_vram_mb||0)/1024)} / ${Math.round((gpu.total_vram_mb||0)/1024)} GB VRAM</p><div class="progress-bar"><span style="width:${pct}%"></span></div></article>`;
    }).join(""):`<article class="card"><div class="card-icon">${I("graphics-card")}</div><h3>${t("gpu")}</h3><p class="muted">${t("unknown")}</p></article>`;
    const ramPct=Math.round(ram.utilization_pct||0); const cpuPct=Math.round(cpu.utilization_pct||0);
    return `<section style="margin-top:38px"><div class="section-title"><h2>${t("thisComputer")}</h2></div><div class="cards">${gpuCards}<article class="card"><div class="card-icon">${I("memory")}</div><h3>${t("memory")}</h3><p class="muted">${Math.round((ram.used_mb||0)/1024)} / ${Math.round((ram.total_mb||0)/1024)} GB</p><div class="progress-bar"><span style="width:${ramPct}%"></span></div></article><article class="card"><div class="card-icon">${I("desktop")}</div><h3>CPU</h3><p class="muted">${cpuPct}%</p><div class="progress-bar"><span style="width:${cpuPct}%"></span></div></article></div></section>`;
  }
  function providersView() {
    const budget=state.budget; const providers=budget?.providers||[];
    const body=providers.length?`<div class="cards">${providers.map(providerCard).join("")}</div>`:`<div class="empty">${I("graphics-card")}<div><h3>${t("noProviders")}</h3></div></div>`;
    const banner=!budget?`<div class="status-banner warning">${I("warning-circle")}<div>${t("budgetUnavailable")}</div></div>`:"";
    return `<div class="page">${pageHeader(t("computeProviders"),t("computeProvidersCopy"),`<button class="button small" data-action="refresh">${I("arrow-clockwise")} ${t("refresh")}</button>`)}${banner}${body}${budget?localCapacityHtml(budget.local):""}</div>`;
  }
  function wizardSidebar() { const labels=["stepWelcome","stepHardware","stepRuntime","stepModel","stepReview","stepInstall"]; return `<aside class="wizard-side"><h2>Imperium setup</h2><div class="wizard-progress">${labels.map((label,index)=>`<div class="wizard-step ${state.wizardStep===index+1?"active":state.wizardStep>index+1?"done":""}"><span class="wizard-step-number">${state.wizardStep>index+1?"✓":index+1}</span><span>${t(label)}</span></div>`).join("")}</div></aside>`; }
  function option(value,current,title,copy,icon="",disabled=false,group="wizard-option") { return `<label class="option ${current===value?"selected":""} ${disabled?"disabled":""}"><input type="radio" name="${group}" value="${esc(value)}" ${current===value?"checked":""} ${disabled?"disabled":""}>${icon?I(icon):""}<span><strong>${esc(title)}</strong><span>${esc(copy)}</span></span></label>`; }
  function graphicsMemory(gpu) {
    const verifiedDedicated=gpu.source==="nvidia-smi"&&gpu.dedicated_vram_mb;
    const reported=gpu.reported_graphics_memory_mb||gpu.reported_memory_mb||(!verifiedDedicated&&gpu.source==="windows_cim"?gpu.dedicated_vram_mb:null);
    if (verifiedDedicated) return `${t("dedicatedGraphicsMemory")}: ${Math.round(verifiedDedicated/1024)} GB`;
    if (reported) return `${t("reportedGraphicsMemory")}: ${Math.round(reported/1024)} GB · ${t("sharedUnknown")}`;
    return t("unknown");
  }
  function hardwareSummary() { const hw=state.setup?.hardware||{}; const gpu=hw.gpus?.[0]||{}; return `<div class="cards"><article class="card"><div class="card-icon">${I("desktop")}</div><h3>${esc(hw.system?.os||t("unknown"))}</h3><p class="muted">${esc(hw.system?.release||"")}</p><span class="badge">${t("confidence")}: ${esc(hw.system?.confidence||"unknown")}</span></article><article class="card"><div class="card-icon">${I("graphics-card")}</div><h3>${esc(gpu.name||t("unknown"))}</h3><p class="muted">${esc(graphicsMemory(gpu))}</p><span class="badge">${esc(gpu.source||"fallback")}</span></article><article class="card"><div class="card-icon">${I("memory")}</div><h3>${state.setup?.hardware?.ram?.total_mb?Math.round(state.setup.hardware.ram.total_mb/1024)+" GB RAM":t("unknown")}</h3><p class="muted">${t("freeDisk")}: ${esc(hw.disk?.free_gb??t("unknown"))} GB</p></article></div>`; }
  function accelerationHtml(allowOverride=false) {
    const candidates=state.setup?.backend_candidates||[]; const row=candidates.find(item=>item.id===state.wizard.backend)||candidates.find(item=>item.recommended)||{};
    const capability=row.id?.startsWith("cuda")?"cuda":row.id==="rocm"?"hip":row.id; const evidence=state.setup?.hardware?.accelerator_evidence?.[capability]||{};
    const inferred=String(evidence.source||"").includes("inference")||(!["cpu",undefined].includes(row.id)&&row.confidence!=="high");
    const certainty=row.id==="cpu"?t("available"):inferred?t("inferred"):t("verified");
    const override=allowOverride?`<div class="field"><label for="backend-override">${t("backendOverride")}</label><select class="select" id="backend-override">${candidates.map(item=>`<option value="${esc(item.id)}" ${item.id===state.wizard.backend?"selected":""} ${item.eligible?"":"disabled"}>${esc(item.id.toUpperCase())} · ${esc(item.eligible?item.confidence:item.reason_code)}</option>`).join("")}</select></div><div class="field"><label for="runtime-channel">llama.cpp channel</label><select class="select" id="runtime-channel"><option value="recommended" ${state.wizard.channel==="recommended"?"selected":""}>${t("recommendedBuild")}</option><option value="latest" ${state.wizard.channel==="latest"?"selected":""}>${t("latestStable")}</option></select></div>`:"";
    return `<div class="acceleration">${I(row.id==="cpu"?"desktop":"graphics-card")}<div><strong>${t("acceleration")}: ${esc((row.id||"cpu").toUpperCase())}</strong><div class="muted">${certainty}</div></div></div><details class="technical-details"><summary>${t("technicalDetails")}</summary><div class="detail-body"><div>${t("evidence")}: <span class="mono">${esc(evidence.source||"cpu")}</span></div><div>${t("confidence")}: ${esc(row.confidence||evidence.confidence||"unknown")}</div>${override}</div></details>`;
  }
  function wizardContent() {
    const step=state.wizardStep; const model=modelDetails(state.wizard.modelId)||state.setup?.recommendation;
    if (step===1) return `<h1>${t("wizardWelcome")}</h1><p>${t("wizardWelcomeCopy")}</p><div class="language-choice" role="group" aria-label="${t("language")}"><button class="button small" data-language="en" aria-pressed="${state.lang==="en"}">${t("english")}</button><button class="button small" data-language="he" aria-pressed="${state.lang==="he"}">${t("hebrew")}</button></div><div class="status-banner" style="margin-top:20px">${I("info")}<div>${t("setupPrinciples")}</div></div>`;
    if (step===2) return state.wizard.scanned?`<h1>${t("detectedHardware")}</h1><p>${t("detectedHardwareCopy")}</p>${hardwareSummary()}${accelerationHtml()}<button class="button small ghost" style="margin-top:16px" data-action="scan-system">${I("magnifying-glass")} ${t("scanAgain")}</button>`:`<h1>${t("scanSystem")}</h1><p>${t("scanning")}</p><button class="button primary" data-action="scan-system" ${state.busy?"disabled":""}>${I("magnifying-glass")} ${t("scanSystem")}</button>`;
    if (step===3) {
      const planned=state.setup?.platform_support?.status!=="supported";
      const runtimeOptions=`<div class="wizard-options">${option("native",state.wizard.runtime,t("native"),planned?t("plannedPlatform"):t("nativeCopy"),"code",planned,"runtime-option")}${option("existing",state.wizard.runtime,t("existing"),t("existingCopy"),"plugs-connected",false,"runtime-option")}${option("advanced",state.wizard.runtime,t("advancedChoice"),t("advancedCopy"),"gear-six",false,"runtime-option")}</div>`;
      const existing=state.wizard.runtime==="existing"?`<div class="field" style="margin-top:18px"><label for="existing-url">${t("endpoint")}</label><input class="input mono" id="existing-url" value="${esc(state.wizard.existingUrl||"http://127.0.0.1:8080/v1")}"></div>`:"";
      const managed=state.wizard.runtime!=="existing"?`${accelerationHtml(state.mode==="advanced"||state.wizard.runtime==="advanced")}<fieldset class="choice-group"><legend>${t("launchBehavior")}</legend><div class="wizard-options">${option("background",state.wizard.launch,t("background"),t("backgroundCopy"),"play",false,"launch-option")}${option("terminal",state.wizard.launch,t("terminal"),t("terminalCopy"),"terminal-window",false,"launch-option")}</div></fieldset>`:"";
      return `<h1>${t("runtimeChoice")}</h1><p>${t("runtimeChoiceCopy")}</p>${runtimeOptions}${existing}${managed}`;
    }
    if (step===4) {
      if (state.wizard.runtime==="existing") return `<h1>${t("recommendedModel")}</h1><p>${t("existingModelCopy")}</p><div class="status-banner">${I("plugs-connected")}<div><strong>${t("automatic")}</strong><div class="muted mono">auto</div></div></div>`;
      const localModels=(state.setup?.discovery?.local_models||[]).map(localModelDetails);
      const recommendation=state.setup?.recommendation;
      const alternatives=(state.setup?.models||[]).filter(row=>row.id!==recommendation?.id&&row.fit!=="incompatible");
      const localOptions=localModels.map(row=>option(row.id,state.wizard.modelId,`${row.name} · ${row.quant}`,`${row.size_gb||"—"} GB · ${row.path}`,"cube",false,"model-option")).join("");
      const recommendedOption=recommendation?option(recommendation.id,state.wizard.modelId,`${recommendation.name} · ${recommendation.quant}`,`${recommendation.size_gb} GB · ${textValue(recommendation.fit_reason_i18n)||recommendation.fit_reason}`,"cube",false,"model-option"):"";
      return `<h1>${t(localModels.length?"chooseRecommended":"recommendedModel")}</h1><p>${t("chooseRecommendedCopy")}</p><div class="status-banner"><div><strong>${t("modelsFolder")}</strong><div class="mono">${esc(state.setup?.discovery?.models_dir||"")}</div></div><button class="button small" data-action="models-folder">${I("folder-open")} ${t("changeFolder")}</button></div>${localModels.length?`<h3>${t("installedTab")}</h3><div class="wizard-options">${localOptions}</div>`:""}${recommendedOption?`<h3 style="margin-top:24px">${t("recommended")}</h3><div class="wizard-options">${recommendedOption}</div>`:`<div class="status-banner warning">${I("warning-circle")}<div>${t("setupRequired")}</div></div>`}${state.mode==="advanced"&&alternatives.length?`<details class="technical-details"><summary>${t("otherModels")}</summary><div class="wizard-options">${alternatives.map(row=>option(row.id,state.wizard.modelId,`${row.name} · ${row.quant}`,`${row.size_gb} GB · ${textValue(row.fit_reason_i18n)||row.fit_reason}`,"cube",false,"model-option")).join("")}</div></details>`:""}`;
    }
    if (step===5) return `<h1>${t("reviewInstall")}</h1><p>${t("reviewInstallCopy")}</p><div class="card"><div class="model-name" style="font-size:19px">${esc(model?.name||(state.wizard.runtime==="existing"?t("existing"):t("model")))}</div><div class="meta-line">${model?`<span>${t("size")}: ${esc(model.size_gb||0)} GB</span><span>${t("license")}: ${esc(model.license||"—")}</span>`:""}<span>${t("acceleration")}: ${esc(state.wizard.runtime==="existing"?t("existing"):state.wizard.backend)}</span></div><p class="mono muted" style="margin-top:16px">${esc(state.setupPlan?.destination?.models||state.setup?.discovery?.models_dir||"")}</p><button class="button small" data-action="models-folder">${I("folder-open")} ${t("changeFolder")}</button>${state.setupPlan?.steps?`<ol class="muted">${state.setupPlan.steps.map(item=>`<li>${esc(item.action.replaceAll("_"," "))}</li>`).join("")}</ol>`:""}</div><div class="wizard-options">${setupNeedsDownload()?`<label class="option"><input id="confirm-downloads" type="checkbox" ${state.wizard.confirmDownloads?"checked":""}><span><strong>${t("confirmDownloads")}</strong><span>${esc(model?.source_url||t("existing"))}</span></span></label>`:""}<label class="option"><input id="confirm-config" type="checkbox" ${state.wizard.confirmConfig?"checked":""}><span><strong>${t("confirmConfig")}</strong><span>${esc(state.setupPlan?.destination?.config||state.setup?.discovery?.config_path||"")}</span></span></label></div>`;
    if (state.setupFailure) { const memory=state.setupFailure.code==="insufficient_available_memory"; return `<h1>${t("installationFailed")}</h1><p>${esc(state.setupFailure.message)}</p><div class="log" role="status" aria-live="polite">${state.events.map(event=>`[${esc(event.status)}] ${esc(event.detail)}`).join("\n")}</div><div class="wizard-actions"><button class="button ghost" data-action="use-existing-setup">${t("useExisting")}</button><button class="button primary" data-action="retry-setup">${I("arrow-clockwise")} ${memory?t("closeAppsRescan"):t("tryAgain")}</button></div>`; }
    if (!state.setupRunning&&state.progress>=1) return `<h1>${t("setupComplete")}</h1><p>${t("setupCompleteCopy")}</p><div class="status-banner success">${I("check-circle")}<div><strong>${t("ready")}</strong><div class="muted">runtime · model · config · route</div></div></div><div class="quick-actions"><button class="button primary" data-action="finish-setup">${t("startChat")} ${I("arrow-right")}</button></div>`;
    return `<h1>${t("installing")}</h1><p>${t("installingCopy")}</p><div class="progress-bar"><span style="width:${Math.round(state.progress*100)}%"></span></div><div class="log" role="status" aria-live="polite">${state.events.length?state.events.map(event=>`[${esc(event.status)}] ${esc(event.detail)}`).join("\n"):t("loading")}</div>${state.setupRunning?`<button class="button danger" style="margin-top:18px" data-action="cancel-setup">${t("cancel")}</button>`:`<button class="button primary" style="margin-top:18px" data-action="retry-setup">${I("arrow-clockwise")} ${t("tryAgain")}</button>`}`;
  }
  function wizardPage() {
    saveSetup();
    const canNext=state.wizardStep!==2||state.wizard.scanned;
    const runtimeReady=state.wizardStep!==3||(state.wizard.runtime==="existing"?Boolean(state.wizard.existingUrl||"http://127.0.0.1:8080/v1"):Boolean(state.wizard.backend));
    const modelReady=state.wizardStep!==4||state.wizard.runtime==="existing"||Boolean(state.wizard.modelId);
    const confirmed=state.wizardStep!==5||((!setupNeedsDownload()||state.wizard.confirmDownloads)&&state.wizard.confirmConfig);
    return `<div class="page"><div class="wizard-shell">${wizardSidebar()}<section class="wizard-body"><div class="wizard-content">${wizardContent()}</div>${state.wizardStep<6?`<div class="wizard-actions"><button class="button ghost" data-action="wizard-${state.wizardStep===1?"exit":"back"}">${state.wizardStep===1?t("exit"):t("back")}</button><button class="button primary" data-action="${state.wizardStep===5?"apply-setup":"wizard-next"}" ${canNext&&runtimeReady&&modelReady&&confirmed?"":"disabled"}>${state.wizardStep===5?t("installNow"):t("continue")} ${I("arrow-right")}</button></div>`:""}</section></div></div>`;
  }
  function render(opts={}) {
    const routeChanged=Boolean(opts.routeChanged);
    const mainEl=document.getElementById("main");
    const active=!routeChanged&&mainEl?.contains(document.activeElement)?document.activeElement:null;
    const focusSnap=active?.id?{id:active.id,value:"value" in active?active.value:null,start:typeof active.selectionStart==="number"?active.selectionStart:null,end:typeof active.selectionEnd==="number"?active.selectionEnd:null}:null;
    const prevComposer=!routeChanged?document.getElementById("chat-input"):null;
    const composerSel=prevComposer&&typeof prevComposer.selectionStart==="number"?[prevComposer.selectionStart,prevComposer.selectionEnd]:null;
    const prevWinY=!routeChanged?window.scrollY:0;
    const docEl=document.documentElement;
    const winAtBottom=routeChanged||(docEl.scrollHeight-window.scrollY-window.innerHeight<48);
    const prevMessages=!routeChanged?document.getElementById("chat-messages"):null;
    const wasAtBottom=!prevMessages||(prevMessages.scrollHeight-prevMessages.scrollTop-prevMessages.clientHeight<24);
    const prevScrollTop=prevMessages?prevMessages.scrollTop:0;
    const prevMsgCount=state.renderedMsgCount||0;
    renderNav(); applyPreferences(); renderAlerts();
    let html;
    if (state.route.startsWith("setup")) html=wizardPage(); else if (state.route==="home") html=homePage(); else if (state.route==="chat") html=chatPage(); else if (state.route.startsWith("models")) html=modelsPage(); else if (state.route==="connections") html=connectionsPage(); else if (state.route==="advanced/fleet") html=fleetPage(); else if (state.route==="advanced/routing") html=routingPage(); else if (state.route==="advanced/providers") html=providersView(); else if (state.route==="advanced/configuration") html=configurationPage(); else html=diagnosticsPage();
    mainEl.innerHTML=html;
    const msgAdded=state.messages.length>prevMsgCount;
    state.renderedMsgCount=state.messages.length;
    window.scrollTo(0,routeChanged?0:prevWinY);
    const nextComposer=document.getElementById("chat-input");
    if (nextComposer&&composerSel) { try { nextComposer.setSelectionRange(composerSel[0],composerSel[1]??composerSel[0]); } catch(_){} }
    if (focusSnap) {
      const nextEl=document.getElementById(focusSnap.id);
      if (nextEl&&!nextEl.disabled) {
        if (focusSnap.value!==null&&"value" in nextEl&&focusSnap.id!=="chat-input"&&nextEl.value!==focusSnap.value) nextEl.value=focusSnap.value;
        nextEl.focus({preventScroll:true});
        if (focusSnap.start!==null&&typeof nextEl.setSelectionRange==="function") { try { nextEl.setSelectionRange(focusSnap.start,focusSnap.end??focusSnap.start); } catch(_){} }
      }
    } else if (routeChanged) {
      mainEl.focus({preventScroll:true});
    }
    requestAnimationFrame(()=>{
      const messages=document.getElementById("chat-messages");
      if(state.route==="chat"&&(routeChanged||(msgAdded&&(winAtBottom&&wasAtBottom)))){
        if(messages)messages.scrollTop=messages.scrollHeight;
        if(msgAdded)window.scrollTo(0,document.documentElement.scrollHeight);
      } else {
        if(messages&&messages.scrollHeight>messages.clientHeight)messages.scrollTop=prevScrollTop;
        if(!routeChanged)window.scrollTo(0,prevWinY);
      }
    });
  }
  function routeTo(route) {
    closeDrawer();
    route=route||"home";
    if (route==="setup"&&state.wizardStep>1) route=setupRoutes[state.wizardStep-1];
    state.route=route;
    if (setupStepByRoute[route]) state.wizardStep=setupStepByRoute[route];
    history.replaceState(null,"",`#/${route}`);
    document.getElementById("sidebar").classList.remove("open");
    document.getElementById("mobile-menu").setAttribute("aria-expanded","false");
    if (route.startsWith("setup")) saveSetup();
    render({routeChanged:true});
  }
  function openDrawer(title,body) { const drawer=document.getElementById("drawer");if(!drawer.classList.contains("open"))drawerReturnFocus=document.activeElement;document.getElementById("drawer-title").textContent=title;document.getElementById("drawer-body").innerHTML=body;drawer.classList.add("open");requestAnimationFrame(()=>drawer.querySelector('[data-action="close-drawer"]')?.focus()); }
  function closeDrawer() { const drawer=document.getElementById("drawer");const wasOpen=drawer.classList.contains("open");drawer.classList.remove("open");if(wasOpen&&drawerReturnFocus?.isConnected)drawerReturnFocus.focus();drawerReturnFocus=null; }
  function openAlerts() { openDrawer(t("alerts"),`<p class="muted">${t("alertsCopy")}</p>${issuesHtml()}`); }
  function setupNeedsDownload() { return (state.setupPlan?.steps||[]).some(step=>step.action==="install_runtime"||step.action==="download_model"); }
  function setupPayload(confirm=false) { return {backend:state.wizard.runtime==="existing"?"existing":state.wizard.backend,runtime_channel:state.wizard.channel,model_id:state.wizard.runtime==="existing"?null:state.wizard.modelId,existing_url:state.wizard.existingUrl,launch_mode:state.wizard.launch,...(confirm?{confirm_download:!setupNeedsDownload()||state.wizard.confirmDownloads,confirm_write:state.wizard.confirmConfig}:{})}; }
  async function scanSystem() { state.busy=true; render(); try { state.setup=await request("/setup/scan",{method:"POST"}); state.wizard.backend=state.setup.recommended_backend||""; state.wizard.runtime=state.setup.platform_support?.status==="supported"?"native":"existing"; state.wizard.modelId=state.setup.recommendation?.id||""; state.wizard.scanned=true;setWizardStep(2); } catch(error){ toast(error.message); } finally { state.busy=false;saveSetup();render(); } }
  async function preparePlan() { try { state.setupPlan=await request("/setup/plan",{method:"POST",body:JSON.stringify(setupPayload())});setWizardStep(5);render(); } catch(error){toast(error.message);} }
  async function pollEvents() { try { const payload=await request(`/setup/events?after=${state.eventCursor||0}`); if(payload.resync_required){ state.events=(payload.events||[]).slice(-50); state.eventCursor=Number(payload.cursor??state.events.at(-1)?.id??0); const progress=state.events.filter(e=>typeof e.progress==="number").at(-1)?.progress; if(typeof progress==="number")state.progress=progress; render(); return; } if(payload.events?.length){ for(const event of payload.events){ if(Number(event.id||0)<=(state.eventCursor||0))continue; const last=state.events.at(-1);if(last&&last.stage===event.stage&&last.status===event.status&&last.detail===event.detail)state.events[state.events.length-1]=event;else state.events.push(event);}state.events=state.events.slice(-50);state.eventCursor=Math.max(state.eventCursor||0,Number(payload.cursor??payload.events.at(-1)?.id??0));const progress=payload.events.filter(e=>typeof e.progress==="number").at(-1)?.progress;if(typeof progress==="number")state.progress=progress;render(); } else if(typeof payload.cursor==="number"){ state.eventCursor=Math.max(state.eventCursor||0,payload.cursor); } } catch(_){} }
  async function applySetup() { if(state.setupRunning)return;state.setupRunning=true;setWizardStep(6);state.setupFailure=null;state.events=[];state.eventCursor=0;state.progress=.02;render();const timer=setInterval(pollEvents,700);try{const result=await request("/setup/apply",{method:"POST",body:JSON.stringify(setupPayload(true))});clearInterval(timer);state.setup=result.state||state.setup;state.progress=1;announce(t("setupComplete"));await loadData();}catch(error){state.setupFailure={code:error.payload?.error,message:error.message,remediation:error.payload?.remediation||[]};state.events.push({status:"error",detail:error.message});}finally{state.setupRunning=false;clearInterval(timer);saveSetup();render();} }
  async function sendChat(prompt) { const fromInput=prompt===undefined; const raw=fromInput?(document.getElementById("chat-input")?.value??state.chatDraft):prompt; const content=String(raw||"").trim(); if(!content||state.busy)return;if(gpuChatBlocked()){toast(t("sendBlockedLoading"));await loadData();return;} if(fromInput)state.chatDraft=""; state.messages.push({role:"user",content});saveChat();state.busy=true; state.aborter=new AbortController(); render(); try { const response=await request("/v1/chat/completions",{method:"POST",signal:state.aborter.signal,body:JSON.stringify({model:state.chatModel||"auto",messages:state.messages.filter(m=>!m.error).map(({role,content})=>({role,content})),stream:false,max_tokens:1024})}); const choice=response.choices?.[0]||{};const message=choice.message||{}; const answer=(message.content||message.reasoning_content||choice.text||"").trim(); state.messages.push({role:"assistant",content:answer||"—",routing:response.model||"",truncated:choice.finish_reason==="length"}); } catch(error){ if(error.name!=="AbortError"){const loading=["gpu_swap_in_progress","gpu_model_changed","gpu_swap_failed","gpu_slot_stopped"].includes(error.code);state.messages.push({role:"assistant",content:loading?t("sendBlockedLoading"):`${t("chatFailed")} ${error.message}`,error:true});if(loading)await loadData();} } finally { saveChat();state.busy=false;state.aborter=null;render(); } }
  async function verifyConnection(model="auto") { try { await request("/v1/models"); toast(t("connectionOk")+` · ${model}`); } catch(error){ toast(`${t("connectionFailed")}: ${error.message}`); } }
  function downloadDiagnostics(report) { const stamp=String(report.generated_at||new Date().toISOString()).replace(/\.\d{3}(?=Z$)/,"").replace(/[-:]/g,"");const blob=new Blob([JSON.stringify(report,null,2)+"\n"],{type:"application/json"});const url=URL.createObjectURL(blob);const link=document.createElement("a");link.href=url;link.download=`imperium-diagnostics-${stamp}.json`;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),0); }
  async function runDiagnostics(exportReport=false) { if(state.diagnosticsBusy)return;state.diagnosticsBusy=true;render();try{const report=await request("/diagnostics/report");state.diagnostics=report;if(exportReport){downloadDiagnostics(report);toast(t("diagnosticsExported"));}else toast(t("checksComplete"));}catch(error){toast(error.message);}finally{state.diagnosticsBusy=false;render();} }
  async function testRouting(showDrawer=true) { try { const payload=await request("/routing/request",{method:"POST",body:JSON.stringify({agent_id:"dashboard",agent_type:"human",role:state.routingRole,task_type:state.routingRole==="embed"?"embedding":"chat",requested_model:"auto",routing_strategy:state.routingStrategy})});state.routingDecision=payload;render();if(showDrawer)openDrawer(t("testRoute"),`<div class="meta-line"><span>${esc(payload.selected_model||"—")}</span><span>${esc(payload.selected_source||"—")}</span><span>${esc(payload.routing_strategy||"—")}</span></div><p class="muted">${esc((payload.reason_codes||[]).join(" · "))}</p><details><summary>${t("technicalDetails")}</summary><pre class="json">${esc(JSON.stringify(payload,null,2))}</pre></details>`); } catch(error){toast(error.message);} }
  document.addEventListener("click", async event => {
    const language=event.target.closest("[data-language]")?.dataset.language;if(language){state.lang=language;localStorage.setItem("imperium.lang",state.lang);saveSetup();render();return;}
    const route=event.target.closest("[data-route]")?.dataset.route; if(route){closeDrawer();routeTo(route);return;}
    const action=event.target.closest("[data-action]")?.dataset.action; if(!action)return;
    if(action==="close-drawer")closeDrawer();
    if(action==="show-alerts")openAlerts();
    if(action==="refresh-alerts"){await loadData();openAlerts();}
    if(action==="wizard-exit")routeTo("home");
    if(action==="wizard-back"){setWizardStep(state.wizardStep-1);render();}
    if(action==="wizard-next"){
      if(state.wizardStep===3){const input=document.getElementById("existing-url");if(input)state.wizard.existingUrl=input.value.trim();setWizardStep(4);render();}
      else if(state.wizardStep===4)preparePlan();
      else{setWizardStep(state.wizardStep+1);render();}
    }
    if(action==="scan-system")scanSystem();
    if(action==="apply-setup")applySetup();
    if(action==="retry-setup"){if(state.setupFailure?.remediation?.includes("rescan")){state.setupFailure=null;await scanSystem();}else applySetup();}
    if(action==="use-existing-setup"){state.setupFailure=null;state.wizard.runtime="existing";setWizardStep(3);render();}
    if(action==="cancel-setup")request("/setup/cancel",{method:"POST",body:"{}"}).then(()=>toast(t("cancel"))).catch(error=>toast(error.message));
    if(action==="finish-setup"){localStorage.removeItem("imperium.setup");state.wizardStep=1;routeTo("chat");}
    if(action==="select-model"){const mid=event.target.closest("[data-model]").dataset.model;state.wizard.modelId=mid;setWizardStep(4);render();}
    if(action==="choose-chat-model"){const mid=event.target.closest("[data-model]").dataset.model;state.chatModel=mid;localStorage.setItem("imperium.chatModel",state.chatModel);routeTo("chat");}
    if(action==="model-details"){const model=modelDetails(event.target.closest("[data-model]").dataset.model);openDrawer(model?.name||t("details"),`<div class="meta-line"><span>${esc(model?.quant||"—")}</span><span>${esc(model?.size_gb||"—")} GB</span><span>${esc(model?.max_ctx_fit||model?.context||"—")} ctx</span></div><p class="muted">${esc((model?.reasons||[]).join(" · ")||model?.fit_reason||model?.path||"")}</p><details><summary>${t("technicalDetails")}</summary><pre class="json">${esc(JSON.stringify(model,null,2))}</pre></details>`);}
    if(action==="models-folder"){const folder=state.setup?.discovery?.models_dir||"";openDrawer(t("modelsFolder"),`<p class="muted">${t("modelsFolderCopy")}</p><div class="field"><label for="models-folder-path">${t("destination")}</label><input class="input mono" id="models-folder-path" value="${esc(folder)}"></div><button class="button primary" data-action="save-models-folder">${t("saveFolder")}</button>`);}
    if(action==="save-models-folder"){const path=document.getElementById("models-folder-path").value.trim();try{const payload=await request("/models/directory",{method:"POST",body:JSON.stringify({path})});state.setup={...(state.setup||{}),discovery:payload.discovery};const locals=payload.discovery?.local_models||[];if(locals.length&&!locals.some(row=>row.id===state.wizard.modelId)){state.wizard.modelId="";state.wizard.preferLocal=true;}closeDrawer();toast(t("saveFolder"));await loadData();if(state.setup?.recommendation?.local){state.wizard.modelId=state.setup.recommendation.id;state.wizard.preferLocal=true;}saveSetup();render();}catch(error){toast(error.message);}}
    if(action==="integration-details"){const app=integrations.find(row=>row.id===event.target.closest("[data-integration]").dataset.integration);const base=state.status?.api?.base_url||location.origin;const model=app.id==="hermes"?(hermesConnection()?.model||app.model):app.model;openDrawer(app.name,`<p class="muted">${localized(app.copy)}</p><div class="field"><label>${t("endpoint")}</label><div class="copy-field"><input class="input mono" readonly value="${esc(base)}/v1"><button class="button" data-copy="${esc(base)}/v1">${t("copy")}</button></div></div><div class="field"><label>${t("pinnedModel")}</label><div class="copy-field"><input class="input mono" readonly value="${esc(model)}"><button class="button" data-copy="${esc(model)}">${t("copy")}</button></div></div><button class="button primary" data-verify="${esc(model)}">${t("verify")}</button>`);}
    if(action==="pin-hermes"){const harness=hermesHarness();const connection=hermesConnection();const model=document.getElementById("hermes-pin-model")?.value;if(!harness||!connection||!model)return;request(`/harnesses/${encodeURIComponent(harness.harness_id)}/connections/${encodeURIComponent(connection.name)}`,{method:"PATCH",body:JSON.stringify({model})}).then(()=>{toast(t("pinHermesSaved"));return loadData();}).catch(error=>toast(error.message));}
    if(action==="open-harness")openDrawer(t("addHarness"),`<div class="field"><label for="harness-id">ID</label><input class="input mono" id="harness-id" value="custom_app"></div><div class="field"><label for="harness-name">${t("name")}</label><input class="input" id="harness-name" value="${t("customApp")}"></div><div class="field"><label for="harness-model">${t("pinnedModel")}</label><input class="input mono" id="harness-model" value="auto"></div><button class="button primary" data-action="create-harness">${t("addHarness")}</button>`);
    if(action==="create-harness"){const id=document.getElementById("harness-id").value.trim();const name=document.getElementById("harness-name").value.trim();const model=document.getElementById("harness-model").value.trim();request("/harnesses",{method:"POST",body:JSON.stringify({harness_id:id,display_name:name,kind:"custom",protocol:"openai",location:"host",connections:{default:{model}}})}).then(()=>{toast(t("saveLocal"));closeDrawer();loadData();}).catch(error=>toast(error.message));}
    if(action==="clear-chat"){state.messages=[];localStorage.removeItem("imperium.chat");render();}
    if(action==="stop-chat")state.aborter?.abort();
    if(action==="retry-chat"){const last=[...state.messages].reverse().find(m=>m.role==="user");state.messages=state.messages.filter(m=>!m.error);if(last)sendChat(last.content);}
    if(action==="refresh")loadData();
    if(action==="run-diagnostics")runDiagnostics(false);
    if(action==="export-diagnostics")runDiagnostics(true);
    if(action==="show-config")openDrawer(t("rawPreview"),`<pre class="json">${esc(JSON.stringify(state.config,null,2))}</pre>`);
    if(action==="save-api-key"){state.apiKey=document.getElementById("api-key").value.trim();saveApiKey(state.apiKey);toast(t("saveLocal"));loadData();}
    if(action==="fleet-start"||action==="fleet-stop"){if(action==="fleet-stop"&&!confirm(t("stopAll")+"?"))return;request(action==="fleet-start"?"/fleet/start":"/fleet/stop",{method:"POST",body:"{}"}).then(loadData).catch(error=>toast(error.message));}
    if(action==="route-test")testRouting();
  });
  document.addEventListener("change", event => {
    if(event.target.name==="runtime-option")state.wizard.runtime=event.target.value;
    if(event.target.name==="launch-option")state.wizard.launch=event.target.value;
    if(event.target.name==="model-option")state.wizard.modelId=event.target.value;
    if(event.target.id==="backend-override")state.wizard.backend=event.target.value;
    if(event.target.id==="runtime-channel")state.wizard.channel=event.target.value;
    if(event.target.id==="existing-url")state.wizard.existingUrl=event.target.value.trim();
    if(event.target.id==="confirm-downloads")state.wizard.confirmDownloads=event.target.checked;
    if(event.target.id==="confirm-config")state.wizard.confirmConfig=event.target.checked;
    if(event.target.closest(".wizard-shell")){saveSetup();render();}
    if(event.target.id==="chat-model"){state.chatModel=event.target.value;localStorage.setItem("imperium.chatModel",state.chatModel);render();}
    if(event.target.id==="routing-role"){state.routingRole=event.target.value;testRouting(false);}
    if(event.target.id==="routing-strategy"){state.routingStrategy=event.target.value;testRouting(false);}
  });
  document.addEventListener("submit", event => { if(event.target.id==="chat-form"){event.preventDefault();sendChat();} });
  document.addEventListener("input", event => { if(event.target.id==="chat-input"){state.chatDraft=event.target.value;const btn=document.getElementById("chat-send");if(btn)btn.disabled=!state.chatDraft.trim()||gpuChatBlocked();}if(event.target.id==="fleet-search"){state.fleetQuery=event.target.value;render();} });
  document.addEventListener("click", event => { const copy=event.target.closest("[data-copy]")?.dataset.copy;if(copy)navigator.clipboard.writeText(copy).then(()=>toast(t("copied")));const verify=event.target.closest("[data-verify]")?.dataset.verify;if(verify)verifyConnection(verify);const prompt=event.target.closest("[data-prompt]")?.dataset.prompt;if(prompt)sendChat(prompt);const tab=event.target.closest("[data-model-tab]")?.dataset.modelTab;if(tab){state.modelTab=tab;render();} });
  document.getElementById("theme-control").addEventListener("click",()=>{state.theme=state.theme==="light"?"dark":"light";localStorage.setItem("imperium.theme",state.theme);render();});
  document.getElementById("language-control").addEventListener("click",()=>{state.lang=state.lang==="en"?"he":"en";localStorage.setItem("imperium.lang",state.lang);render();});
  document.getElementById("advanced-toggle").addEventListener("click",event=>{const open=event.currentTarget.getAttribute("aria-expanded")!=="true";event.currentTarget.setAttribute("aria-expanded",String(open));document.getElementById("advanced-nav").classList.toggle("open",open);});
  document.getElementById("mobile-menu").addEventListener("click",event=>{const open=document.getElementById("sidebar").classList.toggle("open");event.currentTarget.setAttribute("aria-expanded",String(open));});
  document.getElementById("drawer").addEventListener("click",event=>{if(event.target.id==="drawer")closeDrawer();});
  document.addEventListener("keydown",event=>{if(event.key==="Escape"){closeDrawer();document.getElementById("sidebar").classList.remove("open");document.getElementById("mobile-menu").setAttribute("aria-expanded","false");}});
  document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")pollGpuStatus(true);});
  window.addEventListener("focus",()=>pollGpuStatus(true));
  window.addEventListener("online",()=>pollGpuStatus(true));
  function tick(){document.getElementById("clock").textContent=new Intl.DateTimeFormat(state.lang==="he"?"he-IL":"en-US",{dateStyle:"medium",timeStyle:"short"}).format(new Date());if(isStatusStale())render();}
  tick();setInterval(tick,30000);setInterval(pollGpuStatus,3000);
  const initial=location.hash.replace(/^#\//,"");state.route=initial||"home";if(setupStepByRoute[state.route])state.wizardStep=setupStepByRoute[state.route];if(state.route==="setup"&&savedSetup.step>1)state.route=setupRoutes[savedSetup.step-1];applyPreferences();renderNav();render({routeChanged:true});loadData();
})();
