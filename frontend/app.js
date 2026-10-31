/* Leave Management System - single-page frontend (no build step, no libraries).
 *
 * Login: Cognito USER_PASSWORD_AUTH called directly over HTTPS (InitiateAuth).
 * API:   every call sends the Cognito ID token in the Authorization header;
 *        API Gateway's Cognito authorizer verifies it before Lambda runs.
 */
(function () {
  "use strict";

  const CONFIG = window.APP_CONFIG || {};
  const STORAGE_KEY = "leave-app-session";
  const STATUS_LABEL = {
    PENDING_MANAGER: "Pending manager",
    PENDING_HR: "Pending HR",
    APPROVED: "Approved",
    REJECTED: "Rejected",
    AUTO_REJECTED: "Auto-rejected",
  };
  const LEVEL_LABEL = { MANAGER: "Manager approval", HR: "HR approval (long leave)", ESCALATION: "Escalated to HR" };

  const state = { me: null, leaveTypes: [], balances: [], calMonth: null, pendingChallenge: null };
  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------ helpers
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function toast(text) {
    const el = $("toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, 4000);
  }

  function isoDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function parseIso(s) {
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d);
  }

  function fmtDate(s) {
    return parseIso(s).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }

  function dateRange(start, end) {
    return start === end ? fmtDate(start) : `${fmtDate(start)} – ${fmtDate(end)}`;
  }

  function badge(status) {
    return `<span class="badge ${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;
  }

  function weekdaysBetween(startIso, endIso) {
    let count = 0;
    const end = parseIso(endIso);
    for (let d = parseIso(startIso); d <= end; d.setDate(d.getDate() + 1)) {
      const wd = d.getDay();
      if (wd !== 0 && wd !== 6) count += 1;
    }
    return count;
  }

  function setBusy(button, busy) {
    button.disabled = busy;
  }

  // ------------------------------------------------------------ session / Cognito
  function loadSession() {
    try { return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null"); } catch (e) { return null; }
  }

  function saveSession(s) {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(s)); } catch (e) { /* storage blocked */ }
  }

  function clearSession() {
    try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) { /* ignore */ }
  }

  function tokenExpiry(jwt) {
    try {
      const payload = JSON.parse(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      return payload.exp * 1000;
    } catch (e) {
      return 0;
    }
  }

  async function cognito(action, body) {
    const resp = await fetch(`https://cognito-idp.${CONFIG.region}.amazonaws.com/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}`,
      },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const err = new Error(data.message || data.__type || "Sign-in failed");
      err.code = (data.__type || "").split("#").pop();
      throw err;
    }
    return data;
  }

  function storeAuthResult(result, previous) {
    const session = {
      idToken: result.IdToken,
      refreshToken: result.RefreshToken || (previous && previous.refreshToken),
      expiresAt: tokenExpiry(result.IdToken),
    };
    saveSession(session);
    return session;
  }

  async function signIn(email, password, newPassword) {
    if (state.pendingChallenge && newPassword) {
      const data = await cognito("RespondToAuthChallenge", {
        ClientId: CONFIG.userPoolClientId,
        ChallengeName: "NEW_PASSWORD_REQUIRED",
        Session: state.pendingChallenge,
        ChallengeResponses: { USERNAME: email, NEW_PASSWORD: newPassword },
      });
      state.pendingChallenge = null;
      return storeAuthResult(data.AuthenticationResult);
    }
    const data = await cognito("InitiateAuth", {
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: CONFIG.userPoolClientId,
      AuthParameters: { USERNAME: email, PASSWORD: password },
    });
    if (data.ChallengeName === "NEW_PASSWORD_REQUIRED") {
      state.pendingChallenge = data.Session;
      return null;
    }
    if (!data.AuthenticationResult) throw new Error(`Unsupported sign-in step: ${data.ChallengeName}`);
    return storeAuthResult(data.AuthenticationResult);
  }

  async function validSession() {
    const session = loadSession();
    if (!session) return null;
    if (session.expiresAt - Date.now() > 60 * 1000) return session;
    if (!session.refreshToken) return null;
    try {
      const data = await cognito("InitiateAuth", {
        AuthFlow: "REFRESH_TOKEN_AUTH",
        ClientId: CONFIG.userPoolClientId,
        AuthParameters: { REFRESH_TOKEN: session.refreshToken },
      });
      return storeAuthResult(data.AuthenticationResult, session);
    } catch (e) {
      return null;
    }
  }

  // ------------------------------------------------------------ API
  async function api(method, path, body) {
    const session = await validSession();
    if (!session) {
      showLogin("Your session has ended. Please sign in again.");
      throw new Error("Not signed in");
    }
    const resp = await fetch(CONFIG.apiUrl.replace(/\/$/, "") + path, {
      method,
      headers: { Authorization: session.idToken, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await resp.json(); } catch (e) { /* empty body */ }
    if (resp.status === 401) {
      clearSession();
      showLogin("Your session has ended. Please sign in again.");
    }
    if (!resp.ok) {
      const err = new Error(data.error || data.reason || data.message || `Request failed (${resp.status})`);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------ views
  function showLogin(message) {
    $("app-view").hidden = true;
    $("login-view").hidden = false;
    $("login-error").textContent = message || "";
  }

  function showView(name) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
    document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${name}`; });
    if (name === "approvals") loadApprovals();
    if (name === "calendar") loadCalendar();
  }

  async function startApp() {
    $("login-view").hidden = true;
    $("app-view").hidden = false;
    try {
      state.me = await api("GET", "/me");
    } catch (e) {
      if (e.status !== 401) showLogin(e.message);
      return;
    }
    const approver = state.me.is_manager || state.me.is_hr;
    document.querySelectorAll('[data-role="approver"]').forEach((el) => { el.hidden = !approver; });
    $("user-name").textContent = state.me.name;
    $("user-roles").innerHTML = state.me.groups.map((g) => `<span>${esc(g)}</span>`).join("");
    showView("mine");
    await Promise.all([loadLeaveTypes(), loadBalances(), loadHistory()]);
    if (approver) refreshPendingCount();
  }

  // ------------------------------------------------------------ employee
  async function loadLeaveTypes() {
    const data = await api("GET", "/config/leave-types");
    state.leaveTypes = data.leave_types;
    renderLeaveTypeOptions();
    $("rules").innerHTML = "<dl>" + state.leaveTypes.map((t) => {
      const parts = [];
      parts.push(t.requires_balance ? `${t.annual_quota} days per year` : "No balance needed");
      if (t.carry_forward) parts.push(`up to ${t.max_carry_forward} unused days carry forward`);
      if (t.min_notice_days) parts.push(`${t.min_notice_days} day(s) notice`);
      if (t.max_backdate_days) parts.push(`can be applied up to ${t.max_backdate_days} day(s) late`);
      parts.push(`max ${t.max_consecutive_days} working days at a time`);
      return `<dt>${esc(t.name)}</dt><dd>${esc(parts.join(" · "))}</dd>`;
    }).join("") + "</dl><p class='hint'>Leave longer than the HR threshold also needs HR Admin approval. " +
      "Your balance is only deducted after final approval.</p>";
  }

  function renderLeaveTypeOptions() {
    const select = $("leave-type");
    const current = select.value;
    select.innerHTML = state.leaveTypes.map((t) => {
      const bal = state.balances.find((b) => b.leave_type === t.leave_type);
      const extra = bal && bal.requires_balance ? ` (${bal.usable} available)` : "";
      return `<option value="${esc(t.leave_type)}">${esc(t.name + extra)}</option>`;
    }).join("");
    if (current) select.value = current;
  }

  async function loadBalances() {
    const data = await api("GET", "/me/balances");
    state.balances = data.balances;
    $("balance-year").textContent = String(data.year);
    $("balances").innerHTML = data.balances.map((b) => {
      if (!b.requires_balance) {
        return `<div class="balance"><div class="name">${esc(b.name)}</div>
          <div class="big">${esc(b.used)} <small>days used</small></div>
          <div class="detail">No balance limit${b.pending ? ` · ${esc(b.pending)} pending` : ""}</div></div>`;
      }
      const total = Math.max(b.allocated + b.carried_forward, 1);
      const usedPct = Math.min(100, (b.used / total) * 100);
      const pendPct = Math.min(100 - usedPct, (b.pending / total) * 100);
      return `<div class="balance"><div class="name">${esc(b.name)}</div>
        <div class="big">${esc(b.usable)} <small>of ${esc(b.allocated + b.carried_forward)} available</small></div>
        <div class="bar"><i class="used" style="width:${usedPct}%"></i><i class="pend" style="width:${pendPct}%"></i></div>
        <div class="detail">Quota ${esc(b.allocated)}${b.carried_forward ? ` + ${esc(b.carried_forward)} carried forward` : ""}
          · Used ${esc(b.used)}${b.pending ? ` · Pending ${esc(b.pending)}` : ""}</div></div>`;
    }).join("");
    renderLeaveTypeOptions();
  }

  async function loadHistory() {
    const data = await api("GET", "/me/leaves");
    const rows = data.requests;
    if (!rows.length) {
      $("history").innerHTML = `<tr><td colspan="6" class="empty">No leave requests yet.</td></tr>`;
      return;
    }
    $("history").innerHTML = rows.map((r) => {
      let detail = r.reason ? esc(r.reason) : "";
      if (r.rejection_reason) detail = `<strong>Reason:</strong> ${esc(r.rejection_reason)}`;
      else if (r.status === "PENDING_HR" && r.escalated) detail = "Escalated to HR (manager did not respond in time)";
      else if (r.status === "APPROVED" && r.approved_at) detail = `Approved ${esc(fmtDate(r.approved_at.slice(0, 10)))}`;
      return `<tr><td class="mono">${esc(r.request_id)}</td><td>${esc(r.leave_type_name || r.leave_type)}</td>
        <td>${esc(dateRange(r.start_date, r.end_date))}</td><td>${esc(r.days)}</td>
        <td>${badge(r.status)}</td><td class="detail">${detail}</td></tr>`;
    }).join("");
  }

  function updateEstimate() {
    const s = $("start-date").value;
    const e = $("end-date").value;
    const out = $("day-estimate");
    if (!s || !e) { out.textContent = ""; return; }
    if (e < s) { out.textContent = "The end date must be on or after the start date."; return; }
    const n = weekdaysBetween(s, e);
    out.textContent = `${n} working day(s), excluding weekends. Company holidays are also excluded when you submit.`;
  }

  async function submitLeave(ev) {
    ev.preventDefault();
    const msg = $("apply-message");
    const payload = {
      leave_type: $("leave-type").value,
      start_date: $("start-date").value,
      end_date: $("end-date").value,
      reason: $("reason").value.trim(),
    };
    msg.className = "message bad";
    if (!payload.leave_type) { msg.textContent = "Choose a leave type."; return; }
    if (!payload.start_date || !payload.end_date) { msg.textContent = "Choose both dates."; return; }
    if (payload.end_date < payload.start_date) { msg.textContent = "The end date must be on or after the start date."; return; }

    const button = $("apply-button");
    setBusy(button, true);
    msg.className = "message";
    msg.textContent = "Submitting…";
    try {
      const data = await api("POST", "/leaves", payload);
      msg.className = "message ok";
      msg.textContent = `Submitted (${data.request.days} working day(s)). Your ${data.request.requires_hr ? "manager and HR" : "manager"} will be notified.`;
      $("apply-form").reset();
      $("day-estimate").textContent = "";
    } catch (e) {
      msg.className = "message bad";
      msg.textContent = e.status === 422 ? `Automatically rejected: ${e.data.reason}` : e.message;
    } finally {
      setBusy(button, false);
      loadHistory().catch(() => {});
      loadBalances().catch(() => {});
    }
  }

  // ------------------------------------------------------------ approvals
  async function refreshPendingCount() {
    try {
      const data = await api("GET", "/approvals/pending");
      const el = $("pending-count");
      el.textContent = String(data.pending.length);
      el.hidden = data.pending.length === 0;
      return data;
    } catch (e) {
      return null;
    }
  }

  async function loadApprovals() {
    const box = $("approvals");
    box.innerHTML = `<p class="muted">Loading…</p>`;
    let data;
    try {
      data = await refreshPendingCount() || await api("GET", "/approvals/pending");
    } catch (e) {
      box.innerHTML = `<p class="error">${esc(e.message)}</p>`;
      return;
    }
    if (!data.pending.length) {
      box.innerHTML = `<div class="panel empty">Nothing waiting for your approval.</div>`;
      return;
    }
    box.innerHTML = data.pending.map((r, i) => `
      <div class="approval" data-index="${i}">
        <div>
          <h3>${esc(r.employee_name)} · ${esc(r.leave_type_name)}</h3>
          <div class="meta">
            ${esc(dateRange(r.start_date, r.end_date))} · <strong>${esc(r.days)} working day(s)</strong>
            <span class="badge level">${esc(LEVEL_LABEL[r.my_level] || r.my_level)}</span><br>
            ${r.reason ? `Reason: ${esc(r.reason)}<br>` : ""}
            <span class="mono">${esc(r.request_id)}</span> · submitted ${esc(fmtDate(r.created_at.slice(0, 10)))}
          </div>
        </div>
        <div class="actions">
          <textarea placeholder="Comment (optional)" maxlength="500" aria-label="Comment"></textarea>
          <div class="btns">
            <button class="btn approve" data-decision="APPROVED">Approve</button>
            <button class="btn reject" data-decision="REJECTED">Reject</button>
          </div>
        </div>
      </div>`).join("");
    box.querySelectorAll(".approval").forEach((card) => {
      const r = data.pending[Number(card.dataset.index)];
      card.querySelectorAll("button[data-decision]").forEach((btn) => {
        btn.addEventListener("click", () => decide(r, btn.dataset.decision, card));
      });
    });
  }

  async function decide(r, decision, card) {
    const buttons = card.querySelectorAll("button");
    buttons.forEach((b) => setBusy(b, true));
    try {
      await api("POST", `/approvals/${encodeURIComponent(r.employee_id)}/${encodeURIComponent(r.request_id)}`, {
        decision,
        comment: card.querySelector("textarea").value,
      });
      toast(`${decision === "APPROVED" ? "Approved" : "Rejected"}: ${r.employee_name}, ${dateRange(r.start_date, r.end_date)}`);
    } catch (e) {
      toast(e.message);
    }
    // The workflow finishes within a second or two; reload after a short pause.
    setTimeout(() => loadApprovals(), 1200);
  }

  // ------------------------------------------------------------ calendar
  async function loadCalendar() {
    if (!state.calMonth) {
      const now = new Date();
      state.calMonth = new Date(now.getFullYear(), now.getMonth(), 1);
    }
    const first = state.calMonth;
    const gridStart = new Date(first);
    gridStart.setDate(1 - ((first.getDay() + 6) % 7)); // Monday on/before the 1st
    const gridEnd = new Date(gridStart);
    gridEnd.setDate(gridStart.getDate() + 41);
    $("cal-title").textContent = first.toLocaleDateString(undefined, { month: "long", year: "numeric" });

    const cal = $("calendar");
    let data;
    try {
      data = await api("GET", `/calendar?from=${isoDate(gridStart)}&to=${isoDate(gridEnd)}`);
    } catch (e) {
      cal.innerHTML = `<p class="error">${esc(e.message)}</p>`;
      return;
    }
    const holidays = new Set(data.holidays);
    const todayIso = isoDate(new Date());
    let html = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => `<div class="dow">${d}</div>`).join("");
    for (let i = 0; i < 42; i += 1) {
      const d = new Date(gridStart);
      d.setDate(gridStart.getDate() + i);
      const iso = isoDate(d);
      const weekend = d.getDay() === 0 || d.getDay() === 6;
      const classes = ["day"];
      if (d.getMonth() !== first.getMonth()) classes.push("other");
      if (weekend) classes.push("weekend");
      if (holidays.has(iso)) classes.push("holiday");
      if (iso === todayIso) classes.push("today");
      const people = weekend || holidays.has(iso) ? [] :
        data.entries.filter((e) => e.start_date <= iso && e.end_date >= iso);
      const shown = people.slice(0, 3).map((p) => {
        const cls = p.status === "APPROVED" ? "APPROVED" : "pending";
        const title = `${p.employee_name} · ${p.leave_type_name} · ${STATUS_LABEL[p.status] || p.status}`;
        return `<span class="who ${cls}" title="${esc(title)}">${esc(p.employee_name)}</span>`;
      }).join("");
      const more = people.length > 3 ? `<span class="more">+${people.length - 3} more</span>` : "";
      html += `<div class="${classes.join(" ")}"><span class="num">${d.getDate()}</span>${shown}${more}</div>`;
    }
    cal.innerHTML = html;

    if (todayIso < isoDate(gridStart) || todayIso > isoDate(gridEnd)) {
      $("today-list").innerHTML = `<p class="muted">Click "This month" to see who is off today.</p>`;
      return;
    }
    const today = data.entries.filter((e) => e.status === "APPROVED" && e.start_date <= todayIso && e.end_date >= todayIso);
    $("today-list").innerHTML = today.length
      ? "<ul>" + today.map((e) => `<li>${esc(e.employee_name)} – ${esc(e.leave_type_name)} until ${esc(fmtDate(e.end_date))}</li>`).join("") + "</ul>"
      : `<p class="muted">Nobody is on approved leave today.</p>`;
  }

  function moveMonth(delta) {
    const m = state.calMonth || new Date();
    state.calMonth = new Date(m.getFullYear(), m.getMonth() + delta, 1);
    loadCalendar();
  }

  // ------------------------------------------------------------ reports
  async function downloadReport(ev) {
    ev.preventDefault();
    const from = $("report-from").value;
    const to = $("report-to").value;
    const msg = $("report-message");
    msg.className = "message bad";
    if (!from || !to) { msg.textContent = "Choose both dates."; return; }
    if (to < from) { msg.textContent = "'To' must be on or after 'From'."; return; }
    const button = $("report-button");
    setBusy(button, true);
    msg.className = "message";
    msg.textContent = "Preparing report…";
    try {
      const data = await api("GET", `/reports/leaves?from=${from}&to=${to}`);
      msg.className = "message ok";
      msg.textContent = `${data.rows} request(s) in ${data.filename}. Download starting…`;
      window.location.href = data.url;
    } catch (e) {
      msg.className = "message bad";
      msg.textContent = e.message;
    } finally {
      setBusy(button, false);
    }
  }

  // ------------------------------------------------------------ wiring
  function init() {
    if (!CONFIG.apiUrl || !CONFIG.userPoolClientId || !CONFIG.region) {
      showLogin("config.js is missing. Run scripts/deploy_frontend.py to generate it.");
      $("login-button").disabled = true;
      return;
    }

    $("login-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const email = $("login-email").value.trim();
      const password = $("login-password").value;
      const newPassword = $("login-new-password").value;
      const err = $("login-error");
      if (!email || !password) { err.textContent = "Enter your email and password."; return; }
      if (state.pendingChallenge && !newPassword) { err.textContent = "Choose a new password."; return; }
      setBusy($("login-button"), true);
      err.textContent = "";
      try {
        const session = await signIn(email, password, newPassword);
        if (!session) {
          $("new-password-block").hidden = false;
          err.textContent = "You need to set a new password before continuing.";
          return;
        }
        $("new-password-block").hidden = true;
        $("login-password").value = "";
        $("login-new-password").value = "";
        await startApp();
      } catch (e) {
        err.textContent = e.code === "NotAuthorizedException" ? "Incorrect email or password." : e.message;
      } finally {
        setBusy($("login-button"), false);
      }
    });
    ["login-email", "login-password", "login-new-password"].forEach((id) => {
      $(id).addEventListener("input", () => { $("login-error").textContent = ""; });
    });

    $("logout").addEventListener("click", () => {
      clearSession();
      state.me = null;
      showLogin();
    });
    $("tabs").addEventListener("click", (ev) => {
      const tab = ev.target.closest(".tab");
      if (tab) showView(tab.dataset.view);
    });
    $("apply-form").addEventListener("submit", submitLeave);
    ["start-date", "end-date"].forEach((id) => {
      $(id).addEventListener("change", () => {
        if (id === "start-date" && !$("end-date").value) $("end-date").value = $("start-date").value;
        $("apply-message").textContent = "";
        updateEstimate();
      });
    });
    $("refresh-history").addEventListener("click", () => { loadHistory(); loadBalances(); });
    $("refresh-approvals").addEventListener("click", loadApprovals);
    $("cal-prev").addEventListener("click", () => moveMonth(-1));
    $("cal-next").addEventListener("click", () => moveMonth(1));
    $("cal-today").addEventListener("click", () => { state.calMonth = null; loadCalendar(); });
    $("report-form").addEventListener("submit", downloadReport);

    const now = new Date();
    $("report-from").value = isoDate(new Date(now.getFullYear(), now.getMonth(), 1));
    $("report-to").value = isoDate(new Date(now.getFullYear(), now.getMonth() + 1, 0));

    validSession().then((s) => (s ? startApp() : showLogin()));
  }

  document.addEventListener("DOMContentLoaded", init);
})();
