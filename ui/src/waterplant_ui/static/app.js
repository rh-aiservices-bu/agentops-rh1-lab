/* Northgate Water Treatment Works — operator console.
 *
 * Polls the BFF once a second and drives an analogue panel: SVG gauges with
 * needles, recessed LCD readouts, indicator lamps and valve switches.
 *
 * Polling rather than SSE is deliberate — far fewer failure modes through
 * Routes and gateways, and a 1 Hz poll of a small JSON document is nothing at
 * 30 tenants. When the plant goes unreachable the panel says so instead of
 * freezing on stale readings; a console that quietly stops updating is worse
 * than one that admits it, especially in an exercise about whether the thing
 * in front of you is telling the truth.
 */

const POLL_MS = 1000;
const $ = (id) => document.getElementById(id);

/* Auto-scroll only when the reader is already at the foot of the buffer.
 *
 * Without this, a streaming answer drags the view down while someone is
 * scrolled up reading an earlier tool call — which is precisely when they are
 * most likely to be checking what the agent actually did. */
const STICK_SLACK_PX = 56;
const isPinned = (el) =>
  el.scrollHeight - el.scrollTop - el.clientHeight < STICK_SLACK_PX;
const stick = (el, wasPinned) => {
  if (wasPinned) el.scrollTop = el.scrollHeight;
};

let agentAvailable = false;

/* Whether to render the live MCP tool trace.
 *
 * On by default: seeing the calls land is most of the point. But an instructor
 * demonstrating the *conversation* wants the prose alone, and a participant
 * comparing before/after answers does too. Persisted per browser — a
 * convenience, so a failure to read it just means the default. */
let showTrace = true;
try {
  showTrace = localStorage.getItem("wp.showTrace") !== "0";
} catch {
  /* private window or blocked storage: keep the default */
}

/* Terminal phosphor. "off" is not a third colour — it is the accessibility
 * setting: standard system faces, normal case and tracking, comfortable size,
 * and no CRT simulation at all. For anyone who finds the vintage treatment
 * hard to read, or who is projecting it to a room. Persisted per browser,
 * best-effort. */
const PHOSPHORS = ["amber", "green", "off"];
const PHOSPHOR_LABEL = { amber: "Amber", green: "Green", off: "Readable" };
let phosphor = "amber";
try {
  const saved = localStorage.getItem("wp.phosphor");
  if (PHOSPHORS.includes(saved)) phosphor = saved;
} catch {
  /* keep the default */
}

function applyPhosphor() {
  // Amber is the stylesheet default, so it carries no attribute.
  if (phosphor === "amber") delete document.documentElement.dataset.phosphor;
  else document.documentElement.dataset.phosphor = phosphor;
  const label = $("phosphor-label");
  if (label) label.textContent = PHOSPHOR_LABEL[phosphor];
}

const num = (v, d = 1) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));

function statusOf(checks, subject, metric) {
  const c = checks.find((x) => x.subject === subject && x.metric === metric);
  return c ? c.status : "ok";
}
function worstFor(checks, subject) {
  const rel = checks.filter((c) => c.subject === subject);
  if (rel.some((c) => c.status === "critical")) return "critical";
  if (rel.some((c) => c.status === "warn")) return "warn";
  return "ok";
}

/* ── analogue gauge ───────────────────────────────────────
 * 270° sweep from 135° to 405°. Drawn as SVG so the needle is a real rotation
 * rather than a sprite, and so the dial scales cleanly on any panel size.
 */
const A0 = 135;
const SWEEP = 270;

function polar(cx, cy, r, deg) {
  const rad = ((deg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}

function arcPath(cx, cy, r, from, to) {
  const [x1, y1] = polar(cx, cy, r, from);
  const [x2, y2] = polar(cx, cy, r, to);
  const large = to - from > 180 ? 1 : 0;
  return `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
}

let gaugeSeq = 0;

function gauge({ value, min, max, label, unit, dangerFrom, ticks = 5 }) {
  // Unique gradient ids per gauge. Repeating <defs> ids across many inline SVGs
  // makes every gauge resolve to the first one's gradients — harmless while
  // they are identical, and a confusing bug the moment one differs.
  const uid = `g${++gaugeSeq}`;
  const v = Math.max(min, Math.min(max, Number(value) || 0));
  const frac = (v - min) / (max - min || 1);
  const angle = A0 + frac * SWEEP;

  const tickMarks = [];
  for (let i = 0; i <= ticks; i++) {
    const t = i / ticks;
    const deg = A0 + t * SWEEP;
    const [ax, ay] = polar(50, 50, 33, deg);
    const [bx, by] = polar(50, 50, 39, deg);
    const [lx, ly] = polar(50, 50, 26, deg);
    const val = min + t * (max - min);
    const text = Math.abs(val) >= 100 ? val.toFixed(0) : val.toFixed(0);
    tickMarks.push(
      `<line x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}" stroke="#4a4233" stroke-width="1.3"/>` +
        `<text x="${lx}" y="${ly}" font-size="6.5" fill="#5c5340" text-anchor="middle" dominant-baseline="middle">${text}</text>`
    );
  }

  const danger =
    dangerFrom === undefined
      ? ""
      : `<path d="${arcPath(50, 50, 36, A0 + ((dangerFrom - min) / (max - min)) * SWEEP, A0 + SWEEP)}"
             stroke="#b3392b" stroke-width="4" fill="none" stroke-linecap="butt" opacity=".85"/>`;

  return `
  <svg class="gauge" viewBox="0 0 100 100" role="img" aria-label="${label} ${num(v, 2)} ${unit || ""}">
    <defs>
      <radialGradient id="${uid}-face" cx="38%" cy="30%">
        <stop offset="0%" stop-color="#f0e6cc"/>
        <stop offset="62%" stop-color="#ded2b4"/>
        <stop offset="100%" stop-color="#b9ab8b"/>
      </radialGradient>
      <linearGradient id="${uid}-bezel" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#9aa3a8"/>
        <stop offset="38%" stop-color="#4d585e"/>
        <stop offset="100%" stop-color="#1b2427"/>
      </linearGradient>
      <radialGradient id="${uid}-glass" cx="34%" cy="24%">
        <stop offset="0%" stop-color="#ffffff" stop-opacity=".38"/>
        <stop offset="55%" stop-color="#ffffff" stop-opacity=".05"/>
        <stop offset="100%" stop-color="#000000" stop-opacity=".18"/>
      </radialGradient>
    </defs>
    <circle cx="50" cy="50" r="48" fill="url(#${uid}-bezel)"/>
    <circle cx="50" cy="50" r="43" fill="url(#${uid}-face)"/>
    ${danger}
    ${tickMarks.join("")}
    <text x="50" y="63" font-size="7" fill="#5c5340" text-anchor="middle"
          letter-spacing=".6" font-family="Oswald, sans-serif">${label}</text>
    <text x="50" y="72" font-size="5.6" fill="#7a6f57" text-anchor="middle">${unit || ""}</text>
    <g transform="rotate(${angle} 50 50)">
      <polygon points="50,14 47.6,50 52.4,50" fill="#1a1a18"/>
    </g>
    <circle cx="50" cy="50" r="4.6" fill="#2b2b28" stroke="#8c8674" stroke-width=".8"/>
    <circle cx="50" cy="50" r="43" fill="url(#${uid}-glass)" pointer-events="none"/>
  </svg>`;
}

function lcd(value, unit, status = "ok") {
  const cls = status === "ok" ? "" : ` ${status}`;
  return `<span class="lcd${cls}">${value}${unit ? `<span class="u">${unit}</span>` : ""}</span>`;
}

/* ── rendering ───────────────────────────────────────────── */
function renderStatus(safety) {
  const box = $("status-box");
  box.className = `status-box ${safety.status}`;
  $("status-v").textContent = safety.status;

  $("lamp-live").className = "lamp on green";
  const alarm = $("lamp-alarm");
  alarm.className =
    safety.status === "critical"
      ? "lamp on red"
      : safety.status === "warn"
      ? "lamp on amber"
      : "lamp";
}

/* Reservoir level, and turbidity beside it.
 *
 * The inflow/outflow/capacity readouts that used to sit here are gone. Two of
 * them were percent-of-reservoir-per-second, a unit nothing in the lab refers
 * to, and capacity was the constant 5,000 m3 — a third of a panel spent
 * restating a number that cannot change. The "% of capacity" caption under
 * the needle already says the useful thing.
 *
 * Turbidity survived the same cull that removed the pH and chlorine gauges,
 * and is the only water-quality reading left, because it is the only one that
 * is actually checked: it climbs when the reservoir is drawn down or the
 * emergency bypass dumps unfiltered water through, which makes it the visible
 * consequence of the destructive attacks. pH never moved off 7.2 and both it
 * and chlorine rendered a hardcoded "ok" no matter what the plant was doing. */
function renderReservoir(r, q, checks) {
  $("g-reservoir").innerHTML =
    gauge({ value: r.level_pct, min: 0, max: 100, label: "Reservoir", unit: "%", ticks: 4 }) +
    `<span class="gauge-cap">${num(r.level_pct, 1)} % of capacity</span>`;

  const turb = statusOf(checks, "water_quality", "turbidity");
  $("turbidity-aside").innerHTML =
    `<span class="k engraved">Turbidity</span>${lcd(num(q.turbidity_ntu, 3), "NTU", turb)}`;
}

/* One compact row per pump, not a card with two big dials.
 *
 * Four 258px cards needed a full-width row of their own, which is a large
 * part of why the console used to be two viewports tall. Side by side in the
 * narrower left column they got ~160px each, too little for two dials, and
 * the fourth wrapped out of its panel.
 *
 * Both dials are gone rather than shrunk. Four rows in the space available
 * are ~47px tall and the smallest legible dial is ~54px, so a dial here is a
 * clipped dial — worse than none. Speed becomes a bar, which says "derated to
 * 70%" at a glance without being read at all; vibration joins discharge and
 * bearing as an LCD, taking its colour from the same safety check that used
 * to drive the dial's red arc. The reservoir keeps its needle, so the console
 * does not lose the instrument it is built around.
 *
 * Vibration is listed first of the three: it is the reading the golden
 * workflow turns on and the figure module 1 quotes. */
function renderPumps(pumps, checks) {
  $("pumps").innerHTML = Object.keys(pumps)
    .sort((a, b) => a - b)
    .map((id) => {
      const p = pumps[id];
      const subject = `pump-${p.id}`;
      const worst = p.running ? worstFor(checks, subject) : "stopped";
      const lampCls =
        !p.running ? "lamp" : worst === "critical" ? "lamp on red" : worst === "warn" ? "lamp on amber" : "lamp on green";

      const headStatus = statusOf(checks, subject, "head_deficit");
      const tempStatus = statusOf(checks, subject, "bearing_temp");
      const speedStatus = statusOf(checks, subject, "vibration");

      // A stopped pump turns at zero, but the drive keeps its speed reference
      // and resumes from it. Showing that is the difference between "this pump
      // is off" and "this pump is off and will come back at 60%".
      const speedShown = p.running ? p.speed_pct : p.speed_setpoint_pct;

      return `
      <div class="pump plate ${p.running ? "" : "idle"}">
        <div class="pump-id">
          <span class="${lampCls}"></span>
          <span class="nm engraved">Pump ${p.id}</span>
          <span class="chip ${worst}">${worst}</span>
        </div>
        <div class="pump-speed">
          <span class="k engraved">${p.running ? "Speed" : "Setpoint"}</span>
          <span class="bar ${speedStatus}"><span style="width:${Math.max(0, Math.min(100, speedShown))}%"></span></span>
          <span class="pc">${num(speedShown, 0)}%</span>
        </div>
        <div class="pump-lcds">
          <div class="cell"><span class="k engraved">Vibration</span>${lcd(num(p.vibration_mm_s, 2), "mm/s", speedStatus)}</div>
          <div class="cell"><span class="k engraved">Discharge</span>${lcd(num(p.discharge_bar, 2), "bar", headStatus)}</div>
          <div class="cell"><span class="k engraved">Bearing</span>${lcd(num(p.bearing_temp_c, 1), "°C", tempStatus)}</div>
        </div>
      </div>`;
    })
    .join("");
}

function renderValves(valves) {
  $("valves").innerHTML = ["intake", "discharge", "emergency_bypass"]
    .map((name) => {
      const v = valves[name];
      if (!v) return "";
      const danger = name === "emergency_bypass" && v.open;
      const lampCls = v.open ? (danger ? "lamp on red" : "lamp on green") : "lamp";
      return `
      <div class="valve ${danger ? "danger" : ""}">
        <span class="nm engraved">${name.replace(/_/g, " ")}</span>
        <span class="toggle ${v.open ? "on" : "off"}">
          <span class="body"><span class="lever"></span></span>
          <span class="state">${v.open ? "open" : "shut"}</span>
        </span>
        <span class="${lampCls}"></span>
        ${lcd(v.open ? `OPEN ${num(v.position_pct, 0)}%` : "CLOSED", "", danger ? "critical" : "ok")}
      </div>`;
    })
    .join("");
}

function renderChecks(safety) {
  const breaches = safety.checks.filter((c) => c.status !== "ok");
  if (!breaches.length) {
    $("checks").innerHTML = `
      <div class="check ok">
        <span class="lamp on green"></span>
        <span class="sub engraved">All readings within limits</span>
        <span class="val">OK</span>
      </div>`;
    return;
  }
  $("checks").innerHTML = breaches
    .map(
      (c) => `
      <div class="check ${c.status}">
        <span class="lamp on ${c.status === "critical" ? "red" : "amber"}"></span>
        <span class="sub engraved">${c.subject} · ${c.metric}
          ${c.note ? `<span class="note">${c.note}</span>` : ""}
        </span>
        <span class="val">${num(c.value, 2)}<span class="u">${c.unit || ""}</span></span>
      </div>`
    )
    .join("");
}

/* ── polling ─────────────────────────────────────────────── */
/* The attack/eval event cursor.
 *
 * -1 means "I have just loaded": the server answers with the current sequence
 * number and no backlog, so a reload does not replay an hour of old attacks as
 * live toasts. From then on we send the highest sequence we have seen and get
 * everything after it — which is the entire notification mechanism. No
 * websocket, no SSE, no push channel: a dropped poll costs nothing because the
 * next one asks from the same cursor again. */
let eventCursor = -1;

async function poll() {
  try {
    const res = await fetch(`/api/state?eventsSince=${eventCursor}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { plant, safety, events, eventCursor: cursor, probesBusy } = await res.json();

    /* An emergency shutdown stops the intake controller, so the reservoir
     * drains from here on. It is the one plant state that gets worse the
     * longer it goes unnoticed, and a red row in the safety list is easy to
     * miss on a panel that is already amber. The whole console goes red. */
    document.body.classList.toggle("plant-shutdown", !!plant.shutdown_active);
    $("mast-alarm").hidden = !plant.shutdown_active;

    renderStatus(safety);
    renderReservoir(plant.reservoir, plant.quality, safety.checks);
    renderPumps(plant.pumps, safety.checks);
    renderValves(plant.valves);
    renderChecks(safety);

    if (typeof cursor === "number") eventCursor = cursor;
    (events || []).forEach(handleProbeEvent);
    setProbeButtons(!probesBusy);

    /* Nothing to say while the plant is answering. This used to read
     * "live · tick 3927", which counted simulator ticks — a number with no
     * meaning to a participant, sitting in the most prominent row on the
     * page. The live lamp beside it already says the poll is landing.
     *
     * The element stays, because it is where the opposite case is reported:
     * when plant-api stops answering, the catch below puts the reason here.
     */
    $("conn").textContent = "";
    $("conn").className = "tick";
  } catch (err) {
    $("conn").textContent = `plant unreachable — ${err.message}`;
    $("conn").className = "tick down";
    $("lamp-live").className = "lamp";
    $("lamp-alarm").className = "lamp on red";
    $("status-box").className = "status-box unknown";
    $("status-v").textContent = "—";
  }
}

/* ── attack and evaluation notifications ──────────────────
 *
 * Three states, and they must stay visually distinct or the console lies
 * about the one thing it exists to tell the participant:
 *
 *   blocked      the attack was stopped. You are defended against this.
 *   got_through  it worked. You are not.
 *   inconclusive the engine could not judge it — agent unreachable, a probe
 *                error, or a case whose observer does not exist yet. NOT a
 *                pass, and rendering it as one would be the single most
 *                dangerous thing this panel could do.
 */

/* Live toasts by case id, so a `resolved` event updates the toast its
 * `started` event raised rather than stacking a second one beside it. */
/* Chapter names for the attack picker's groups.
 *
 * Titles rather than "module-03", because the point of grouping is to match
 * what the participant has open. Keyed by the definitions' maps_to_module;
 * an unmapped or unknown module falls back to the raw key rather than
 * vanishing from the list. */
const MODULE_NAMES = {
  "module-01": "Module 1 — Introduction & Meet Hermie",
  "module-02": "Module 2 — Sandbox the Agent",
  "module-03": "Module 3 — Let the Agent Use Its Tools",
  "module-04": "Module 4 — Let the Agent Write Files",
  "module-05": "Module 5 — Let the Agent Reach the Docs Website",
  "module-06": "Module 6 — Guard Against a Compromised Account",
};

const liveToasts = new Map();

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const OUTCOME = {
  blocked: { cls: "ok", mark: "✓", word: "Blocked" },
  /* Its own colour, between blocked and got-through. The attack did not land,
   * but nothing stopped it — the agent simply chose not to comply, and a
   * model's judgement is not a control. Showing this as green "Blocked" would
   * tell a participant who has hardened nothing that they are defended. */
  declined: { cls: "weak", mark: "~", word: "Declined, not blocked" },
  got_through: { cls: "bad", mark: "✗", word: "Got through" },
  inconclusive: { cls: "unknown", mark: "?", word: "Inconclusive" },
  passed: { cls: "ok", mark: "✓", word: "Passed" },
  failed: { cls: "bad", mark: "✗", word: "Failed" },
};

/* A toast is a shell — a dismiss button and a body — built once. The render
 * functions rewrite only the body, so the close button keeps its listener
 * across the started -> resolved transition instead of being re-bound on
 * every repaint.
 *
 * Nothing expires on a timer. An attack that lands while someone is reading
 * the transcript, or an eval scorecard that arrives while they are looking at
 * the plant, is exactly the result they most need to see — and a result that
 * removes itself after a few seconds is one they will miss. They go when
 * dismissed and not before. */
function toastEl(key) {
  let el = liveToasts.get(key);
  if (el && el.isConnected) return el;
  el = document.createElement("div");
  el.className = "toast";
  el.innerHTML =
    '<button type="button" class="t-close" aria-label="Dismiss notification" ' +
    'title="Dismiss">&times;</button><div class="t-body"></div>';

  el.querySelector(".t-close").addEventListener("click", (e) => {
    e.stopPropagation();
    dismissToast(el);
  });

  $("toasts").prepend(el);
  liveToasts.set(key, el);
  return el;
}

/* Where the render functions write. */
const toastBody = (el) => el.querySelector(".t-body");

function dismissToast(el) {
  el.classList.add("leaving");
  setTimeout(() => {
    el.remove();
    for (const [k, v] of liveToasts) if (v === el) liveToasts.delete(k);
  }, 300);
}

/* Append a trace link to the attack's reply in the transcript.
 *
 * Typed turns end with "see the trace →"; an attack's reply did not, which
 * made the one turn a participant most wants to inspect the only one without
 * a way in. The line is found by the session it was rendered with rather than
 * by position, so a reply that has since scrolled up still gets its link. */
function attachTraceToTranscript(session, url) {
  if (!session) return;
  const line = [...$("chat").querySelectorAll(".line.agent")]
    .reverse()
    .find((l) => l.dataset.session === session);
  if (!line || line.querySelector(".trace-link")) return;

  const note = document.createElement("span");
  note.className = url ? "trace-link" : "trace-link none";
  if (url) {
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "see the trace →";
    note.appendChild(a);
  } else {
    note.textContent = "no trace was collected for this turn";
  }
  line.appendChild(note);
  stick($("chat"), isPinned($("chat")));
}

function handleProbeEvent(ev) {
  /* An attack's turns, rendered exactly as a typed one would be.
   *
   * The participant has to be able to see what was said to the agent and what
   * it answered — a toast saying "got through" without the exchange behind it
   * asks them to take the verdict on trust.
   *
   * Adopting ev.session is the half that keeps it honest. The attack runs in
   * its own Hermes conversation, so without this the transcript would show an
   * exchange the agent has no memory of and "why did you do that?" would be
   * answered by a different session. Taking the id over means the console
   * continues the conversation the attack actually had.
   *
   * remember() too, for the stateless native agent, which is replayed the
   * on-screen transcript rather than keeping a session of its own. */
  if (ev.kind === "turn") {
    if (ev.session && ev.session !== conversationId) conversationId = ev.session;
    addMessage(ev.role, ev.text, ev.persona);
    if (ev.role !== "system") remember(ev.role, ev.text);
    /* The same waiting indicator a typed turn gets. An attack's prompt lands
     * in the transcript and then nothing moves for however long the agent
     * takes — which, for an attack that drives a tool chain, is the longest
     * wait in the console and the one most likely to read as a hang. It was
     * only wired into the chat form, so the path a participant watches most
     * closely was the one path without it. */
    if (ev.role === "user") {
      startPending();
    } else {
      stopPending();
      /* Tagged with its session so the trace event can find this line again
       * and append the link, the same way a typed turn ends with one. */
      if (ev.role === "assistant" && ev.session) {
        const last = $("chat").lastElementChild;
        if (last) last.dataset.session = ev.session;
      }
    }
    return;
  }
  /* The trace link, arriving after the verdict.
   *
   * MLflow's exporter is asynchronous, so an attack is usually over before
   * its trace is searchable. The verdict no longer waits for it — this fills
   * in both places it belongs: the notification, and the transcript line, so
   * an attack reads exactly like a typed turn. */
  if (ev.kind === "trace") {
    const el = liveToasts.get(`attack:${ev.id}`);
    const slot = el?.querySelector(".t-tracewrap");
    if (slot) {
      slot.innerHTML = ev.traceUrl
        ? `<a class="t-trace" href="${esc(ev.traceUrl)}" target="_blank" rel="noopener">see the trace →</a>`
        : `<span class="t-trace none">no trace was collected</span>`;
    }
    attachTraceToTranscript(ev.session, ev.traceUrl);
    return;
  }
  if (ev.kind === "plant") {
    /* Clear the transcript with the plant. After a reset the conversation
     * above refers to pumps, work orders and readings that no longer exist,
     * and the agent's own session is gone with it — so leaving it on screen
     * invites reading a stale answer as a current one. Covers all three
     * paths, because they all emit this event: the button, before an attack,
     * and before an eval sweep. */
    if (ev.id === "plant-reset") clearTranscript(ev.detail || "");
    return toastNotice(
      "plant-reset", "ok", ev.title || "Plant reset", ev.detail || ""
    );
  }
  if (ev.kind === "evals") return handleEvalEvent(ev);
  if (ev.kind === "eval") return; // per-eval detail lands in the sweep card
  if (ev.kind !== "attack") return;

  const el = toastEl(`attack:${ev.id}`);
  const body = toastBody(el);

  if (ev.phase === "started") {
    el.className = "toast running";
    body.innerHTML = `
      <div class="t-hd"><span class="t-mark spin">◴</span>
        <span class="t-kind">Attack in progress</span>
        <span class="t-sev ${esc(ev.severity)}">${esc(ev.severity)}</span></div>
      <div class="t-title">${esc(ev.title)}</div>
      <div class="t-blurb">${esc(ev.blurb)}</div>`;
    return;
  }

  /* Whatever happened, the agent no longer has the floor. Covers the runs
   * that resolve without an assistant turn at all — an unreachable agent, or
   * an attack skipped before it was sent. */
  stopPending();

  const o = OUTCOME[ev.outcome] || OUTCOME.inconclusive;
  const cleanup = ev.cleanup || {};
  /* Cleanup is shown whenever it was attempted, and loudly when it failed:
   * an attack that got through and could not be undone has left the plant in
   * a state the participant needs to know about. */
  /* "the agent put it back when asked" and "the lab reached past the agent"
   * are different events, and only the first says anything about the agent —
   * so the path is named rather than folded into the detail text. */
  const via =
    cleanup.via === "agent" ? "Cleaned up by the agent"
    : cleanup.via === "direct" ? "Cleaned up directly"
    : "Cleaned up";
  const cleanupLine = cleanup.attempted
    ? `<div class="t-cleanup ${cleanup.succeeded ? "ok" : "bad"}">
         ${cleanup.succeeded ? via : "Cleanup failed"} — ${esc(cleanup.detail)}
       </div>`
    : "";

  /* The attack's own trace. Every typed turn in the chat pane ends with this
   * link; an attack is the turn a participant most wants to open, and until
   * now it was the one turn that did not offer it. Absent when MLflow is not
   * configured, or when the exporter had not indexed it before the lookup
   * gave up — said plainly rather than shown as a dead link. */
  /* A slot rather than a value: the link is still being looked up. Says
   * which of the two it is, so an absent link is never mistaken for a slow
   * one. */
  const traceLine = ev.tracePending
    ? `<span class="t-tracewrap"><span class="t-trace waiting">looking for the trace…</span></span>`
    : `<span class="t-tracewrap"><span class="t-trace none">no trace was collected</span></span>`;

  el.className = `toast ${o.cls}`;
  body.innerHTML = `
    <div class="t-hd"><span class="t-mark">${o.mark}</span>
      <span class="t-kind">${o.word}</span>
      <span class="t-sev ${esc(ev.severity)}">${esc(ev.severity)}</span></div>
    <div class="t-title">${esc(ev.title)}</div>
    <div class="t-blurb">${esc(ev.blurb)}</div>
    ${ev.detail ? `<div class="t-detail">${esc(ev.detail)}</div>` : ""}
    ${cleanupLine}
    <div class="t-actions">${traceLine}
      <button type="button" class="t-reset">Reset plant</button>
    </div>`;

  /* Offered, not done automatically. Running an attack used to be able to
   * reset the plant on the participant's behalf, which discards every work
   * order and maintenance record they had raised — a thing to be asked for,
   * not a side effect. The button is right where they are already looking. */
  body.querySelector(".t-reset").addEventListener("click", (e) => {
    e.stopPropagation();
    resetPlant();
  });
}

function handleEvalEvent(ev) {
  const el = toastEl("evals");
  const body = toastBody(el);

  if (ev.phase === "started") {
    el.className = "toast running wide";
    body.innerHTML = `
      <div class="t-hd"><span class="t-mark spin">◴</span>
        <span class="t-kind">Running evaluations</span></div>
      <div class="t-blurb">${esc(ev.total)} evals against the agent — this takes a minute.</div>`;
    return;
  }

  const all = ev.passed === ev.total && ev.total > 0;
  const rows = (ev.results || [])
    .map((r) => {
      const o = OUTCOME[r.outcome] || OUTCOME.inconclusive;
      /* Each eval links to its own trace. MLflow's Evaluations tab on the run
       * cannot show these — see probes/mlflow_run.py — so the scorecard is
       * where the connection is made. */
      const link = r.traceUrl
        ? ` <a class="t-trace" href="${esc(r.traceUrl)}" target="_blank" rel="noopener">trace →</a>`
        : "";
      return `<li class="${o.cls}"><span class="m">${o.mark}</span>
        <span class="n">${esc(r.title)}${link}</span>
        <span class="d">${esc(r.detail)}</span></li>`;
    })
    .join("");

  el.className = `toast wide ${all ? "ok" : "bad"}`;
  body.innerHTML = `
    <div class="t-hd"><span class="t-mark">${all ? "✓" : "✗"}</span>
      <span class="t-kind">Evaluation suite</span></div>
    <div class="t-score">${ev.passed} <span class="of">/ ${ev.total}</span>
      <span class="w">evals passed</span></div>
    ${ev.inconclusive ? `<div class="t-detail">${ev.inconclusive} could not be judged — not counted as failures.</div>` : ""}
    <ul class="t-list">${rows}</ul>
    ${
      ev.mlflowRunUrl
        ? `<a class="t-link" href="${esc(ev.mlflowRunUrl)}" target="_blank" rel="noopener">Open the MLflow run →</a>`
        : `<div class="t-detail">No MLflow run recorded — tracking is not configured here.</div>`
    }`;
}

function setProbeButtons(enabled) {
  for (const id of ["run-attack", "run-evals", "reset-plant", "attack-choice"]) {
    const b = $(id);
    if (b) b.disabled = !enabled;
  }
}

function toastNotice(key, cls, title, detail) {
  const el = toastEl(key);
  const body = toastBody(el);
  el.className = `toast ${cls}`;
  body.innerHTML = `<div class="t-hd"><span class="t-mark">!</span>
      <span class="t-kind">${esc(title)}</span></div>
    <div class="t-blurb">${esc(detail)}</div>`;
}

async function resetPlant() {
  /* Confirmed, because it is not only the plant that goes back: /reset
   * reseeds the maintenance store too, so any work order raised since the
   * last reset is gone. Destroying someone's work on a single stray click
   * is not a thing a control panel should do. */
  const ok = window.confirm(
    "Reset the plant to its seeded starting state?\n\n" +
      "This also discards every work order and maintenance record raised " +
      "since the last reset."
  );
  if (!ok) return;

  setProbeButtons(false);
  try {
    const res = await fetch("/api/plant/reset", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      toastNotice("plant-reset", "unknown", "Reset failed", body.detail || `HTTP ${res.status}`);
    }
    /* On success the server emits the event and the 1 Hz poll renders it,
     * the same path a scheduled attack takes. */
  } catch (err) {
    toastNotice("plant-reset", "unknown", "Reset failed", err.message);
  } finally {
    setProbeButtons(true);
  }
}

async function releaseShutdown() {
  /* Not confirmed, unlike Reset: this changes one latch and nothing else —
   * no work orders, no pump state — and the plant is actively draining while
   * the dialog would be open. */
  const btn = $("release-shutdown");
  btn.disabled = true;
  try {
    const res = await fetch("/api/plant/release-shutdown", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      toastNotice(
        "shutdown-release", "unknown", "Could not release the shutdown",
        body.detail || `HTTP ${res.status}`
      );
    }
    /* On success the server emits the event and the poll renders it, the
     * same path every other plant change takes. */
  } catch (err) {
    toastNotice("shutdown-release", "unknown", "Could not release the shutdown", err.message);
  } finally {
    btn.disabled = false;
  }
}

async function runAttack() {
  setProbeButtons(false);
  try {
    const res = await fetch("/api/attacks/run", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ id: $("attack-choice").value || null }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      toastNotice("probe-error", "unknown", "Attack not started", body.detail || `HTTP ${res.status}`);
      setProbeButtons(true);
    }
    /* On success we say nothing here. The `started` event is already on its
     * way through the 1 Hz poll and will raise the real toast — announcing it
     * twice, once locally and once from the server, is how the button and the
     * scheduler drift apart. */
  } catch (err) {
    toastNotice("probe-error", "unknown", "Attack not started", err.message);
    setProbeButtons(true);
  }
}

async function runEvals() {
  setProbeButtons(false);
  try {
    const res = await fetch("/api/evals/run", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      toastNotice("probe-error", "unknown", "Evals not started", body.detail || `HTTP ${res.status}`);
      return;
    }
    /* The sweep is synchronous, so the scorecard is in this response. It also
     * arrives as an event; handleEvalEvent is keyed on "evals" so whichever
     * lands first renders and the other updates the same card in place. */
    handleEvalEvent({ ...body, phase: "resolved" });
  } catch (err) {
    toastNotice("probe-error", "unknown", "Evals failed", err.message);
  } finally {
    setProbeButtons(true);
  }
}

/* ── assistant ───────────────────────────────────────────── */
function personaSlug() {
  return $("persona").value;
}

/* Terminal lines rather than chat bubbles: on a phosphor screen, a bubble
   would look like a web page pretending to be a terminal.

   Nothing is parked at the foot of the buffer after a write. A block cursor
   used to be, and a blinking block under a finished answer reads as "still
   thinking" — the one thing a transcript should never be ambiguous about.
   The composer below already shows where input goes. */
function addMessage(role, text, persona) {
  const chat = $("chat");
  const pinned = isPinned(chat);
  chat.querySelector(".cursor-line")?.remove();

  const line = document.createElement("p");
  /* "assistant" is the role the model API uses and what the turn events
   * carry; "agent" is what this transcript has always called it in CSS and
   * what the typed-chat path writes. Normalised here so both paths produce
   * the same element.
   *
   * They did not. An echoed attack reply came out as `.line.assistant`,
   * which is styled nowhere — so its tag missed the dim phosphor colour —
   * and the trace lookup, which searches `.line.agent`, never found it. The
   * "see the trace →" line silently failed to attach to exactly the turn a
   * participant most wants to open. */
  line.className = `line ${role === "assistant" ? "agent" : role}`;

  if (role === "user") {
    const p = document.createElement("span");
    p.className = "prompt";
    /* `persona` is passed only for an echoed attack turn, which always asks
       as the operator. Falling back to the picker would label the attack with
       whichever identity the participant happens to have selected — wrong
       about exactly the thing the identity attacks are testing. */
    p.textContent = `${persona || personaSlug()}@northgate ~ % `;
    line.appendChild(p);
  } else if (role === "system") {
    const t = document.createElement("span");
    t.className = "tag";
    t.textContent = "system: ";
    line.appendChild(t);
  } else {
    const t = document.createElement("span");
    t.className = "tag";
    t.textContent = "assistant: ";
    line.appendChild(t);
  }
  line.appendChild(document.createTextNode(text));
  chat.appendChild(line);

  stick(chat, pinned);
}

/* The conversation, as the console remembers it.
 *
 * Without it a follow-up has nothing to follow — "check pump 3" is answered and
 * offers to start it, then "yes" arrives cold and is met with "Hello! How can I
 * assist you today?" Which half of what follows is used depends on the harness
 * behind the BFF, which the console does not need to know.
 *
 * `conversationId` names the conversation for a harness that keeps its own.
 * Hermes does, with every tool call in it, and is sent only the id: replaying
 * the on-screen text to it instead shows the model answers with no tool calls
 * behind them, and it starts answering the same way (see harness.request in
 * the BFF). One per page load, so a reload is a new conversation.
 *
 * `history` is the transcript as shown on screen, for our own agent, which is
 * stateless and has it replayed with each question.
 *
 * Only what was actually said is kept. Trace lines and system errors are the
 * console's own narration and would read to the model as though the operator
 * had typed them. Trimmed to the same bound the agent enforces, so a long
 * session degrades by forgetting its oldest turns rather than by failing.
 */
const MAX_HISTORY_TURNS = 20;
const history = [];
/* Not const: a plant reset starts a new conversation. Keeping the old id
   would have Hermes reload a session whose transcript describes a plant that
   no longer exists, and answer follow-ups from it. */
let conversationId = crypto.randomUUID();

/* Wipe the console back to a clean start, in step with the plant. */
function clearTranscript(reason) {
  const chat = $("chat");
  if (chat) chat.innerHTML = "";
  history.length = 0;
  conversationId = crypto.randomUUID();
  addMessage(
    "system",
    reason
      ? `Plant reset — ${reason} This conversation has been cleared and the agent starts fresh.`
      : "Plant reset. This conversation has been cleared and the agent starts fresh."
  );
}

/* What the console shows while the agent is thinking.
 *
 * A blinking block on its own line, which is what this screen already uses
 * for "the machine has the floor" — and the reason it was taken off the foot
 * of a *finished* answer, where it read as "still going". Here that reading
 * is the correct one.
 *
 * The elapsed seconds are the part a spinner cannot give. A turn here is a
 * multi-step tool chain through a shared model endpoint that answers 429
 * under load, so thirty seconds of silence is normal and two minutes is not;
 * without a number on screen those look identical and the participant's only
 * move is to reload and lose the conversation.
 *
 * Torn down by the first thing the agent produces — a token, a tool call, or
 * an error — so it never overlaps real output.
 */
let pendingTimer = null;

function startPending() {
  stopPending();
  const chat = $("chat");
  const pinned = isPinned(chat);
  chat.querySelector(".cursor-line")?.remove();

  const line = document.createElement("p");
  line.className = "line agent pending";
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = "assistant: ";
  const cursor = document.createElement("span");
  cursor.className = "cursor";
  const wait = document.createElement("span");
  wait.className = "wait";
  line.append(tag, cursor, wait);
  chat.appendChild(line);
  stick(chat, pinned);

  const started = Date.now();
  const tick = () => {
    const s = Math.round((Date.now() - started) / 1000);
    // Silent for the first couple of seconds: a fast answer should not flash
    // a timer on its way past.
    wait.textContent = s >= 2 ? `  thinking… ${s}s` : "";
  };
  tick();
  pendingTimer = setInterval(tick, 1000);
}

function stopPending() {
  if (pendingTimer) {
    clearInterval(pendingTimer);
    pendingTimer = null;
  }
  $("chat")?.querySelector(".line.pending")?.remove();
}

function remember(role, content) {
  const text = (content || "").trim();
  if (!text) return;
  history.push({ role, content: text });
  if (history.length > MAX_HISTORY_TURNS) history.splice(0, history.length - MAX_HISTORY_TURNS);
}

/* Streamed request.
 *
 * Tool calls are rendered the moment they fire rather than summarised at the
 * end. That is the pedagogically important half: a participant sees what the
 * agent DID, live, separately from what it later SAYS it did — and a denial
 * shows the component that refused it.
 */
async function ask(event) {
  event.preventDefault();
  const input = $("prompt");
  const message = input.value.trim();
  if (!message) return;

  addMessage("user", message);
  input.value = "";
  $("send").disabled = true;

  // Snapshot before the new turn joins it, so the question is not also in the
  // history the request replays.
  const priorTurns = history.slice();
  remember("user", message);
  startPending();

  const chat = $("chat");
  let replyLine = null;
  let replyText = "";

  const write = (text) => {
    stopPending();
    const pinned = isPinned(chat);
    if (!replyLine) {
      chat.querySelector(".cursor-line")?.remove();
      replyLine = document.createElement("p");
      replyLine.className = "line agent";
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "assistant: ";
      replyLine.appendChild(tag);
      chat.appendChild(replyLine);
    }
    replyLine.appendChild(document.createTextNode(text));
    replyText += text;
    stick(chat, pinned);
  };

  const trace = (cls, text, link) => {
    // Stopped even when the trace pane is off: a tool call means the agent is
    // no longer thinking, whether or not this participant is watching them.
    stopPending();
    if (!showTrace) return;
    const pinned = isPinned(chat);
    chat.querySelector(".cursor-line")?.remove();
    const el = document.createElement("p");
    el.className = `line trace ${cls}`;
    el.textContent = text;
    // A status line can carry somewhere to go — the harness that cannot show a
    // tool trace here traces it to MLflow instead. Appended as a real anchor so
    // it is clickable; the href is our own config, never model output.
    if (link?.href && link?.text) {
      el.appendChild(document.createTextNode(" "));
      const a = document.createElement("a");
      a.href = link.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = link.text;
      el.appendChild(a);
    }
    chat.appendChild(el);
    stick(chat, pinned);
    replyLine = null; // a tool call ends the current prose run
  };

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        message,
        persona: $("persona").value,
        history: priorTurns,
        conversationId,
      }),
    });

    if (!res.ok || !res.body) {
      const body = await res.json().catch(() => ({}));
      addMessage("system", body.detail || body.error || `Request failed (${res.status})`);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line; a frame can straddle chunks.
      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        if (!frame.startsWith("data:")) continue;

        let ev;
        try {
          ev = JSON.parse(frame.slice(5).trim());
        } catch {
          continue;
        }

        if (ev.type === "token") {
          write(ev.text);
        } else if (ev.type === "status") {
          // An empty message means the link is the whole line.
          const body = ev.message ? `· ${ev.message}` : "·";
          trace(ev.level === "bad" ? "err" : "dim", body, ev.link);
        } else if (ev.type === "tool_call") {
          const args = Object.entries(ev.arguments || {})
            .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
            .join(" ");
          trace("call", `→ ${ev.name}${args ? " " + args : ""}`);
        } else if (ev.type === "tool_result") {
          if (ev.ok) {
            trace("ok", `  ${ev.summary}`);
          } else {
            trace("err", `  ✗ ${ev.deniedBy ? `denied by ${ev.deniedBy}` : "error"} — ${ev.summary}`);
          }
        } else if (ev.type === "error") {
          addMessage("system", ev.detail);
        } else if (ev.type === "done") {
          if (ev.rateLimitRetries) {
            trace("dim", `· ${ev.rateLimitRetries} rate-limit retries absorbed`);
          }
        }
      }
    }
  } catch (err) {
    addMessage("system", `Could not reach the assistant: ${err.message}`);
  } finally {
    // Recorded here rather than on the "done" event so a turn cut short by a
    // rate limit or a dropped stream still leaves behind what was actually
    // said. A partial answer is context; a missing one silently desynchronises
    // the transcript from what the operator can see on screen.
    remember("assistant", replyText);

    stopPending();
    chat.querySelector(".cursor-line")?.remove();
    // Always return to the foot when a turn completes: the answer is finished
    // and the prompt is waiting.
    chat.scrollTop = chat.scrollHeight;

    $("send").disabled = !agentAvailable;
    input.focus();
  }
}

/* ── boot ────────────────────────────────────────────────── */
(async function init() {
  $("composer").addEventListener("submit", ask);

  applyPhosphor();
  $("phosphor").addEventListener("click", () => {
    phosphor = PHOSPHORS[(PHOSPHORS.indexOf(phosphor) + 1) % PHOSPHORS.length];
    applyPhosphor();
    try {
      localStorage.setItem("wp.phosphor", phosphor);
    } catch {
      /* not worth failing the interaction over */
    }
  });

  const toggle = $("trace-toggle");
  toggle.checked = showTrace;
  toggle.addEventListener("change", () => {
    showTrace = toggle.checked;
    try {
      localStorage.setItem("wp.showTrace", showTrace ? "1" : "0");
    } catch {
      /* not worth failing the interaction over */
    }
    // Hide or reveal what is already on screen, so the switch acts on the
    // transcript in front of you rather than only on the next question.
    document.querySelectorAll(".line.trace").forEach((el) => {
      el.hidden = !showTrace;
    });
  });
  const syncSigil = () => { $("sigil").textContent = `${personaSlug()}@northgate ~ %`; };
  $("persona").addEventListener("change", syncSigil);
  syncSigil();

  try {
    const cfg = await (await fetch("/api/config")).json();
    agentAvailable = cfg.agentAvailable;
    if (cfg.namespace) $("ns").textContent = cfg.namespace;
    if (cfg.mlflowUrl) {
      const link = $("mlflow-link");
      link.href = cfg.mlflowUrl;
      link.hidden = false;
    }
  } catch {
    /* config is best-effort; the panel matters more */
  }

  if (!agentAvailable) {
    $("send").disabled = true;
    $("prompt").placeholder = "Assistant not deployed in this environment yet";
    addMessage(
      "system",
      "The maintenance assistant is not deployed here yet. The plant is live " +
        "and the MCP servers are running, so this panel reflects real simulator " +
        "state — including any control action taken against it."
    );
  }

  $("run-attack").addEventListener("click", runAttack);
  $("run-evals").addEventListener("click", runEvals);
  $("reset-plant").addEventListener("click", resetPlant);
  $("release-shutdown").addEventListener("click", releaseShutdown);

  /* Escape clears the lot. With several results stacked up, dismissing them
   * one × at a time is busywork, and the composer is the only other thing on
   * the page that wants the key — so leave it alone while someone is typing. */
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (document.activeElement === $("prompt")) return;
    document.querySelectorAll(".toast").forEach(dismissToast);
  });

  /* The probe catalogue decides whether the buttons do anything at all. A
   * console with no agent, or with the engine switched off, says so on the
   * button rather than letting someone press it and get a 503. */
  try {
    const probes = await (await fetch("/api/probes")).json();
    if (probes.problem) {
      /* No definitions reached the container. That is a deployment fault, not
       * a lab state, and it must not be left looking like "everything is
       * switched off". */
      setProbeButtons(false);
      toastNotice(
        "probe-defs",
        "unknown",
        "No attack definitions loaded",
        probes.problem
      );
    }
    if (!probes.available) {
      setProbeButtons(false);
      // Resetting the plant needs plant-api, not the agent, so it stays live
      // even when there is nothing to probe.
      $("reset-plant").disabled = false;
      const why = agentAvailable
        ? "The attack and evaluation engine is disabled here."
        : "Attacks and evals need a deployed agent to run against.";
      for (const id of ["run-attack", "run-evals"]) $(id).title = why;
    } else {
      const armed = probes.attacks.filter((a) => a.enabled).length;
      const held = probes.attacks.length - armed;

      /* Fill the picker, grouped by the module each attack belongs to.
       *
       * Flat, the seven titles are a wall of similar-sounding sentences and
       * nothing says which one goes with the chapter the participant is on.
       * The grouping comes from the definitions' own maps_to_module, so it
       * cannot drift from the lab content.
       *
       * Held cases are listed but disabled, with the reason on the option — a
       * participant who cannot see that a case never runs would reasonably
       * assume they are defended against it. */
      const sel = $("attack-choice");
      const groups = new Map();
      for (const a of probes.attacks) {
        const key = a.module || "";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(a);
      }
      for (const key of [...groups.keys()].sort()) {
        const parent = key
          ? Object.assign(document.createElement("optgroup"),
                          { label: MODULE_NAMES[key] || key })
          : sel;
        for (const a of groups.get(key)) {
          const opt = document.createElement("option");
          opt.value = a.id;
          opt.textContent = a.enabled ? a.title : `${a.title} — not armed`;
          opt.disabled = !a.enabled;
          if (a.disabledReason) opt.title = a.disabledReason;
          parent.appendChild(opt);
        }
        if (parent !== sel) sel.appendChild(parent);
      }
      /* Say how many are not armed. A participant who cannot see that three of
       * the seven attacks never fire would reasonably conclude they are
       * defended against them. */
      $("run-attack").title =
        `${armed} of ${probes.attacks.length} attacks are armed` +
        (held ? ` — ${held} need infrastructure that does not exist yet` : "") +
        ". Fires the selected attack now, or a random one." +
        (probes.scheduled
          ? ""
          : " Automatic attacks are switched off, so this button is the only way one runs.");
      const live = probes.evals.filter((e) => e.enabled).length;
      $("run-evals").title = `Runs all ${live} armed evals and scores them.`;
    }
  } catch {
    /* the catalogue is a nicety; the buttons still work without it */
  }

  poll();
  setInterval(poll, POLL_MS);
})();
