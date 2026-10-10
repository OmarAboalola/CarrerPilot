"use strict";
const $ = id => document.getElementById(id);
const workspaceHintBase = $("workspaceHint").textContent;
let session = {}, profile = null, hasProfile = false, scanId = "", currentScan = null;
let draft = null, pendingContact = null, history = [], pollTimer = null, draftTimer = null;
let polling = false, offline = false, retryCount = 0, initialized = false, detailKind = "", draftStatusUnknown = false;
let pollAgain = false;
let lastErrorCode = "", connectionNoticeKind = "";
let draftRefreshWarning = "";
let profileVersion = "", profileFormVersion = "", profileFormBaseline = "", profileConflict = false, profileReadSequence = 0;
const draftEdits = new Map();
const editStorageKey = "careerpilot-draft-edits";
let editStorageAvailable = true;
let reloadWithRecoveredEdits = false;
try {
  const saved = JSON.parse(sessionStorage.getItem(editStorageKey) || "[]");
  if (Array.isArray(saved)) for (const [id,edit] of saved) {
    if (typeof id === "string" && edit && typeof edit.account === "string" &&
        ["to","subject","body"].every(key => typeof edit[key] === "string") &&
        edit.to.length <= 254 && edit.subject.length <= 250 && edit.body.length <= 6000) draftEdits.set(id,edit);
  }
} catch { editStorageAvailable = false; }
function persistDraftEdits() {
  try {
    if (draftEdits.size) sessionStorage.setItem(editStorageKey,JSON.stringify([...draftEdits]));
    else sessionStorage.removeItem(editStorageKey);
    editStorageAvailable = true;
  } catch { editStorageAvailable = false; }
}
const busy = new Set();
const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("careerpilot-workspace") : null;

function node(tag, text = "", cls = "") {
  const element = document.createElement(tag); element.textContent = text;
  if (cls) element.className = cls; return element;
}
function clearError() {
  lastErrorCode = ""; $("error").textContent = ""; $("error").classList.add("hidden");
  document.querySelectorAll(".dialog-error").forEach(element => element.remove());
}
function showError(error) {
  lastErrorCode = error.code || ""; const message = error.message || String(error);
  $("error").textContent = message; $("error").classList.remove("hidden");
  const dialog = document.querySelector("dialog[open]");
  if (dialog) {
    let notice = dialog.querySelector(".dialog-error");
    if (!notice) { notice = node("p", "", "error dialog-error"); dialog.prepend(notice); }
    notice.textContent = message;
  }
}
async function api(path, method = "GET", body, extraHeaders = {}) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 30000);
  const headers = {...(method === "GET" ? {} : {"X-CSRF-Token": session.csrf || ""}),...extraHeaders};
  if (body !== undefined && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  try {
    const response = await fetch("/api" + path, {method, headers, signal:controller.signal,
      body:body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body)});
    let data; try { data = JSON.parse(await response.text()); } catch {
      const error = Error("The local service returned an unexpected response. Refresh status before trying again.");
      error.status = response.status; error.code = "service_unavailable"; throw error;
    }
    if (!response.ok) {
      const validation = Array.isArray(data.detail) ? data.detail.map(item => {
        const field = item.loc?.slice(1).join(" ");
        return (field ? field + ": " : "") + item.msg;
      }).join("; ") : "";
      const error = Error(data.message || validation || (data.detail ? "Invalid input; check required fields." : "The service is unavailable. Refresh status to recover."));
      error.status = response.status; error.code = data.error; throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError" || error instanceof TypeError) {
      const failure = Error("Connection interrupted or timed out. Your saved work is retained. Refresh status before trying again.");
      failure.code = "network_unavailable"; throw failure;
    }
    throw error;
  } finally { clearTimeout(timer); }
}
function button(text, fn, cls = "secondary") {
  const element = node("button", text, cls); element.type = "button";
  element.addEventListener("click", async () => {
    if (element.disabled) return; element.disabled = true; clearError();
    try { await fn(); } catch (error) { showError(error); } finally { element.disabled = false; }
  }); return element;
}
function wire(id, fn) {
  $(id).addEventListener("click", async () => {
    if (busy.has(id)) return; busy.add(id); $(id).disabled = true; clearError(); syncControls();
    try { await fn(); } catch (error) { showError(error); }
    finally { busy.delete(id); $(id).disabled = false; syncControls(); }
  });
}
function safeLink(url, text) {
  try {
    const parsed = new URL(url); if (parsed.protocol !== "https:") return null;
    const element = node("a", text, "link"); element.href = parsed.href;
    element.target = "_blank"; element.rel = "noopener noreferrer"; return element;
  } catch { return null; }
}
let questionContext = {question:"",source_ids:[]};
function clearAnswer({keepContext=false}={}) { $("answer").textContent = ""; $("answerSources").replaceChildren(); if (!keepContext) questionContext={question:"",source_ids:[]}; }
function renderAnswer(value, answeredQuestion = $("question").value) {
  clearAnswer({keepContext:true}); $("answer").textContent = value.answer + (value.coverage_note ? "\n\n" + value.coverage_note : "");
  if (answeredQuestion !== $("question").value) $("answer").textContent = "Answer to: " + answeredQuestion + "\n\n" + $("answer").textContent;
  questionContext = {question:answeredQuestion,source_ids:value.source_ids || []};
  const rows = $("answerSources");
  for (const id of new Set(value.source_ids || [])) {
    const job = currentScan?.result.jobs?.find(item => item.id === id), email = currentScan?.result.emails?.find(item => item.id === id);
    const source = job || email; if (!source) continue;
    const row = node("div","","button-row");
    row.append(node("p",job ? job.title + " · " + job.company : email.subject,"muted"),button("Inspect evidence",() => showSource(job ? "job" : "email",id))); rows.append(row);
  }
}
function gmailLink(email) {
  if (currentScan?.mode !== "live" || email.mode !== "live" || !session.account || currentScan.result.gmail_account !== session.account || currentScan.result.gmail_invalidated) return null;
  const link = node("a","Open in Gmail","link");
  link.href = "/api/scans/" + encodeURIComponent(currentScan.id) + "/emails/" + encodeURIComponent(email.id) + "/open";
  link.target = "_blank"; link.rel = "noopener noreferrer";
  link.title = email.reply_message_id ? "Search this connected Gmail account by the message's Message-ID." : "Message-ID unavailable; opens the connected account's inbox. Locate the conversation manually.";
  return link;
}
async function changeApplicationStatus(job, state) {
  const key = "status:" + job.id; if (busy.has(key)) return; busy.add(key);
  const prior = job.application_status || {state:"untracked"};
  const target = state === "undo_applied" ? prior.previous_state || "untracked" : state;
  renderScan(currentScan);
  try {
    let saved;
    try { saved = await api("/opportunities/status","POST",{job_id:job.id,state,expected_state:prior.state}); }
    catch (error) {
      const actual = await api("/opportunities/status/" + encodeURIComponent(job.id)).catch(() => null);
      if (actual) job.application_status = actual;
      if (actual) clearAnswer();
      if (!actual || actual.state !== target || error.code === "status_conflict") throw error;
      saved = actual;
    }
    job.application_status = saved; clearAnswer();
    try {
      renderScan(await api("/scans/" + currentScan.id)); await refreshSupportingViews();
    } catch {
      const other = (currentScan.result.actions || []).filter(item => item.source_kind !== "job");
      const pending = currentScan.result.jobs.filter(item => !item.blockers.length && item.recommendation !== "skip" && item.role_relevant !== false && !["applied_by_user","dismissed"].includes(item.application_status?.state)).slice(0,3);
      currentScan.result.actions = [...other,...pending.map(item => ({source_kind:"job",source_id:item.id,kind:"apply_manually",priority:item.score >= 75 ? 60 : 35,title:"Review opportunity: " + item.title,why:"Heuristic fit " + item.score + "/100; verify unknown eligibility and experience.",evidence:item.matches.join(", "),uncertainty:item.unknowns.join("; ")}))].sort((a,b) => b.priority - a.priority);
      $("workspaceHint").textContent = workspaceHintBase + " Application tracking saved. Refresh workspace status to reload related results."; $("retryStatus").classList.remove("hidden");
    }
  } finally {
    busy.delete(key); renderScan(currentScan);
  }
}
function showDialog(id) { clearError(); if (!$(id).open) $(id).showModal(); }
function tabView(name) {
  document.querySelectorAll("[data-tab]").forEach(element => {
    const active = element.dataset.tab === name;
    element.classList.toggle("active", active); element.setAttribute("aria-current", active ? "page" : "false");
  });
  for (const view of ["jobs", "emails", "activity", "actions"]) $(view + "Panel").classList.toggle("hidden", view !== name);
}
function formProfile(value) {
  profile = value;
  $("cvFileStatus").textContent = "No CV file selected in this session.";
  for (const [id,key] of Object.entries({profileName:"name",seniority:"seniority",employment:"employment_type",currency:"salary_currency",priorities:"priorities"})) $(id).value = value[key] || "";
  for (const [id,key] of Object.entries({roles:"target_roles",locations:"locations",industries:"industries"})) $(id).value = (value[key] || []).join(", ");
  for (const [id,key] of Object.entries({projects:"projects",experience:"experience",education:"education",languages:"languages",achievements:"achievements",summary:"summary",activities:"activities"})) $(id).value = (value[key] || []).join("\n");
  $("sourceText").value = value.source_text || "";
  $("unmappedText").value = (value.unmapped_text || []).join("\n");
  $("extractionNotes").textContent = (value.extraction_notes || []).join(" ");
  $("skills").value = (value.skills || []).map(skill => skill.name).join(", ");
  $("skillEvidence").textContent = (value.skills || []).map(skill => skill.name + " [" + skill.level + "]\n" + skill.evidence).join("\n\n");
  $("remote").checked = !!value.remote; $("learn").checked = !!value.willing_to_learn; $("salary").value = value.salary_minimum ?? "";
}
const split = (text, separator = ",") => text.split(separator).map(value => value.trim()).filter(Boolean);
function splitSkills(text) {
  const items = []; let start = 0, depth = 0;
  for (let i = 0; i < text.length; i++) {
    if ("([".includes(text[i])) depth++;
    else if (")]".includes(text[i])) depth = Math.max(0,depth - 1);
    else if (text[i] === "," && !depth) { items.push(text.slice(start,i).trim()); start = i + 1; }
  }
  items.push(text.slice(start).trim()); return items.filter(Boolean);
}
function collectProfile() {
  const original = new Map((profile?.skills || []).map(skill => [skill.name.toLowerCase(),skill]));
  const facts = Object.fromEntries(["projects","experience","education","languages","achievements","summary","activities"].map(key => {
    const saved = profile?.[key] || [];
    return [key,$(key).value === saved.join("\n") ? saved : split($(key).value,"\n")];
  }));
  return {name:$("profileName").value,
    skills:$("skills").value === (profile?.skills || []).map(skill => skill.name).join(", ") ? profile.skills : splitSkills($("skills").value).map(name => original.get(name.toLowerCase()) || {name,level:"user_confirmed",evidence:"Explicitly entered and confirmed by user"}),
    ...facts,source_text:profile?.source_text || "",unmapped_text:profile?.unmapped_text || [],extraction_method:profile?.extraction_method || "",extraction_notes:profile?.extraction_notes || [],
    target_roles:split($("roles").value),locations:split($("locations").value),remote:$("remote").checked,seniority:$("seniority").value,
    employment_type:$("employment").value || "Any",salary_minimum:$("salary").value === "" ? null : Number($("salary").value),
    salary_currency:$("currency").value,industries:split($("industries").value),willing_to_learn:$("learn").checked,priorities:$("priorities").value};
}
function profileDirty() { return !!profileFormBaseline && JSON.stringify(collectProfile()) !== profileFormBaseline; }
function loadProfileForm(value) {
  formProfile(value.profile || {skills:[],remote:true,willing_to_learn:true,seniority:"entry"});
  profileFormVersion = value.id || ""; profileFormBaseline = JSON.stringify(collectProfile()); profileConflict = false;
  $("profileConflict").classList.add("hidden");
}
async function refreshProfile({replaceForm = false} = {}) {
  const sequence = ++profileReadSequence, value = await api("/profile");
  if (sequence !== profileReadSequence) return;
  const changed = profileVersion !== (value.id || "");
  hasProfile = !!value.profile; profileVersion = value.id || "";
  $("profileSummary").textContent = value.profile ? value.profile.name + " · " + value.profile.target_roles.join(", ") : "Confirm your profile to begin.";
  if (replaceForm || !profileDirty()) loadProfileForm(value);
  else { profileConflict = profileFormVersion !== profileVersion; $("profileConflict").classList.toggle("hidden",!profileConflict); }
  if (changed && currentScan) { currentScan.profile_current = false; renderScan(currentScan); }
  syncControls();
}
function syncControls() {
  $("profileForm").querySelector('[type="submit"]').disabled = busy.has("profile") || busy.has("upload") || busy.has("extractText") || profileConflict;
  $("uploadPdf").disabled = busy.has("upload") || busy.has("profile") || busy.has("extractText");
  $("uploadPdf").textContent = busy.has("upload") ? "Extracting CV…" : "Extract PDF";
  const gmailBusy = session.gmail_busy || busy.has("oauth") || ["connectGmail","composeGmail","disconnectGmail"].some(id => busy.has(id));
  $("connectGmail").disabled = gmailBusy || session.mode !== "live" || !session.gmail_configured;
  $("composeGmail").disabled = gmailBusy || session.mode !== "live" || !session.gmail_configured;
  $("disconnectGmail").disabled = gmailBusy || !session.account;
  const modelReady = session.mode === "live" && !!session.model_configured;
  $("emailConsent").disabled = gmailBusy || !session.account || !modelReady;
  $("processingConsent").disabled = !modelReady;
  $("profileProcessingConsent").disabled = !modelReady;
  $("processingConsentHint").textContent = session.mode === "fixture" ? "Synthetic demo: no external model is called, so this approval is not needed." :
    !session.mode ? "Checking model availability. Refresh workspace status if this does not finish." :
    !modelReady ? "No model is configured, so AI-processing approval is unavailable. Local CV extraction, scans and drafts still work. Choose Model setup to enable optional AI processing." :
    "Select this approval before a scan that uses your configured external model. It does not grant Gmail access.";
  $("emailConsentHint").textContent = session.mode === "fixture" ? "Synthetic demo: real Gmail/model processing is unavailable." :
    !session.mode ? "Checking Gmail and model availability." :
    !modelReady ? "Gmail model-processing approval needs a configured model and a connected Gmail account. Choose Model setup; Gmail itself remains optional." :
    !session.account ? "Connect Gmail first to enable this approval. Profile/source processing can be approved separately below." :
    gmailBusy ? "Wait for the Gmail connection operation to finish." :
    "You can select this approval to allow relevant Gmail excerpts to your model. Google read/draft permissions are separate.";
  $("processingConsent").setAttribute("title",$("processingConsentHint").textContent);
  $("emailConsent").setAttribute("title",$("emailConsentHint").textContent);
  $("modelSetup").classList.toggle("hidden",session.mode === "fixture");
  $("saveModel").disabled = busy.has("model") || session.mode !== "live";
  $("disableModel").disabled = busy.has("model") || !session.model_configured;
  const running = currentScan && ["queued","running"].includes(currentScan.state);
  $("scanButton").disabled = !hasProfile || !!running || offline || busy.has("scanButton");
  $("cancelScan").classList.toggle("hidden",!running); $("cancelScan").disabled = offline || busy.has("cancelScan");
  $("askButton").disabled = !scanId || !!running || !currentScan?.profile_current || offline || busy.has("question");
  $("questionHint").textContent = !scanId ? "Run a career scan first. Gmail is optional for job and skill questions." :
    !currentScan?.profile_current ? "Your profile changed. Run a fresh scan before asking questions." :
    running ? "Questions will be available when the scan finishes." : "Answers use this scan's evidence and limited source coverage.";
  if (draft && $("draftDialog").open) updateDraftControls();
}
function clearGmailViews({initial = false} = {}) {
  for (const [id,edit] of draftEdits) if ((edit.account || edit.gmail) && (!initial || edit.account !== session.account)) draftEdits.delete(id);
  persistDraftEdits();
  clearAnswer();
  if (detailKind === "email") { if ($("detailDialog").open) $("detailDialog").close(); $("detailBody").replaceChildren(); $("detailTitle").textContent = "Evidence"; }
  if (draft?.payload.mode === "live" && (draft.payload.account || draft.payload.source_kind === "email")) {
    if ($("draftDialog").open) $("draftDialog").close(); draft = null; clearTimeout(draftTimer);
    for (const id of ["draftTo","draftSubject","draftBody"]) $(id).value = "";
    for (const id of ["draftMode","draftAccount","draftState","draftHint"]) $(id).textContent = "";
  }
  history = []; $("draftHistory").replaceChildren(); $("draftRecovery").replaceChildren(); $("draftRecovery").classList.add("hidden");
  if (currentScan?.mode === "live") {
    const value = structuredClone(currentScan); value.result.emails = []; value.result.gmail_account = null; value.result.gmail_invalidated = true;
    value.result.actions = (value.result.actions || []).filter(action => action.source_kind !== "email");
    const sources = value.result.error_sources || [], safe = sources.filter(item => ["jobs","model","scan"].includes(item.source));
    const sourced = new Set(sources.map(item => item.message));
    const legacy = (value.result.errors || []).filter(message => !sourced.has(message) &&
      (/^Greenhouse board [a-zA-Z0-9_-]{1,80}: unavailable or invalid response\.$/.test(message) ||
       ["Model analysis failed for a job; labeled keyword fallback used.","Scan interrupted or exceeded its time budget; partial evidence retained.","Scan failed; available partial results retained."].includes(message)));
    value.result.errors = [...new Set([...safe.map(item => item.message),...legacy])]; value.result.error_sources = safe;
    value.result.coverage = {...value.result.coverage,gmail:{state:session.account ? "fresh_scan_required" : "disconnected"}}; renderScan(value);
  } else if (!currentScan) renderGmailEmpty();
}
function connectionNotice(message, failed = false, retry = false, manage = false) {
  $("connectionNotice").classList.remove("hidden"); $("connectionNotice").classList.toggle("error",failed);
  $("connectionNoticeText").textContent = message; $("retryGmail").classList.toggle("hidden",!retry); $("manageGoogleAccess").classList.toggle("hidden",!manage);
}
function showDisconnectNotice(value) {
  if (!value) return; const failed = value.had_connection && value.revocation_confirmed !== true;
  connectionNoticeKind = "disconnect";
  connectionNotice((failed ? "Disconnected locally. Google access revocation could not be confirmed. In Google, choose the Gmail account you disconnected and remove CareerPilot access." :
    value.had_connection ? "Gmail disconnected. Google access revocation confirmed." : "Gmail is disconnected.") +
    " Cached scan mail was cleared. Draft records remain for recovery and are hidden from other Gmail accounts.",failed,false,failed);
}
async function refreshSession() {
  const initial = !session.csrf, previous = session.account, previousVersion = session.gmail_version;
  const previousModelVersion = session.model_version, previousProvider = session.model_provider, previousModel = session.model_name;
  session = await api("/session");
  if (!initial && (previousModelVersion !== session.model_version || previousProvider !== session.model_provider || previousModel !== session.model_name)) {
    $("processingConsent").checked = false; $("profileProcessingConsent").checked = false;
  }
  const changed = previous !== session.account || (previousVersion !== undefined && previousVersion !== session.gmail_version);
  if (changed) clearGmailViews({initial});
  if (session.account && connectionNoticeKind === "disconnect") { $("connectionNotice").classList.add("hidden"); connectionNoticeKind = ""; }
  $("modeBadge").textContent = session.mode === "fixture" ? "SYNTHETIC DEMO" : "LIVE · LIMITED COVERAGE";
  $("notice").textContent = session.mode === "fixture" ? "Synthetic jobs and email. Draft execution is a simulation; no Gmail access." :
    "Job sources are limited to configured employer boards and optional public searches. Without a model, extraction, matching and drafts use a labeled local baseline.";
  $("connection").textContent = session.account ? "Gmail: " + session.account +
    (session.gmail_state === "reconnect_required" ? " · reconnect required" : session.compose ? " · drafts enabled" : " · read only") : "Gmail disconnected";
  $("connectGmail").textContent = session.account ? "Reconnect / change Gmail" : "Connect Gmail";
  $("composeGmail").textContent = session.compose ? "Review Gmail draft access" : "Enable Gmail drafts";
  $("modelStatus").textContent = session.model_configured ? "AI model: " + (session.model_name || "configured model") + (session.model_provider ? " · " + session.model_provider : "") : "No model configured · local keyword/template baseline";
  $("sourceStatus").textContent = "Job sources: Greenhouse boards " + session.job_coverage + " · JSearch " + (session.jsearch_state || "disabled").replaceAll("_"," ");
  $("emailConsent").checked = session.email_processing_consent;
  $("extractionHint").textContent = session.mode === "live" && session.model_configured ?
    "Extraction uses your configured external model only with your approval below. Review all extracted facts." :
    "Extraction runs locally using listed skills and recognized CV sections. Review the name, skills and copied facts, fill any gaps, and choose your career goals. No external model is used.";
  $("profileProcessingLabel").classList.toggle("hidden",session.mode !== "live" || !session.model_configured);
  $("gmailConfigHint").textContent = session.mode === "fixture" ? "Real Gmail is unavailable in the synthetic demo." :
    !session.gmail_configured ? "Gmail connection requires local Google OAuth configuration. You can continue with jobs and local drafts." :
    "Gmail is optional. Read-only access finds relevant conversations; draft creation requires the additional permission.";
  $("dismissConnectionNotice").textContent = session.account ? "Continue in workspace" : "Continue without Gmail";
  showDisconnectNotice(session.gmail_disconnect); syncControls(); return changed;
}
function renderGmailEmpty(state = session.account ? "not_read" : "disconnected") {
  const box = $("emails"); box.replaceChildren();
  const running = !!currentScan && ["queued","running"].includes(currentScan.state);
  const message = state === "disconnected" ? "Gmail is disconnected. Connect it to review conversations; job discovery and local drafts still work." :
    running ? "The scan is running. Gmail results will appear when mailbox checking finishes." :
    ["not_read","fresh_scan_required","account_changed"].includes(state) ? "Run a fresh scan of the connected Gmail account to load conversations." :
    state === "external_processing_consent_required" ? "Approve Gmail model-processing consent above, or use a local baseline configuration, then run a fresh scan." :
    "Gmail could not be checked. Outstanding replies are unknown. Reconnect Gmail or retry a fresh scan.";
  const card = node("div","","empty"); card.append(node("p",message));
  if (session.mode === "live" && session.gmail_configured && (!session.account || ["unavailable","account_changed"].includes(state))) card.append(button(session.account ? "Reconnect Gmail" : "Connect Gmail",() => startGmail(false)));
  if (session.account && hasProfile) {
    const start = button("Run a fresh scan",startScan); start.disabled = !!currentScan && ["queued","running"].includes(currentScan.state); card.append(start);
  }
  box.append(card);
}
function renderScan(value) {
  currentScan = value; const data = value.result, jobs = data.jobs || [], market = data.market || {};
  const running = ["queued","running"].includes(value.state);
  let coverage = data.coverage?.gmail || {state:"not_read"};
  const permitted = value.mode === "fixture" || !!(session.account && data.gmail_account === session.account && !data.gmail_invalidated);
  const emails = permitted ? data.emails || [] : [];
  if (!permitted && coverage.state === "read") coverage = {state:session.account ? "fresh_scan_required" : "disconnected"};
  const checked = ["read","synthetic"].includes(coverage.state) && permitted;
  $("progress").classList.remove("hidden"); $("progress").textContent = (value.state === "partial" ? "Scan finished with partial results" : value.progress) +
    (value.profile_current ? "" : " — profile changed; run a fresh scan.");
  $("jobCount").textContent = jobs.length; $("emailCount").textContent = checked ? emails.filter(email => email.requires_response).length : "—";
  $("emailCountLabel").textContent = checked ? "Confirmed reply requests" : "Gmail not checked · replies unknown";
  $("gapCount").textContent = (market.learning || []).length; $("sampleCount").textContent = market.sample_size ?? "—";
  const next = $("nextActions"); next.replaceChildren();
  for (const [index,item] of (data.actions || []).filter(action => action.source_kind !== "email" || checked).entries()) {
    const card = node("div","","action-card"); card.append(node("p",index === 0 ? "YOUR HIGHEST PRIORITY" : "NEXT IN YOUR PLAN","eyebrow"),node("h3",item.title),node("p",item.why));
    if (item.uncertainty) card.append(node("p",item.uncertainty,"muted"));
    if (["email","job"].includes(item.source_kind)) card.append(button("Inspect evidence",() => showSource(item.source_kind,item.source_id))); next.append(card);
  }
  if (!next.children.length) next.append(node("div",running ? "Collecting evidence for your next actions…" :
    jobs.length && jobs.every(job => ["applied_by_user","dismissed"].includes(job.application_status?.state)) ?
      "All current opportunities are marked applied or dismissed. Review their status, or run a fresh scan for new opportunities." :
      "No supported next action yet. Review coverage below or run a fresh scan.","empty"));
  const marketBox = $("market"); marketBox.replaceChildren();
  for (const item of market.learning || []) {
    const row = node("div","","market-row"); row.append(node("strong",item.skill),node("span",item.percent === null ? item.count + " jobs" : item.percent + "% · " + item.count + " jobs")); marketBox.append(row);
  }
  if (!(market.learning || []).length) marketBox.append(node("p",!market.sample_size ? "No usable matching jobs for learning analysis. Review source coverage and target roles, or run a fresh scan." : "No supported missing-skill priorities in this sample.","muted"));
  marketBox.append(node("p",market.caveat || "No usable sample yet.","muted"));
  const jobBox = $("jobs"); jobBox.replaceChildren();
  for (const job of jobs) {
    const card = node("article","","job-card");
    card.append(node("p",job.mode.toUpperCase() + " · " + job.company,"eyebrow"),node("h3",job.title),node("p",job.location,"muted"),node("div",job.coverage ? job.score + "/100 heuristic fit" : "Insufficient evidence to score fit","score"),node("p","Scoring inputs available " + job.coverage + "% · " + job.recommendation,"muted"));
    const tags = node("div","","tags"); for (const skill of job.matches.slice(0,5)) tags.append(node("span",skill,"tag"));
    for (const skill of job.gaps.slice(0,3)) tags.append(node("span","Gap: " + skill,"tag gap")); card.append(tags);
    card.append(node("p","Source: " + (job.source || "Unknown") + (job.publisher ? " · " + job.publisher : "") + " · Published: " + (job.published_at || "unknown") + (job.updated_at ? " · Source updated: " + job.updated_at : "") + (job.source_stale ? " · older cached sample" : job.source_cached ? " · cached" : ""),"muted"));
    if (job.blockers.length) card.append(node("p",job.blockers[0],"muted"));
    if (job.application_status?.state !== "untracked" && job.application_status) card.append(node("p","Application status: " + job.application_status.state.replaceAll("_"," ") + " · " + new Date(job.application_status.updated).toLocaleDateString(),"application-status"));
    const controls = node("div","","button-row"), outreach = button("Prepare outreach",() => openContact(job.id));
    outreach.disabled = !value.profile_current || !["succeeded","partial","interrupted"].includes(value.state);
    const applied = job.application_status?.state === "applied_by_user";
    const mark = button(applied ? "Undo mark applied" : "Mark applied",() => changeApplicationStatus(job,applied ? "undo_applied" : "applied_by_user"));
    mark.disabled = busy.has("status:" + job.id);
    if (applied) {
      mark.title = "Changes CareerPilot tracking only; does not withdraw an external application.";
      card.append(node("p","CareerPilot tracking only. Undo does not withdraw an application.","muted"));
    }
    controls.append(button("Why this fit?",() => showSource("job",job.id)),outreach,mark);
    const link = safeLink(job.url,"Open original role"); if (link) controls.append(link); card.append(controls); jobBox.append(card);
  }
  if (!jobs.length) jobBox.append(node("div",running ? "Scanning the configured job sources. Results will appear as they are analyzed." : "No relevant jobs in this retrieved sample. This is not evidence that no suitable jobs exist. Try a fresh scan or review your profile.","empty"));
  if (!checked) renderGmailEmpty(coverage.state);
  else {
    const box = $("emails"); box.replaceChildren();
    box.append(node("p",value.mode === "fixture" ? "Synthetic mail; Gmail was not accessed." :
      "Snapshot for " + data.gmail_account + " · " + new Date(data.analyzed_at || value.created).toLocaleString() + " · " + coverage.threads + (coverage.threads === 1 ? " thread" : " threads") + " in the last 30 days. Coverage is limited.","muted"));
    if (!emails.length) box.append(node("div","No matching conversations in this limited scan. Run a fresh scan later to check again.","empty"));
    for (const email of emails) {
      const card = node("div","","card"); card.append(node("p",email.mode.toUpperCase() + " · " + email.category + " · " + email.response_state.replaceAll("_"," "),"eyebrow"),node("h3",email.subject),node("p",email.summary),node("p",email.uncertainty || "Review the evidence before acting.","muted"),node("p",email.deadline ? "Deadline: " + email.deadline : "No confirmed deadline","muted"));
      card.append(button("Inspect evidence",() => showSource("email",email.id)));
      const gmail = gmailLink(email); if (gmail) card.append(gmail);
      if (email.requires_response && value.profile_current) card.append(button("Prepare reply",() => prepareReply(email.id))); box.append(card);
    }
  }
  $("scanErrors").textContent = (data.errors || []).join(" "); $("scanErrors").classList.toggle("hidden",!data.errors?.length); syncControls();
}
function resetScan() {
  scanId = ""; currentScan = null; localStorage.removeItem("careerpilot-scan"); clearTimeout(pollTimer);
  $("progress").classList.add("hidden"); $("scanErrors").classList.add("hidden"); clearAnswer();
  for (const id of ["jobCount","emailCount","gapCount","sampleCount"]) $(id).textContent = "—";
  $("emailCountLabel").textContent = "Gmail not checked · replies unknown";
  $("jobs").replaceChildren(node("div","Confirm your profile, then run a career scan. Gmail is optional.","empty"));
  $("nextActions").replaceChildren(node("div","Your next move starts with a confirmed profile and a career scan.","empty"));
  $("market").replaceChildren(node("p","Learning priorities come from the jobs actually analyzed.","muted")); renderGmailEmpty(); syncControls();
}
async function poll() {
  if (!scanId) return;
  if (polling) { pollAgain = true; return; }
  polling = true; clearTimeout(pollTimer); const identifier = scanId;
  try {
    await refreshSession(); const value = await api("/scans/" + identifier); if (identifier !== scanId) return;
    offline = false; retryCount = 0; if (["network_unavailable","service_unavailable"].includes(lastErrorCode)) clearError(); renderScan(value);
    $("retryStatus").classList.add("hidden");
    if (["queued","running"].includes(value.state)) pollTimer = setTimeout(poll,700);
    else await refreshSupportingViews();
  } catch (error) {
    if (error.status === 404) { resetScan(); clearError(); $("workspaceHint").textContent = hasProfile ? "The saved scan is no longer available. Run a new scan; your profile is retained." : "The saved scan is no longer available. Review your profile, then run a new scan."; }
    else {
      offline = true; showError(error); $("progress").textContent = "Connection interrupted. Scan status is unknown; reconnecting safely…";
      $("retryStatus").classList.remove("hidden"); pollTimer = setTimeout(poll,Math.min(10000,800 * 2 ** Math.min(retryCount++,4)));
    }
  } finally {
    polling = false; syncControls();
    if (pollAgain && scanId) { pollAgain = false; clearTimeout(pollTimer); pollTimer = setTimeout(poll,0); }
  }
}
async function startScan() {
  clearError(); offline = false; clearAnswer();
  try {
    const value = await api("/scans","POST",{processing_consent:$("processingConsent").checked}); scanId = value.id; localStorage.setItem("careerpilot-scan",scanId);
    currentScan = {id:scanId,state:"queued",profile_current:true,result:{},progress:"Scan queued",mode:session.mode};
    if (connectionNoticeKind === "connected") $("connectionNotice").classList.add("hidden");
    renderScan(currentScan); await poll();
  } catch (error) {
    // Discover work after a lost response; never repeat the POST automatically.
    try {
      const latest = (await api("/scans/latest")).scan;
      if (latest && ["queued","running"].includes(latest.state)) { scanId = latest.id; localStorage.setItem("careerpilot-scan",scanId); renderScan(latest); await poll(); return; }
    } catch { $("retryStatus").classList.remove("hidden"); } throw error;
  }
}
function showSource(kind,id) {
  const source = (currentScan?.result[kind === "job" ? "jobs" : "emails"] || []).find(item => item.id === id);
  if (!source) throw Error("This evidence is no longer available. Run a fresh scan.");
  if (kind === "email" && currentScan.mode === "live" && currentScan.result.gmail_account !== session.account) throw Error("Gmail changed. Run a fresh scan to inspect mail.");
  detailKind = kind; $("detailTitle").textContent = source.title || source.subject || "Evidence";
  const body = $("detailBody"); body.replaceChildren();
  const section = (title,content) => {
    const box = node("section","","detail-section"); box.append(node("h3",title));
    if (Array.isArray(content)) {
      const list = node("ul"); for (const text of content) list.append(node("li",text));
      box.append(content.length ? list : node("p","None confirmed in this scan.","muted"));
    } else box.append(node("p",content || "Not confirmed in this scan.","detail-text"));
    body.append(box);
  };
  if (kind === "job") {
    body.append(node("p",[source.company,source.location].filter(Boolean).join(" · "),"muted"));
    section("Fit and scoring inputs",(source.coverage ? "Heuristic fit: " + source.score + "/100. " : "Insufficient evidence to score fit. ") + "Scoring inputs available: " + source.coverage + "%. This describes available weighted components, not verified requirements or hiring probability. " + (source.why || "Review the requirements and eligibility before acting."));
    section("Original description",source.description);
    section("CV role evidence",source.role_evidence || []);
    section("Publication and retrieval",["Published: " + (source.published_at || source.updated_at || "unknown"),"Retrieved: " + (source.retrieved_at || "unknown")]);
    section("Other mandatory criteria",(source.requirements?.other_required || []).map(item => item.skill + ": " + item.evidence));
    section("Proficiency questions",source.proficiency_questions || []);
    section("Matched skills",source.matches || []); section("Missing required skills",source.gaps || []);
    section("Required skill evidence",(source.requirements?.required || []).map(item => item.skill + ": " + item.evidence));
    section("Preferred skill evidence",(source.requirements?.preferred || []).map(item => item.skill + ": " + item.evidence));
    section("Eligibility and limitations",[...(source.blockers || []),...(source.unknowns || [])]);
    if (source.projects?.length) section("Relevant confirmed CV projects",source.projects);
    if (source.project_evidence?.length) section("Why these projects are relevant",source.project_evidence.map(item => "Shared terms: " + item.shared_terms.join(", ") + ". Job evidence: " + item.job_evidence + ". Relevance does not confirm proficiency."));
    const link = safeLink(source.url,"Open original role"); if (link) body.append(link);
  } else {
    const gmail = gmailLink(source); if (gmail) body.append(gmail);
    section("Conversation",["From: " + (source.sender || "Not confirmed"),"Reply recipient: " + (source.reply_to || source.sender || "Verify before replying"),"Category: " + (source.category || "Needs review")]);
    section("Summary",source.summary);
    section("Reply status",source.requires_response ? "A direct reply request was identified. Verify the context and recipient before preparing a reply." : "No confirmed reply is required in this snapshot. " + (source.response_state || "needs review").replaceAll("_"," "));
    section("Source evidence",source.evidence);
    section("Deadline",source.deadline ? source.deadline + (source.deadline_evidence ? " — " + source.deadline_evidence : "") : "No confirmed deadline.");
    section("Uncertainty",source.uncertainty || "Review the source context before acting.");
  }
  const technical = node("details"); technical.append(node("summary","Technical details"),node("pre",JSON.stringify(source,null,2),"detail-text")); body.append(technical);
  showDialog("detailDialog");
}
function openContact(id) {
  pendingContact = id; $("contactEmail").value = session.mode === "fixture" ? "recruiter@example.invalid" : "";
  $("contactSource").value = session.mode === "fixture" ? "Synthetic demo contact" : ""; $("contactConfirmed").checked = false;
  $("senderIdentityConfirmed").checked = false; $("senderIdentityConfirmed").disabled = !session.account || session.mode !== "live"; showDialog("contactDialog");
}
async function prepareReply(id) {
  const pending = history.find(action => action.account_available && action.payload.source_id === id && ["unknown","executing"].includes(action.state));
  if (pending) return openDraft(pending.id);
  draft = await api("/drafts","POST",{scan_id:scanId,source_kind:"email",source_id:id,processing_consent:$("processingConsent").checked}); renderDraft(); showDialog("draftDialog"); await refreshSupportingViews();
}
function draftDirty() { return draft && ($("draftTo").value !== draft.payload.to || $("draftSubject").value !== draft.payload.subject || $("draftBody").value !== draft.payload.body); }
function rememberDraftEdits() {
  if (!draft) return;
  const gmail = draft.payload.mode === "live" && !!(draft.payload.account || draft.payload.source_kind === "email");
  if (draftDirty()) draftEdits.set(draft.id,{gmail,account:gmail ? draft.payload.account || session.account || "" : "",to:$("draftTo").value,subject:$("draftSubject").value,body:$("draftBody").value});
  else draftEdits.delete(draft.id);
  persistDraftEdits();
}
function updateDraftControls() {
  if (!draft) return; const synthetic = draft.payload.mode === "synthetic", dirty = draftDirty();
  const expired = draft.state === "approved" && (!draft.expires || new Date(draft.expires).getTime() <= Date.now());
  const available = draft.account_available !== false, accountMatches = synthetic || !!(session.account && session.account === draft.payload.account);
  const working = ["saveDraft","approveDraft","rejectDraft","executeDraft","reconcileDraft","refreshDraft"].some(id => busy.has(id));
  for (const id of ["draftTo","draftSubject","draftBody"]) $(id).readOnly = working || draftStatusUnknown || !available || !["proposed","approved","failed"].includes(draft.state);
  $("executeDraft").textContent = synthetic ? "Run draft simulation" : "Create Gmail draft";
  $("executeDraft").disabled = working || draftStatusUnknown || dirty || !available || draft.state !== "approved" || expired || (!synthetic && (!accountMatches || !session.compose || session.gmail_state === "reconnect_required"));
  $("approveDraft").textContent = expired || draft.state === "failed" ? "Review and approve again" : "Approve exact draft";
  $("approveDraft").disabled = working || draftStatusUnknown || dirty || !available || !(draft.state === "proposed" || draft.state === "failed" || expired);
  $("saveDraft").disabled = working || draftStatusUnknown || !available || !["proposed","approved","failed"].includes(draft.state);
  $("rejectDraft").disabled = working || draftStatusUnknown || !available || !["proposed","approved","failed"].includes(draft.state);
  $("reconcileDraft").classList.toggle("hidden",draft.state !== "unknown" || synthetic); $("reconcileDraft").disabled = working || !accountMatches;
  $("refreshDraft").disabled = working;
  const retained = draftEdits.has(draft.id), editable = ["proposed","approved","failed"].includes(draft.state);
  $("discardDraftEdits").classList.toggle("hidden",!retained);
  $("discardDraftEdits").disabled = working;
  $("viewDraftEdits").classList.toggle("hidden",!retained || editable);
  $("viewDraftEdits").disabled = working;
  $("reloadDraftPage").classList.toggle("hidden",!retained);
  $("reloadDraftPage").disabled = working || !editStorageAvailable;
  $("draftStorageNote").textContent = !editStorageAvailable ?
    "Reload recovery is unavailable in this browser. Keep this tab open or choose Save edits to retain your work." : retained && !editable ?
    "The fields show the saved operation. Your unsaved text is retained separately in this tab; choose View retained edits to copy it." :
    "Unsaved draft edits are kept in this browser tab across refresh. Save edits to update the saved draft, or Discard unsaved edits to remove them. Disconnect and account changes clear Gmail edits.";
  let hint = "Review the exact recipient, subject and body before approval.";
  if (draftStatusUnknown) hint = "The saved status could not be checked. Refresh draft status when connected; do not blindly retry.";
  else if (dirty) hint = "Unsaved edits: save them, then review and approve again.";
  else if (draft.state === "unknown") hint = "Gmail may have created this draft. Reconcile it; do not create a duplicate. Reopen it from Draft history after closing or refreshing.";
  else if (draft.state === "executing") hint = "Execution is pending. Refresh status; do not repeat the operation.";
  else if (expired) hint = "Approval expired. Read the current content and choose Review and approve again.";
  else if (draft.state === "failed") hint = session.gmail_state === "reconnect_required" ? "Gmail access needs reconnection. Reconnect Gmail below, then run a fresh scan and prepare a new draft." : "The operation did not complete. Resolve the reported cause, then review and approve again.";
  else if (draft.state === "succeeded") hint = "Draft operation completed. Close this dialog to continue in the workspace; email sending remains disabled.";
  else if (draft.state === "rejected") hint = "Draft rejected. Close this dialog or prepare a new draft from the workspace.";
  else if (draft.state === "stale") hint = "This draft is no longer current. Close it, run a fresh scan if needed, and prepare a new draft.";
  else if (!synthetic && !draft.payload.account) hint = "This draft is saved locally. To bind new outreach to Gmail, explicitly confirm that account represents the CV candidate in Recruitment contact.";
  else if (!synthetic && !accountMatches) hint = "Connect the original Gmail account before reviewing this draft.";
  else if (!synthetic && !session.compose) hint = "Gmail is read-only. Enable Gmail drafts, then run a fresh scan and prepare a new draft; reconnecting invalidates old approvals.";
  else if (!synthetic && session.gmail_state === "reconnect_required") hint = "Gmail needs reconnection. Reconnect, then prepare a fresh draft.";
  $("draftHint").textContent = draftRefreshWarning ? hint + " " + draftRefreshWarning : hint;
  $("draftCapability").textContent = session.gmail_state === "reconnect_required" ? "Reconnect Gmail for drafts" : "Connect / enable Gmail drafts";
  $("draftCapability").classList.toggle("hidden",synthetic || (!!session.compose && accountMatches && session.gmail_state !== "reconnect_required"));
  $("draftCapability").disabled = session.mode !== "live" || !session.gmail_configured || working;
}
function renderDraft({preserveEdits = false} = {}) {
  draftRefreshWarning = "";
  const edited = preserveEdits && ["proposed","approved","failed"].includes(draft.state) && draftEdits.get(draft.id);
  if (!preserveEdits) draftEdits.delete(draft.id);
  persistDraftEdits();
  const fields = edited || draft.payload;
  draftStatusUnknown = false; $("draftTo").value = fields.to; $("draftSubject").value = fields.subject; $("draftBody").value = fields.body;
  $("draftMode").textContent = draft.payload.mode === "synthetic" ? "SYNTHETIC SIMULATION · no Gmail operation." :
    !draft.payload.account ? "Local-only draft, saved on this computer. Gmail sender identity was not confirmed for this CV candidate." :
    draft.state === "succeeded" ? "Gmail draft saved. Email was not sent; this app does not send email." :
    ["unknown","executing"].includes(draft.state) ? "A Gmail draft operation needs a status check. Keep this record and use status refresh or reconciliation; do not create a duplicate." :
    session.gmail_state === "reconnect_required" ? "Local draft review. Gmail needs reconnection before any draft can be created there." :
    !session.compose ? "Local draft review. Gmail is read-only; enable draft access and prepare a fresh draft before saving it to Gmail." :
    "After exact approval, Create Gmail draft saves this content to Gmail. This app never sends email. Google compose permission also permits sending at the provider.";
  $("draftAccount").textContent = "Account: " + (draft.payload.account || "Local draft · no Gmail account bound");
  if (draft.payload.preparation_method === "configured_model_evidence_selection") $("draftMode").textContent += " Model selected source evidence; factual wording was assembled locally.";
  $("draftState").textContent = "State: " + draft.state + (draft.result.message ? " · " + draft.result.message : draft.result.draft_id ? " · verified draft " + draft.result.draft_id : "");
  updateDraftControls(); clearTimeout(draftTimer); if (draft.state === "executing") draftTimer = setTimeout(() => refreshDraft().catch(showError),1500);
}
async function openDraft(id) { await refreshSession(); draft = await api("/actions/" + id); clearError(); renderDraft({preserveEdits:true}); showDialog("draftDialog"); }
async function refreshDraft() {
  if (!draft) return; await refreshSession(); if (!draft) return;
  draft = await api("/actions/" + draft.id); clearError(); renderDraft({preserveEdits:true});
  try { await refreshHistory(); }
  catch { draftRefreshWarning = "Draft history could not refresh; the saved status above is current. Refresh workspace status to retry."; updateDraftControls(); }
}
async function draftOperation(path,method,body) {
  const identifier = draft.id, previousVersion = draft.version, previousHash = draft.payload_hash;
  try { draft = await api("/actions/" + identifier + path,method,body); }
  catch (error) {
    let recovered = false;
    try {
      await refreshSession();
      if (draft) {
        draft = await api("/actions/" + identifier); renderDraft({preserveEdits:true});
        recovered = (["/execute","/reconcile"].includes(path) && draft.state === "succeeded") ||
          (method === "PUT" && draft.version > previousVersion && ["to","subject","body"].every(key => draft.payload[key] === body[key])) ||
          (path === "/review" && draft.version === previousVersion && draft.payload_hash === previousHash && draft.state === (body.decision === "approve" ? "approved" : "rejected"));
      }
    } catch { draftStatusUnknown = true; updateDraftControls(); }
    if (!recovered) throw error;
  }
  if (!draft) return;
  clearError(); renderDraft();
  try { await refreshSession(); }
  catch {
    draftStatusUnknown = true;
    draftRefreshWarning = "The saved operation returned the status above, but account status could not refresh. Use Refresh draft status before continuing.";
    updateDraftControls(); return;
  }
  if (!draft) return;
  const warnings = await refreshSupportingViews();
  draftRefreshWarning = warnings.length ? "Some workspace information could not refresh. " + warnings.join(" ") + " The saved operation above is confirmed. Use Refresh workspace status to retry." : "";
  updateDraftControls();
}
async function refreshSupportingViews() {
  const outcomes = await Promise.allSettled([refreshActivity(),refreshHistory()]);
  const warnings = outcomes.flatMap((result,index) => result.status === "rejected" ? [(index ? "Draft history" : "Activity") + " could not refresh."] : []);
  $("workspaceHint").textContent = workspaceHintBase + (warnings.length ? " " + warnings.join(" ") + " Saved work remains available. Choose Refresh workspace status to retry." : "");
  $("retryStatus").classList.toggle("hidden",!warnings.length);
  return warnings;
}
async function refreshActivity() {
  const rows = await api("/activity"), box = $("activity"); box.replaceChildren();
  if (!rows.length) box.append(node("p","No activity yet. Confirm your profile and run your first scan.","muted"));
  for (const row of rows) {
    const item = node("div","","activity-row"); item.append(node("span",new Date(row.created).toLocaleString() + " · " + row.event));
    if (row.reference && /^(Action |Local draft|Draft )/.test(row.event)) item.append(button("Open draft",() => openDraft(row.reference))); box.append(item);
  }
}
async function refreshHistory() {
  history = [...new Map((await api("/actions")).map(action => [action.id,action])).values()]; const box = $("draftHistory"), recovery = $("draftRecovery"); box.replaceChildren(); recovery.replaceChildren();
  if (!history.length) box.append(node("div","No drafts yet. Prepare local outreach from a job, or connect Gmail and scan for reply requests.","empty"));
  for (const action of history) {
    const card = node("div","","card"); card.append(node("p",action.state.toUpperCase() + " · " + new Date(action.created).toLocaleString(),"eyebrow"));
    if (action.account_available) {
      card.append(node("h3",action.payload.subject || "Local draft")); if (action.payload.account) card.append(node("p","Account: " + action.payload.account,"muted"));
      card.append(button(["unknown","executing"].includes(action.state) ? "Open recovery" : "Open draft",() => openDraft(action.id)));
    } else {
      card.append(node("h3","Draft from another Gmail connection"),node("p","Content is hidden. Connect the original account to review or reconcile it.","muted"));
      if (session.gmail_configured) card.append(button("Connect original Gmail account",() => startGmail(false)));
    }
    if (action.result.message) card.append(node("p",action.result.message,"muted")); box.append(card);
    if (action.account_available && draftEdits.has(action.id)) {
      card.append(node("p","Unsaved edits recovered in this browser tab.","notice"));
      const item = node("div","","recovery-row"); item.append(node("p","Unsaved draft text is retained in this tab."),button("Open retained draft",() => openDraft(action.id))); recovery.append(item);
    }
  }
  const uncertain = history.filter(action => action.state === "unknown").length;
  const pending = history.filter(action => action.state === "executing").length;
  if (uncertain + pending) {
    const summary = node("div","","recovery-row");
    summary.append(node("p",(uncertain + pending) + ((uncertain + pending) === 1 ? " draft needs" : " drafts need") + " attention: " + uncertain + " uncertain, " + pending + " pending. " + (uncertain ? "Reconcile uncertain outcomes before another create. " : "") + (pending ? "Check pending operations' saved status. " : "") + "Each draft is available in history; another account's content stays hidden."),button("Open Draft history",() => tabView("actions")));
    recovery.append(summary);
  }
  recovery.classList.toggle("hidden",!recovery.children.length);
}
async function startGmail(compose) {
  if (busy.has("oauth")) return; busy.add("oauth"); syncControls();
  try { $("connection").textContent = "Opening Google authorization…"; const value = await api("/gmail/connect?compose=" + compose,"POST"); location.assign(value.url); }
  catch (error) { await refreshSession().catch(() => {}); throw error; } finally { busy.delete("oauth"); syncControls(); }
}
async function refreshWorkspace() {
  const changed = await refreshSession(); offline = false;
  await refreshProfile();
  if (!initialized) {
    scanId = localStorage.getItem("careerpilot-scan") || "";
    if (!scanId) { const latest = (await api("/scans/latest")).scan; if (latest) { scanId = latest.id; localStorage.setItem("careerpilot-scan",scanId); } }
    initialized = true;
  }
  if (scanId) await poll(); else resetScan();
  if (!currentScan || !["queued","running"].includes(currentScan.state)) await refreshSupportingViews();
  if (changed) channel?.postMessage({type:"gmail_changed"}); syncControls();
}
function oauthNotice() {
  const params = new URLSearchParams(location.search), error = params.get("gmail_error");
  const messages = {
    oauth_denied:"Gmail authorization was canceled or declined. Your existing workspace and connection were not changed.",
    oauth_state_invalid:"This authorization attempt expired, was already used, or was interrupted. Start a fresh connection attempt.",
    gmail_read_scope_missing:"Gmail read permission was not granted. Retry and choose read access, or continue without Gmail.",
    oauth_failed:"Google authorization could not finish. Check the local OAuth configuration or retry when connected.",
    fixture_gmail_disabled:"Real Gmail is unavailable in the synthetic demo. Continue with the demo or configure live mode."
  };
  if (error) { connectionNoticeKind = "oauth_error"; connectionNotice(messages[error] || messages.oauth_failed,true,true); }
  else if (params.get("gmail") === "connected") {
    connectionNoticeKind = "connected";
    connectionNotice("Gmail connected" + (session.compose ? " with draft access" : " read-only") + ". Run a fresh scan of this account and prepare new drafts; older approvals were invalidated."); channel?.postMessage({type:"gmail_changed"});
  }
  if (error || params.has("gmail")) window.history.replaceState(null,"",location.pathname);
}

document.querySelectorAll("[data-close]").forEach(element => element.addEventListener("click",() => { $(element.dataset.close).close(); clearError(); }));
document.querySelectorAll("dialog").forEach(dialog => {
  dialog.addEventListener("cancel",clearError);
  dialog.addEventListener("close",clearError);
});
document.querySelectorAll("[data-tab]").forEach(element => element.addEventListener("click",() => tabView(element.dataset.tab)));
wire("editProfile",async () => { await refreshProfile(); showDialog("profileDialog"); });
wire("reloadProfile",async () => {
  if (profileDirty() && !window.confirm("Replace these unsaved profile fields with the latest saved profile?")) return;
  await refreshProfile({replaceForm:true}); clearError();
});
wire("exampleProfile",async () => { formProfile((await api("/profile/example")).profile); clearError(); $("extractionNotice").textContent = "Example loaded. Review facts and replace the example goals with your own."; });
wire("modelSetup",async () => {
  showDialog("modelHelpDialog");
  const value = await api("/model");
  $("modelSetupStatus").textContent = value.setup_error || (value.configured ? "Connected: " + value.model + " · " + value.provider : "Choose Groq Free plan, create an API key, and enter it here.");
  $("modelChoice").value = ["openai/gpt-oss-120b","openai/gpt-oss-20b"].includes(value.model) ? value.model : "openai/gpt-oss-120b";
  $("modelApiKey").value = ""; syncControls();
});
$("modelHelpDialog").addEventListener("close",() => { $("modelApiKey").value = ""; });
$("modelSetupForm").addEventListener("submit",async event => {
  event.preventDefault(); if (busy.has("model")) return;
  busy.add("model"); clearError(); syncControls();
  $("modelSetupStatus").textContent = "Testing with synthetic data only…";
  try {
    const value = await api("/model/setup","POST",{model:$("modelChoice").value,api_key:$("modelApiKey").value});
    $("processingConsent").checked = false; $("profileProcessingConsent").checked = false;
    await refreshSession(); $("modelSetupStatus").textContent = value.message;
    channel?.postMessage({type:"model_changed"});
  } catch (error) {
    $("modelSetupStatus").textContent = "Setup failed. Previous configuration is retained. Re-enter your key and retry.";
    showError(error);
  } finally { $("modelApiKey").value = ""; busy.delete("model"); syncControls(); }
});
wire("disableModel",async () => {
  busy.add("model"); syncControls();
  try {
    const value = await api("/model/disable","POST");
    $("processingConsent").checked = false; $("profileProcessingConsent").checked = false; $("modelApiKey").value = "";
    await refreshSession(); $("modelSetupStatus").textContent = value.message; channel?.postMessage({type:"model_changed"});
  } finally { busy.delete("model"); syncControls(); }
});
wire("uploadPdf",() => $("pdfFile").click());
wire("extractText",async () => {
  $("extractionNotice").textContent = "Extracting the pasted CV text…";
  try {
    const value = await api("/profile/text","POST",{text:$("cvText").value,processing_consent:$("profileProcessingConsent").checked});
    formProfile(value.profile); $("cvFileStatus").textContent = "Profile extracted from pasted CV text."; clearError(); $("extractionNotice").textContent = value.notice;
  } catch (error) { $("extractionNotice").textContent = "Text extraction failed. Existing profile fields are retained; check the text and retry."; throw error; }
});
$("pdfFile").addEventListener("change",async () => {
  if (!$("pdfFile").files.length || busy.has("upload")) return; busy.add("upload"); clearError(); $("extractText").disabled = true; syncControls();
  const file = $("pdfFile").files[0];
  $("cvFileStatus").textContent = "Selected CV: " + file.name + " · " + Math.ceil(file.size / 1024) + " KB · extracting…";
  $("extractionNotice").textContent = "Extracting your CV. The profile fields will appear below; wait before confirming.";
  try {
    const body = new FormData(); body.append("file",file); body.append("processing_consent",String($("profileProcessingConsent").checked));
    const value = await api("/profile/upload","POST",body); formProfile(value.profile); clearError(); $("extractionNotice").textContent = value.notice;
    $("cvFileStatus").textContent = "CV loaded: " + file.name + " · " + (value.profile.skills || []).length + " skills extracted. Review the fields below.";
  } catch (error) {
    $("cvFileStatus").textContent = "CV extraction failed: " + file.name + ". Existing profile fields are retained. Choose a readable PDF or paste CV text to retry.";
    $("extractionNotice").textContent = "The selected CV was not applied to your profile."; showError(error);
  } finally { busy.delete("upload"); $("extractText").disabled = false; $("pdfFile").value = ""; syncControls(); }
});
$("profileForm").addEventListener("submit",async event => {
  event.preventDefault(); if (busy.has("profile") || profileConflict) return; busy.add("profile"); clearError(); const submit = $("profileForm").querySelector('[type="submit"]'); submit.disabled = true;
  ++profileReadSequence; const previousVersion = profileVersion;
  try {
    const value = await api("/profile/confirm","POST",collectProfile(),{"X-Profile-Version":profileFormVersion}); hasProfile = true; profileVersion = value.id; ++profileReadSequence; loadProfileForm(value); profile = value.profile;
    $("profileSummary").textContent = profile.name + " · " + profile.target_roles.join(", "); $("profileDialog").close(); clearError();
    if (currentScan && previousVersion !== value.id) { currentScan.profile_current = false; renderScan(currentScan); }
    clearAnswer(); channel?.postMessage({type:"profile_changed"}); await refreshSupportingViews();
  } catch (error) { if (error.code === "profile_conflict") await refreshProfile().catch(() => {}); showError(error); }
  finally { busy.delete("profile"); submit.disabled = false; syncControls(); }
});
wire("scanButton",startScan);
wire("cancelScan",async () => { await api("/scans/" + scanId + "/cancel","POST"); $("progress").textContent = "Cancellation requested. Checking saved status…"; await poll(); });
wire("retryStatus",refreshWorkspace);
$("contactForm").addEventListener("submit",async event => {
  event.preventDefault(); if (busy.has("contact")) return;
  $("contactSource").value = $("contactSource").value.trim();
  $("contactSource").setCustomValidity($("contactSource").value ? "" : "Explain where this recruitment contact came from.");
  if (!$("contactSource").reportValidity()) return;
  busy.add("contact"); clearError(); const submit = $("contactForm").querySelector('button:not([type="button"])'); submit.disabled = true;
  try {
    draft = await api("/drafts","POST",{scan_id:scanId,source_kind:"job",source_id:pendingContact,processing_consent:$("processingConsent").checked,sender_identity_confirmed:$("senderIdentityConfirmed").checked,sender_account:session.account || "",recipient:$("contactEmail").value,contact_confirmed:$("contactConfirmed").checked,contact_source:$("contactSource").value});
    $("contactDialog").close(); clearError(); renderDraft(); showDialog("draftDialog"); await refreshSupportingViews();
  } catch (error) { showError(error); if (error.code === "pending_execution") await refreshHistory().catch(() => {}); }
  finally { busy.delete("contact"); submit.disabled = false; }
});
$("contactSource").addEventListener("input",() => $("contactSource").setCustomValidity(""));
wire("saveDraft",() => {
  for (const [id,label] of [["draftTo","recipient"],["draftSubject","subject"],["draftBody","body"]]) {
    const input = $(id); input.setCustomValidity(input.value.trim() ? "" : "Enter a " + label + ".");
    if (!input.reportValidity()) return;
  }
  return draftOperation("","PUT",{to:$("draftTo").value,subject:$("draftSubject").value,body:$("draftBody").value});
});
for (const [id,decision] of [["approveDraft","approve"],["rejectDraft","reject"]]) wire(id,async () => {
  if (draftDirty()) throw Error("Save edits and review the saved content before approval or rejection.");
  await draftOperation("/review","POST",{payload_hash:draft.payload_hash,version:draft.version,decision});
});
wire("executeDraft",async () => { if (draftDirty()) throw Error("Save and review your edits first."); await draftOperation("/execute","POST"); });
wire("reconcileDraft",() => draftOperation("/reconcile","POST")); wire("refreshDraft",refreshDraft);
wire("discardDraftEdits",() => { draftEdits.delete(draft.id); renderDraft(); });
wire("viewDraftEdits",() => {
  const edit = draftEdits.get(draft.id); if (!edit) return;
  detailKind = edit.gmail ? "email" : "draft"; $("detailTitle").textContent = "Retained unsaved draft edits";
  $("detailBody").replaceChildren(node("p","These edits were not saved or used for the Gmail operation."),node("p","Subject: " + edit.subject),node("pre",edit.body,"detail-text"));
  showDialog("detailDialog");
});
wire("reloadDraftPage",() => {
  persistDraftEdits();
  if (!editStorageAvailable) throw Error("Save edits before reloading; browser recovery storage is unavailable.");
  if (profileDirty()) throw Error("Review or save your unsaved profile changes before reloading.");
  reloadWithRecoveredEdits = true; window.location.reload();
});
wire("draftCapability",async () => { $("draftDialog").close(); await startGmail(true); });
for (const id of ["draftTo","draftSubject","draftBody"]) $(id).addEventListener("input",() => { $(id).setCustomValidity(""); rememberDraftEdits(); updateDraftControls(); });
window.addEventListener("beforeunload",event => {
  if ((!reloadWithRecoveredEdits && draftEdits.size) || profileDirty()) { event.preventDefault(); event.returnValue = ""; }
});
$("questionForm").addEventListener("submit",async event => {
  event.preventDefault(); if (busy.has("question")) return; busy.add("question"); clearError(); syncControls();
  try {
    if (!scanId) throw Error("Run a career scan first.");
    const requestScan = scanId, requestProfile = profileVersion, requestAccount = session.account;
    const askedQuestion = $("question").value;
    clearAnswer({keepContext:true});
    const value = await api("/questions","POST",{scan_id:scanId,question:askedQuestion,processing_consent:$("processingConsent").checked,previous_question:questionContext.question,previous_source_ids:questionContext.source_ids});
    if (scanId !== requestScan || profileVersion !== requestProfile || session.account !== requestAccount) return;
    if (!$("processingConsent").checked && session.mode === "live" && session.model_configured) return;
    renderAnswer(value,askedQuestion); clearError();
  } catch (error) { if (error.status === 404) resetScan(); showError(error); } finally { busy.delete("question"); syncControls(); }
});
wire("connectGmail",() => startGmail(false)); wire("composeGmail",() => startGmail(true)); wire("retryGmail",() => startGmail(false));
wire("disconnectGmail",async () => {
  clearGmailViews(); $("connection").textContent = "Disconnecting Gmail…";
  try {
    const result = await api("/gmail/disconnect","POST"); await refreshSession();
    connectionNotice(result.message + " " + result.retention,result.revocation_confirmed === false,false,result.revocation_confirmed === false);
    if (scanId) await poll(); await refreshSupportingViews(); channel?.postMessage({type:"gmail_changed"});
  } catch (error) { await refreshSession().catch(() => {}); throw error; }
});
wire("dismissConnectionNotice",async () => { if (session.gmail_disconnect) await api("/gmail/disconnect/acknowledge","POST"); session.gmail_disconnect = null; $("connectionNotice").classList.add("hidden"); });
$("emailConsent").addEventListener("change",async () => {
  clearError(); try { await api("/gmail/consent","POST",{enabled:$("emailConsent").checked}); await refreshSession(); }
  catch (error) { await refreshSession().catch(() => {}); showError(error); }
});
channel?.addEventListener("message",event => {
  if (event.data?.type === "processing_revoked") { $("processingConsent").checked=false; clearAnswer(); }
  if (["gmail_changed","profile_changed","model_changed"].includes(event.data?.type)) refreshWorkspace().catch(showError);
});
window.addEventListener("online",() => refreshWorkspace().catch(showError));
window.addEventListener("pageshow",event => {
  if (event.persisted) { clearGmailViews({initial:true}); $("connection").textContent = "Checking the current Gmail connection…"; refreshWorkspace().catch(showError); }
});
window.addEventListener("focus",() => { if (initialized && !busy.size) refreshWorkspace().catch(showError); });
setInterval(() => {
  if (document.visibilityState === "visible" && initialized && !busy.size && !polling) {
    refreshSession().then(async changed => { if (changed && scanId) await poll(); if (!$("actionsPanel").classList.contains("hidden")) await refreshHistory(); }).catch(() => {});
  }
  syncControls();
},3000);
(async () => {
  try { await refreshWorkspace(); oauthNotice(); }
  catch (error) { offline = true; $("modeBadge").textContent = "WORKSPACE UNAVAILABLE"; showError(error); $("retryStatus").classList.remove("hidden"); syncControls(); }
})();

$("processingConsent").addEventListener("change",async () => {
  if (!$("processingConsent").checked) { clearAnswer(); channel?.postMessage({type:"processing_revoked"}); }
  try { await api("/processing/consent","POST",{enabled:$("processingConsent").checked}); }
  catch (error) { $("processingConsent").checked=false; showError(error); }
});
