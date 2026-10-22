/*
 * The floating assistant: one conversation, the agent's working shown live.
 *
 * Auth is the same handshake the full page uses - Share posts a repository
 * ticket into the frame, so nobody signs in twice.
 *
 * There is one input and no mode switch. The server reads the request and
 * picks the engine; this file only reports which one answered.
 */

const $ = (id) => document.getElementById(id);
// Reassigned when the user starts a new conversation, so the agent's memory
// starts clean rather than carrying the previous thread's context.
let THREAD = `bot-${Math.random().toString(36).slice(2, 10)}`;
// Docked inside a host page, or standing on its own as the full screen.
const FRAMED = window.parent !== window;

// Mixed on purpose: the point of one box is that these need no sorting.
const SUGGESTIONS = [
  "unpaid invoices",
  "where is the Kubernetes migration runbook",
  "tag every UNPAID invoice in Finance as 'overdue'",
  "create a folder 'Q4 Reports' in Shared",
  "what can you do to this repository?",
];

const ROUTE_BADGE = {
  search: { label: "Searching", title: "Reading only \u2014 nothing will be changed" },
  explore: { label: "Investigating", title: "Reading only \u2014 nothing will be changed" },
  act: { label: "Acting", title: "Changes will be held for your approval" },
  undo: { label: "Undoing", title: "Putting this session's changes back" },
  clarify: { label: "Asking", title: "I need to know which items you mean" },
};

const MOOD_BY_CAUSE = { found: "found", query: "stuck", infrastructure: "stuck" };
const VERDICT_TEXT = {
  found: "Found it",
  query: "The query was at fault",
  permissions: "Excluded by permissions",
  infrastructure: "The indexing pipeline is the cause",
  unknown: "Inconclusive",
};

const score = { found: 0, solved: 0, probes: 0, applied: 0 };
const TILES = {
  found: "scoreFound",
  solved: "scoreSolved",
  probes: "scoreProbes",
  applied: "scoreApplied",
};
let busy = false;

/* ------------------------------------------------------------------ utils */
function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function markdown(text) {
  const safe = escapeHtml(text);
  const code = [];
  return safe
    .replace(/`([^`]+)`/g, (_, body) => `\u0000${code.push(`<code>${body}</code>`) - 1}\u0000`)
    // Emphasis before headings, so "### **Title**" becomes a heading with bold
    // inside rather than a heading containing literal asterisks. Neither span
    // may cross a line: one unbalanced ** from the model used to run on and
    // swallow every list that followed it.
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/^#{1,4} (.+)$/gm, "<h3>$1</h3>")
    .replace(/^\s*[-*] (.+)$/gm, "<li>$1</li>")
    .replace(/(<li>[\s\S]*?<\/li>)/g, "<ul>$1</ul>")
    .replace(/\n{2,}/g, "</p><p>")
    // Strip before the code spans come back, or a wildcard inside a query
    // such as PATH:"/a/*" would lose its asterisk.
    .replace(/\*{1,2}/g, "")
    .replace(/\u0000(\d+)\u0000/g, (_, i) => code[i])
    .replace(/^(?!<[hu])/, "<p>") + "</p>";
}

function scrollDown() {
  const log = $("log");
  log.scrollTop = log.scrollHeight;
}

function mood(state, label) {
  $("avatar").dataset.mood = state;
  $("state").textContent = label;
}

/* A long run should look accounted for rather than stalled, so the status line
 * counts while the agent works. */
let ticking = null;

function startClock(label) {
  const began = Date.now();
  stopClock();
  ticking = setInterval(() => {
    const seconds = Math.round((Date.now() - began) / 1000);
    if (seconds >= 3) mood("thinking", `${label} \u00b7 ${seconds}s`);
  }, 1000);
}

function stopClock() {
  if (ticking) clearInterval(ticking);
  ticking = null;
}

/* ---------------------------------------------------------------- bubbles */
function say(html, who = "bot") {
  const node = document.createElement("div");
  node.className = `msg ${who}`;
  node.innerHTML = html;
  $("log").append(node);
  scrollDown();
  return node;
}

/* An answer worth reading is an answer worth keeping. Added only to reports,
 * not to every status line, so the control means something where it appears. */
function withCopy(node, text) {
  node.classList.add("has-copy");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy";
  button.title = "Copy this answer";
  button.textContent = "Copy";
  button.onclick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Press Ctrl+C";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 1800);
  };
  node.append(button);
  return node;
}

function typing() {
  const node = document.createElement("div");
  node.className = "typing";
  node.innerHTML = "<i></i><i></i><i></i>";
  $("log").append(node);
  scrollDown();
  return node;
}

function trail(steps) {
  const node = document.createElement("div");
  node.className = "trail";
  node.innerHTML = steps
    .map((s) => `<span class="chip done" title="${escapeHtml(s.detail || "")}">${escapeHtml(s.node)}</span>`)
    .join("");
  $("log").append(node);
  scrollDown();
}

function verdict(cause) {
  const node = document.createElement("div");
  node.className = `verdict ${cause}`;
  node.textContent = VERDICT_TEXT[cause] || cause;
  $("log").append(node);
  scrollDown();
}

function hit(doc) {
  const node = document.createElement("div");
  node.className = "hit";
  // Links must break out of the frame, or Share would load inside the panel.
  node.innerHTML = `
    <div class="doc"></div>
    <div class="meta">
      <a class="t" href="${SHARE_BASE}/page/document-details?nodeRef=workspace://SpacesStore/${encodeURIComponent(doc.id)}"
         target="_top" rel="noopener">${escapeHtml(doc.name || doc.id)}</a>
      <div class="p">${escapeHtml(doc.path || "")}</div>
    </div>`;
  $("log").append(node);
  scrollDown();
}

function bumpScore() {
  $("scoreboard").hidden = false;
  for (const [key, id] of Object.entries(TILES)) {
    const tile = $(id);
    if (tile.textContent === String(score[key])) continue;
    tile.textContent = score[key];
    flash(tile.parentElement);
  }
  progress();
  saveProgress();
}

/* ---------------------------------------------------------- progression */
/* Weighted by effort, not by count: explaining a zero-hit search is the
 * harder result, and an approved change is the one with consequences. */
const XP_PER = { found: 10, solved: 15, probes: 2, applied: 25 };
const RANKS = [
  { at: 0, name: "Rookie" },
  { at: 40, name: "Scout" },
  { at: 100, name: "Investigator" },
  { at: 200, name: "Analyst" },
  { at: 350, name: "Specialist" },
  { at: 550, name: "Operator" },
  { at: 800, name: "Veteran" },
  { at: 1200, name: "Master" },
];
const RING = 119.4; // 2*pi*r for the r=19 circle in bot.html
let level = 1;

function xp() {
  return Object.entries(XP_PER).reduce((total, [key, per]) => total + score[key] * per, 0);
}

function progress() {
  const total = xp();
  const index = RANKS.reduce((best, rank, i) => (total >= rank.at ? i : best), 0);
  const floor = RANKS[index].at;
  const ceiling = RANKS[index + 1]?.at ?? floor + 400;
  const fraction = Math.min(1, (total - floor) / (ceiling - floor));

  $("xpRing").style.strokeDashoffset = RING * (1 - fraction);
  $("rank").textContent = RANKS[index].name;
  $("xpFill").style.width = `${Math.round(fraction * 100)}%`;
  $("xpText").textContent = RANKS[index + 1]
    ? `${total} / ${ceiling} XP \u00b7 ${RANKS[index + 1].name}`
    : `${total} XP \u00b7 ${RANKS[index].name}`;

  const earned = index + 1;
  if (earned > level) {
    level = earned;
    const badge = $("level");
    badge.textContent = level;
    flash(badge, "up");
    toast("\u2605", `Level ${level}`, RANKS[index].name);
  }
}

/* Re-adding the class is not enough to replay a CSS animation; the reflow
 * between removal and re-add is what restarts it. */
function flash(node, className = "bump") {
  if (!node) return;
  node.classList.remove(className);
  void node.offsetWidth;
  node.classList.add(className);
}

/* Milestones are announced once each. Anything that fires every turn stops
 * reading as an achievement and starts reading as noise. */
const EARNED = new Set();

function award(key, icon, title, detail) {
  if (EARNED.has(key)) return;
  EARNED.add(key);
  toast(icon, title, detail);
}

function toast(icon, title, detail = "") {
  const node = document.createElement("div");
  node.className = "toast";
  node.innerHTML = `<span class="medal">${escapeHtml(icon)}</span>
    <span>${escapeHtml(title)}${detail ? ` <small>${escapeHtml(detail)}</small>` : ""}</span>`;
  $("toasts").append(node);
  setTimeout(() => {
    node.classList.add("out");
    node.addEventListener("animationend", () => node.remove(), { once: true });
  }, 2600);
}

/* --------------------------------------------------------------- approvals */

function formatArgs(args) {
  return Object.entries(args || {})
    .map(([key, value]) => {
      const text = typeof value === "object" ? JSON.stringify(value) : String(value);
      return `<div><em>${escapeHtml(key)}</em> ${escapeHtml(text)}</div>`;
    })
    .join("") || "<div><em>no arguments</em></div>";
}

/* Each held change is its own card: approving a batch blindly is how the
 * wrong thing gets done to a thousand documents. */
function approvalCard(change, index) {
  const node = document.createElement("div");
  node.className = "approval";
  node.dataset.index = String(index);
  node.innerHTML = `
    <div class="approval-head">
      <span class="badge">NEEDS YOU</span>
      <strong>${escapeHtml(change.action)}</strong>
    </div>
    <div class="args">${formatArgs(change.arguments)}</div>
    <div class="buttons">
      <button type="button" class="approve">Approve</button>
      <button type="button" class="reject">Reject</button>
    </div>`;
  $("log").append(node);
  return node;
}

function settle(card, decision) {
  card.classList.add("settled");
  const head = card.querySelector(".approval-head");
  const label = document.createElement("span");
  label.className = `outcome ${decision}`;
  label.textContent = decision === "approved" ? "approved" : "rejected";
  head.append(label);
  head.querySelector(".badge").remove();
}

/* Resolves once every card has an answer, so one resume carries them all. */
function collectDecisions(changes) {
  return new Promise((resolve) => {
    const decisions = new Array(changes.length).fill(null);
    const cards = changes.map((change, index) => approvalCard(change, index));

    const bar = document.createElement("div");
    bar.className = "batch";
    bar.innerHTML = changes.length > 1
      ? `<span>${changes.length} changes waiting</span>
         <button type="button" class="all">Approve all</button>
         <button type="button" class="none">Reject all</button>`
      : "<span>1 change waiting</span>";
    $("log").append(bar);
    scrollDown();

    function decide(index, type) {
      if (decisions[index]) return;
      decisions[index] = type === "approve"
        ? { type: "approve" }
        : { type: "reject", message: "The user rejected this change." };
      settle(cards[index], type === "approve" ? "approved" : "rejected");
      if (decisions.every(Boolean)) {
        bar.remove();
        resolve(decisions);
      }
    }

    cards.forEach((card, index) => {
      card.querySelector(".approve").onclick = () => decide(index, "approve");
      card.querySelector(".reject").onclick = () => decide(index, "reject");
    });
    bar.querySelector(".all")?.addEventListener("click", () =>
      cards.forEach((_, index) => decide(index, "approve")));
    bar.querySelector(".none")?.addEventListener("click", () =>
      cards.forEach((_, index) => decide(index, "reject")));
  });
}

function planPanel(todos) {
  if (!todos?.length) return;
  const node = document.createElement("div");
  node.className = "plan";
  node.innerHTML = `<div class="t">Plan</div><ul>${todos
    .map((t) => {
      const done = (t.status || "").startsWith("compl");
      return `<li class="${done ? "done" : ""}">${escapeHtml(t.content || t.task || "")}</li>`;
    })
    .join("")}</ul>`;
  $("log").append(node);
  scrollDown();
}

/* ------------------------------------------------------------------- auth */
let SHARE_BASE = "";

/* What the host page can see. The agent is told only when the request points
 * at it, so an unrelated search is not biased towards the open folder. */
let PAGE = null;

function onContext(context) {
  PAGE = context && (context.folderId || context.folderName || (context.selected || []).length)
    ? context
    : null;
  renderContext();
}

function renderContext() {
  const chip = $("contextChip");
  if (!PAGE) {
    chip.hidden = true;
    return;
  }
  const picked = (PAGE.selected || []).length;
  const where = PAGE.folderName || "this folder";
  chip.textContent = picked
    ? `${picked} selected in ${where}`
    : `in ${where}`;
  chip.title = "What I can see on the page. Say \u201cthese\u201d or \u201chere\u201d to use it.";
  chip.hidden = false;
}

function announceReady(needsAuth) {
  if (window.parent === window) return;
  window.parent.postMessage({ type: "curator:ready", needsAuth }, "*");
}

function waitForTicket() {
  return new Promise((resolve) => {
    if (window.parent === window) return resolve(null);
    const timer = setTimeout(() => {
      window.removeEventListener("message", onMessage);
      resolve(null);
    }, 8000);
    function onMessage(event) {
      if (event.source !== window.parent) return;
      if (!event.data || event.data.type !== "curator:auth") return;
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      resolve(event.data);
    }
    window.addEventListener("message", onMessage);
  });
}

async function api(path, options = {}, allowRetry = true) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  // A borrowed ticket expires server-side and cannot renew itself.
  if (response.status === 401 && allowRetry && path !== "/api/connect") {
    const auth = await requestTicket();
    if (auth?.ticket && (await connect(auth))) return api(path, options, false);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function requestTicket() {
  const pending = waitForTicket();
  announceReady(true);
  return pending;
}

async function connect(auth) {
  try {
    const state = await api(
      "/api/connect",
      {
        method: "POST",
        body: JSON.stringify({ ticket: auth.ticket, base_url: auth.baseUrl || "", role: "search" }),
      },
      false,
    );
    SHARE_BASE = state.shareBaseUrl || SHARE_BASE;
    return true;
  } catch (error) {
    say(`I could not sign in to Alfresco: ${escapeHtml(error.message)}`);
    return false;
  }
}

/* ------------------------------------------------------------------- ask */
async function ask(prompt) {
  if (busy || !prompt.trim()) return;
  busy = true;
  $("send").disabled = true;
  $("suggestions").innerHTML = "";
  $("log").classList.remove("empty");

  say(escapeHtml(prompt), "user");
  mood("thinking", "working on it\u2026");
  startClock("working on it");
  // The route is only known once the server answers. Clearing it here stops a
  // failed turn from leaving the previous turn's badge on screen, which would
  // read "nothing will be changed" over a change that was actually attempted.
  $("routeBadge").hidden = true;
  const dots = typing();

  try {
    await streamAsk(prompt, dots);
  } catch (error) {
    dots.remove();
    say(explain(error.message));
    mood("stuck", "that did not work");
  } finally {
    stopClock();
    busy = false;
    $("send").disabled = false;
    renderSuggestions();
    saveConversation();
    $("prompt").focus();
  }
}

/* The route is decided before any search runs, and the report arrives a token
 * at a time, so the panel narrates the work instead of hiding it behind a
 * spinner. Falls back to the plain endpoint if the stream cannot be opened:
 * an answer late is better than no answer. */
async function streamAsk(prompt, dots) {
  const response = await fetch("/api/agent/ask/stream", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, thread_id: THREAD, context: PAGE || undefined }),
  });
  if (!response.ok || !response.body) {
    dots.remove();
    return renderWhole(await api("/api/agent/ask", {
      method: "POST",
      body: JSON.stringify({ prompt, thread_id: THREAD, context: PAGE || undefined }),
    }), typing());
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let live = null;        // the bubble the report is being written into
  let trailNode = null;   // the chips, appended to as stages finish
  let expired = false;
  // The deep agent's approval loop outlives the stream, and leaving it
  // detached would re-enable the composer while cards were still waiting.
  let pending = null;

  const handle = (name, data) => {
    if (name === "route") {
      showRoute(data.route, data.reason);
      mood("thinking", LABEL[data.route] || "working on it");
      startClock(LABEL[data.route] || "working on it");
    } else if (name === "step") {
      dots.remove();
      trailNode = chip(trailNode, data.step);
    } else if (name === "token") {
      dots.remove();
      live = live || say("");
      live.dataset.raw = (live.dataset.raw || "") + data.text;
      live.innerHTML = markdown(live.dataset.raw);
      scrollDown();
    } else if (name === "search") {
      renderSearch(data, dots, live);
      live = null;
    } else if (name === "deep") {
      pending = renderDeep(data, dots);
    } else if (name === "undo") {
      dots.remove();
      reportUndo(data);
    } else if (name === "clarify") {
      dots.remove();
      say(`<h3>Which ones?</h3><p>${escapeHtml(data.message)}</p>`);
      mood("waiting", "needs a pointer");
    } else if (name === "expired") {
      expired = true;
    } else if (name === "failed") {
      dots.remove();
      say(explain(data.detail || "the run failed"));
      mood("stuck", "that did not work");
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop();
    for (const frame of frames) {
      const name = (frame.match(/^event: (.+)$/m) || [])[1];
      const raw = (frame.match(/^data: (.*)$/m) || [])[1];
      if (!name || raw === undefined) continue;
      try { handle(name, JSON.parse(raw)); } catch { /* a partial frame */ }
    }
  }
  dots.remove();
  if (pending) await pending;

  // A borrowed ticket expires server side; the stream reports it rather than
  // answering 401, because the response already started.
  if (expired) {
    const auth = await requestTicket();
    if (auth?.ticket && (await connect(auth))) return streamAsk(prompt, typing());
    say("The Alfresco session expired. Open the full workspace to sign in again.");
    mood("stuck", "not signed in");
  }
}

const LABEL = {
  search: "searching",
  explore: "investigating",
  act: "working out the changes",
  undo: "putting it back",
  clarify: "checking",
};

/* One trail that grows, rather than a new row per stage. */
function chip(node, step) {
  if (!node) {
    node = document.createElement("div");
    node.className = "trail";
    $("log").append(node);
  }
  const span = document.createElement("span");
  span.className = "chip done";
  span.title = step.detail || "";
  span.textContent = step.node;
  node.append(span);
  scrollDown();
  return node;
}

function renderWhole(data, dots) {
  showRoute(data.route, data.reason);
  if (data.route === "clarify") {
    dots.remove();
    say(`<h3>Which ones?</h3><p>${escapeHtml(data.message)}</p>`);
    mood("waiting", "needs a pointer");
  } else if (data.route === "undo") {
    dots.remove();
    reportUndo(data.undo);
  } else if (data.route === "search") {
    renderSearch(data.search, dots);
  } else {
    return renderDeep(data.deep, dots);
  }
}

/* The decision is shown, not hidden: a system that routes on the user's
 * behalf has to be able to say what it chose and why. */
function showRoute(route, reason) {
  const badge = ROUTE_BADGE[route] || ROUTE_BADGE.search;
  const node = $("routeBadge");
  node.textContent = badge.label;
  node.title = `${badge.title} \u2014 ${reason}`;
  node.className = `route ${route}`;
  node.hidden = false;
  if (route === "explore") completeQuest("capability");
}

/* Provider errors arrive as a wall of vendor JSON. The only part a user can
 * act on is whether the model ran out of quota, so say that and nothing else. */
function explain(message) {
  const text = String(message || "");
  if (/429|quota|rate_limit|RESOURCE_EXHAUSTED/i.test(text)) {
    // Providers report the reset as "13h51m20.762475371s"; nanoseconds are
    // noise when the answer is "come back tomorrow".
    const wait = text.match(/retry in (?:(\d+)h)?(?:(\d+)m)?/i);
    const parts = wait ? [wait[1] && `${wait[1]}h`, wait[2] && `${wait[2]}m`].filter(Boolean) : [];
    return `<h3>The model is out of quota</h3><p>Every configured model has hit its
      daily limit.${parts.length ? ` It resets in about ${parts.join(" ")}.` : ""}
      Undo still works, because putting changes back needs no model.</p>`;
  }
  return `<h3>That did not work</h3><p>${escapeHtml(text)}</p>`;
}

function renderSearch(data, dots, streamed = null) {
  dots.remove();
  // When the run was streamed the chips are already on screen and the report
  // has been written into `streamed` as it arrived; re-rendering either would
  // show the answer twice.
  if (!streamed) trail(data.steps || []);
  score.probes += (data.steps || []).length;
  verdict(data.cause);

  if (data.aftsQuery) {
    say(`<h3>Query</h3><p><code>${escapeHtml(data.aftsQuery)}</code></p>`);
  }
  for (const doc of (data.results || []).slice(0, 4)) hit(doc);
  if (streamed) {
    // Move the finished report below the hits it describes.
    $("log").append(streamed);
    if (data.report) withCopy(streamed, data.report);
  } else if (data.report) {
    withCopy(say(markdown(data.report)), data.report);
  }

  if (data.cause === "found") {
    score.found += 1;
    mood("found", `found ${data.total}`);
    award("first-find", "\u2691", "First find", "a document located");
    completeQuest("find");
  } else {
    score.solved += 1;
    mood("stuck", "explained why not");
    award("first-explain", "\u2691", "Case closed", "explained a zero-hit search");
    completeQuest("explain");
  }
  bumpScore();
}

/* The agent pauses before every change, so this is a loop: run, ask, resume. */
async function renderDeep(data, dots) {
  dots.remove();

  let round = 0;
  while (data.pending?.length && round < 12) {
    round += 1;
    trail(data.steps || []);
    planPanel(data.todos);
    mood("waiting", `${data.pending.length} change${data.pending.length === 1 ? "" : "s"} need you`);

    const decisions = await collectDecisions(data.pending);
    mood("thinking", "applying\u2026");
    const working = typing();
    data = await api("/api/agent/deep/resume", {
      method: "POST",
      body: JSON.stringify({ thread_id: THREAD, decisions }),
    });
    working.remove();
  }

  trail(data.steps || []);
  planPanel(data.todos);
  score.probes += (data.steps || []).length;
  score.applied = data.changed || score.applied;
  if (data.report) withCopy(say(markdown(data.report)), data.report);
  if (score.applied) award("first-change", "\u2691", "Hands on", "a change applied and audited");
  if (score.applied) completeQuest("act");

  const refused = (data.audit || []).filter((entry) => entry.outcome === "failed").length;
  if (refused) {
    say(banner(`${refused} change${refused === 1 ? " was" : "s were"} refused by Alfresco permissions.`));
  }
  if ((data.audit || []).some((entry) => entry.reversible && !entry.undone && entry.outcome === "ok")) {
    offerUndo(data.audit);
  }
  mood(score.applied ? "found" : "idle", score.applied ? `${score.applied} applied` : "nothing changed");
  bumpScore();
}

/* Approval stops the wrong change before it happens; this is for the one
 * that got through. Only offered while something is actually reversible. */
function offerUndo(audit) {
  const reversible = audit.filter((e) => e.reversible && !e.undone && e.outcome === "ok").length;
  const stuck = audit.filter((e) => !e.reversible && e.outcome === "ok").map((e) => e.action);

  const node = document.createElement("div");
  node.className = "batch undo-offer";
  node.innerHTML = `<span>${reversible} change${reversible === 1 ? "" : "s"} can be put back</span>
    <button type="button" class="none">Undo all</button>`;
  $("log").append(node);
  scrollDown();

  node.querySelector("button").onclick = async () => {
    node.remove();
    mood("thinking", "putting it back\u2026");
    const working = typing();
    try {
      const result = await api("/api/agent/deep/undo", { method: "POST" });
      working.remove();
      reportUndo(result);
    } catch (error) {
      working.remove();
      say(`Undo failed: ${escapeHtml(error.message)}`);
      mood("stuck", "undo failed");
    }
  };

  if (stuck.length) {
    say(banner(`${[...new Set(stuck)].join(", ")} cannot be reversed automatically.`));
  }
}

/* Shared by the button and by simply asking for it, so "undo that" and the
 * offer below a change produce the same result. */
function reportUndo(result) {
  if (!result) return;
  if (!result.reversed && !result.failed?.length && !result.irreversible?.length) {
    say("There is nothing to undo \u2014 I have not changed anything this session.");
    mood("idle", "nothing to undo");
    return;
  }
  say(`<h3>Undone</h3><p>${result.reversed} change${result.reversed === 1 ? "" : "s"} reversed.</p>`
    + (result.failed?.length ? `<p>${result.failed.length} could not be reversed.</p>` : "")
    + (result.irreversible?.length
      ? `<p>No inverse exists for: <code>${escapeHtml(result.irreversible.join(", "))}</code></p>`
      : ""));
  score.applied = Math.max(0, score.applied - result.reversed);
  bumpScore();
  mood("idle", "back to where we started");
  if (result.reversed) award("first-undo", "\u21ba", "Reversed", "the repository put back");
  if (result.reversed) completeQuest("undo");
}

function banner(text) {
  return `<h3>Note</h3><p>${escapeHtml(text)}</p>`;
}

/* Openers, not a permanent menu. Once the conversation has started they are
 * just clutter competing with the transcript for height. */
function renderSuggestions() {
  const rail = $("suggestions");
  const started = !!$("log").querySelector(".msg.user");
  // Drives the full screen's opening layout, which centres on the greeting
  // until there is a transcript worth reading.
  $("log").classList.toggle("empty", !started);
  if (started) {
    rail.innerHTML = "";
    return;
  }
  rail.innerHTML = SUGGESTIONS
    .map((s) => `<button type="button" class="suggestion">${escapeHtml(s)}</button>`)
    .join("");
}

/* ---------------------------------------------------------------- history */
/* Kept in the browser rather than on the server: a transcript belongs to the
 * person who typed it, it survives a container restart this way, and it needs
 * no store to administer. The agent's own memory is per-session, so reopening
 * an old thread restores what was said, not what the model remembers. */
const HISTORY_KEY = "curator.history.v1";
const HISTORY_MAX = 25;

/* The panel used to store under "copilot.*". Renaming the product must not
 * cost a returning user their transcripts or their rank. */
function readStored(key) {
  try {
    const current = localStorage.getItem(key);
    if (current !== null) return current;
    const legacyKey = key.replace(/^curator\./, "copilot.");
    const legacy = localStorage.getItem(legacyKey);
    if (legacy !== null) {
      localStorage.setItem(key, legacy);
      localStorage.removeItem(legacyKey);
    }
    return legacy;
  } catch {
    return null;
  }
}

function readHistory() {
  try {
    return JSON.parse(readStored(HISTORY_KEY) || "[]");
  } catch {
    return [];
  }
}

function writeHistory(entries) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(entries.slice(0, HISTORY_MAX)));
  } catch {
    // A full quota must not take the conversation down with it.
  }
}

function saveConversation() {
  const first = $("log").querySelector(".msg.user");
  if (!first) return;
  const entries = readHistory().filter((e) => e.id !== THREAD);
  entries.unshift({
    id: THREAD,
    title: first.textContent.trim().slice(0, 80),
    at: Date.now(),
    html: $("log").innerHTML,
  });
  writeHistory(entries);
  renderHistory();
}

function renderHistory() {
  const entries = readHistory();
  const list = $("historyList");
  if (!entries.length) {
    list.innerHTML = `<div class="rail-empty">Nothing yet. Ask something.</div>`;
    return;
  }
  list.innerHTML = entries
    .map((e) => `<button type="button" class="rail-item${e.id === THREAD ? " current" : ""}" data-id="${e.id}">
      ${escapeHtml(e.title)}<span class="when">${when(e.at)}</span></button>`)
    .join("");
}

function when(at) {
  const minutes = Math.round((Date.now() - at) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return new Date(at).toLocaleDateString();
}

function openConversation(id) {
  const entry = readHistory().find((e) => e.id === id);
  if (!entry || busy) return;
  THREAD = entry.id;
  $("log").innerHTML = entry.html;
  $("log").classList.remove("empty");
  $("routeBadge").hidden = true;
  // Controls from a past turn have nothing live behind them.
  $("log").querySelectorAll(".copy, .approval .buttons, .batch").forEach((n) => n.remove());
  renderSuggestions();
  renderHistory();
  closeRail();
  scrollDown();
}

/* ----------------------------------------------------------------- quests */
/* Each one names a capability a first-time user would not think to ask for,
 * and clicking it asks. The list is the feature tour. */
const QUESTS = [
  { id: "find", label: "Find a document", prompt: "unpaid invoices" },
  { id: "explain", label: "Explain a missing one", prompt: "why can't I find the penetration test report" },
  { id: "capability", label: "See what it can do", prompt: "what can you do to this repository?" },
  { id: "act", label: "Change something", prompt: "create a folder 'Q4 Reports' in Shared" },
  { id: "undo", label: "Put a change back", prompt: "undo that" },
];

const questsDone = new Set();

/* Progression that forgets itself on reload is not progression. Kept beside
 * the transcripts, in the browser, for the same reasons. */
const PROGRESS_KEY = "curator.progress.v1";

function loadProgress() {
  try {
    const saved = JSON.parse(readStored(PROGRESS_KEY) || "{}");
    Object.assign(score, saved.score || {});
    (saved.quests || []).forEach((q) => questsDone.add(q));
    // Levels already earned must not be announced again on every reload.
    level = saved.level || 1;
  } catch {
    // A corrupt entry starts the user over, which is survivable.
  }
}

function saveProgress() {
  try {
    localStorage.setItem(PROGRESS_KEY, JSON.stringify({
      score, quests: [...questsDone], level,
    }));
  } catch {
    // Never let a full quota break a turn.
  }
}

function renderQuests() {
  $("quests").innerHTML = QUESTS
    .map((q) => `<li class="${questsDone.has(q.id) ? "done" : ""}" data-prompt="${escapeHtml(q.prompt)}"
      title="${questsDone.has(q.id) ? "Done" : "Try it"}">${escapeHtml(q.label)}</li>`)
    .join("");
}

function completeQuest(id) {
  if (questsDone.has(id)) return;
  questsDone.add(id);
  renderQuests();
  saveProgress();
}

/* ------------------------------------------------------------------- rail */
function openRail() { $("rail").classList.add("open"); }
function closeRail() { $("rail").classList.remove("open"); }

$("railBtn").addEventListener("click", () => {
  $("rail").classList.toggle("open");
  renderHistory();
});
$("railClose").addEventListener("click", closeRail);

$("historyList").addEventListener("click", (event) => {
  const item = event.target.closest(".rail-item");
  if (item) openConversation(item.dataset.id);
});

$("quests").addEventListener("click", (event) => {
  const quest = event.target.closest("li");
  if (!quest) return;
  closeRail();
  ask(quest.dataset.prompt);
});

/* ------------------------------------------------------------------- boot */
$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  send();
});

function send() {
  const box = $("prompt");
  const value = box.value;
  box.value = "";
  grow();
  ask(value);
}

/* Enter sends, because that is what a chat does. Shift+Enter is the escape
 * hatch for the multi-line instructions this agent actually gets. */
$("prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});

$("prompt").addEventListener("input", grow);

function grow() {
  const box = $("prompt");
  box.style.height = "auto";
  const wanted = box.scrollHeight;
  box.style.height = `${Math.min(wanted, 160)}px`;
  box.style.overflowY = wanted > 160 ? "auto" : "hidden";
}

/* A fresh thread, not a page reload: reloading would also drop the session
 * and the signed-in state along with the transcript. */
$("newBtn").addEventListener("click", () => {
  if (busy) return;
  saveConversation();
  THREAD = `bot-${Math.random().toString(36).slice(2, 10)}`;
  $("log").innerHTML = "";
  $("routeBadge").hidden = true;
  EARNED.clear();
  greet();
  renderSuggestions();
  renderHistory();
  closeRail();
});

$("suggestions").addEventListener("click", (event) => {
  const button = event.target.closest(".suggestion");
  if (button) ask(button.textContent);
});

// Share has a Curator page of its own and says so, in which case expanding is
// a request for the host to navigate. A host without one is handled here
// instead: relaying the click through postMessage would strip the user
// activation a popup blocker insists on, and the window would never open.
let hostHasPage = false;

$("expandBtn").addEventListener("click", () => {
  if (hostHasPage && FRAMED) {
    window.parent.postMessage({ type: "curator:expand" }, "*");
    return;
  }
  window.open(`${window.location.origin}/`, "curator-full");
});

/* Already signed in? A session established from the docked panel is carried by
 * the same-origin cookie, so opening the full screen in a new tab continues it
 * rather than asking again. */
async function resumeSession() {
  try {
    const state = await api("/api/status", {}, false);
    if (!state.searchingAs) return false;
    SHARE_BASE = state.shareBaseUrl || SHARE_BASE;
    return true;
  } catch {
    return false;
  }
}

/* Full screen has no host to borrow a ticket from, so it asks. The docked
 * panel never shows this: it is handed a ticket by the page around it. */
function signInForm() {
  const node = say(`<h3>Sign in to Alfresco</h3>
    <form id="signin" class="signin">
      <input name="username" placeholder="Username" autocomplete="username" required />
      <input name="password" type="password" placeholder="Password" autocomplete="current-password" required />
      <button type="submit">Sign in</button>
    </form>`);
  mood("waiting", "waiting for you");

  node.querySelector("#signin").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const state = await api("/api/connect", {
        method: "POST",
        body: JSON.stringify({
          username: form.username.value,
          password: form.password.value,
          base_url: "",
          role: "search",
        }),
      }, false);
      SHARE_BASE = state.shareBaseUrl || SHARE_BASE;
      node.remove();
      greet();
      deepLink();
    } catch (error) {
      button.disabled = false;
      say(`That did not sign in: ${escapeHtml(error.message)}`);
    }
  });
}

function greet() {
  mood("idle", "Ask anything about your content");
  say("<strong>Ask me anything about your content.</strong> Find it, learn why it is missing, or change it \u2014 whichever you need, in the same box.");
  renderSuggestions();
}

/* The document library action links here with a node, so the deep link opens
 * as the first question rather than a form the user has to fill in again. */
function deepLink() {
  const params = new URLSearchParams(window.location.search);
  const node = params.get("node");
  const query = params.get("query");
  if (!node && !query) return;
  if (node && query) return ask(`Why does this document not match ${query}? Its node reference is ${node}.`);
  if (node) return ask(`Tell me about the document with node reference ${node}, and whether it is searchable.`);
  return ask(query);
}

(async function start() {
  // Context keeps arriving as the user moves and selects, so this listener
  // outlives the one-shot handshake that collects the ticket.
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    if (event.data?.type === "curator:context") onContext(event.data.context);
    if (event.data?.type === "curator:host") hostHasPage = !!event.data.hasPage;
  });

  if (!FRAMED) document.body.dataset.view = "full";
  loadProgress();
  renderSuggestions();
  renderHistory();
  renderQuests();
  progress();
  if (Object.values(score).some(Boolean)) bumpScore();

  if (await resumeSession()) {
    announceReady(false);
    greet();
    deepLink();
    return;
  }

  const auth = FRAMED ? await requestTicket() : null;
  if (auth?.ticket && (await connect(auth))) {
    announceReady(false);
    greet();
    deepLink();
  } else if (FRAMED) {
    say("Open the full workspace to sign in, then come back.");
    mood("stuck", "not signed in");
  } else {
    signInForm();
  }
})();
