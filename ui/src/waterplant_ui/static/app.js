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

function renderReservoir(r) {
  $("g-reservoir").innerHTML =
    gauge({ value: r.level_pct, min: 0, max: 100, label: "Reservoir", unit: "%", ticks: 4 }) +
    `<span class="gauge-cap">${num(r.level_pct, 1)} % of capacity</span>`;
  $("inflow").innerHTML = num(r.inflow_pct_s, 3);
  $("outflow").innerHTML = num(r.outflow_pct_s, 3);
  $("capacity").innerHTML = r.capacity_m3.toLocaleString();
}

function renderQuality(q, checks) {
  const turb = statusOf(checks, "water_quality", "turbidity");
  const cells = [
    { label: "pH", value: q.ph, min: 0, max: 14, unit: "", digits: 2, status: "ok", ticks: 7 },
    { label: "Chlorine", value: q.chlorine_mg_l, min: 0, max: 3, unit: "mg/L", digits: 2, status: "ok", ticks: 3 },
    { label: "Turbidity", value: q.turbidity_ntu, min: 0, max: 5, unit: "NTU", digits: 3, status: turb, ticks: 5, dangerFrom: 1 },
  ];
  $("quality").innerHTML = cells
    .map(
      (c) => `
      <div class="cell">
        <div class="gauge-wrap">${gauge(c)}</div>
        ${lcd(num(c.value, c.digits), c.unit, c.status)}
      </div>`
    )
    .join("");
}

function renderPumps(pumps, checks) {
  $("pumps").innerHTML = Object.keys(pumps)
    .sort((a, b) => a - b)
    .map((id) => {
      const p = pumps[id];
      const subject = `pump-${p.id}`;
      const worst = p.running ? worstFor(checks, subject) : "stopped";
      const lampCls =
        !p.running ? "lamp" : worst === "critical" ? "lamp on red" : worst === "warn" ? "lamp on amber" : "lamp on green";

      const vibStatus = statusOf(checks, subject, "vibration");
      const headStatus = statusOf(checks, subject, "head_deficit");
      const tempStatus = statusOf(checks, subject, "bearing_temp");

      return `
      <div class="pump plate">
        <div class="pump-hd">
          <div>
            <span class="nm engraved">Pump ${p.id}</span>
            <span class="model">${p.model}</span>
          </div>
          <div class="right">
            <span class="${lampCls}"></span>
            <span class="chip ${worst}">${worst}</span>
          </div>
        </div>
        <div class="pump-gauges">
          <div class="gauge-wrap">
            ${gauge({ value: p.speed_pct, min: 0, max: 100, label: "Speed", unit: "%", ticks: 4 })}
            ${
              // A stopped pump turns at zero, but the drive keeps its speed
              // reference and resumes from it. Showing that is the difference
              // between "this pump is off" and "this pump is off and will come
              // back at 60%".
              p.running
                ? ""
                : `<span class="gauge-cap">setpoint ${num(p.speed_setpoint_pct, 0)}%</span>`
            }
          </div>
          <div class="gauge-wrap">${gauge({ value: p.vibration_mm_s, min: 0, max: 12, label: "Vibration", unit: "mm/s", ticks: 4, dangerFrom: 4.5 })}</div>
        </div>
        <div class="pump-lcds">
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
async function poll() {
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { plant, safety } = await res.json();

    renderStatus(safety);
    renderReservoir(plant.reservoir);
    renderQuality(plant.quality, safety.checks);
    renderPumps(plant.pumps, safety.checks);
    renderValves(plant.valves);
    renderChecks(safety);

    $("conn").textContent = `live · tick ${plant.tick}`;
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

/* ── assistant ───────────────────────────────────────────── */
function personaSlug() {
  return $("persona").value;
}

/* Terminal lines rather than chat bubbles: on a phosphor screen, a bubble
   would look like a web page pretending to be a terminal. The block cursor is
   re-parked at the foot of the buffer after every write. */
function addMessage(role, text) {
  const chat = $("chat");
  const pinned = isPinned(chat);
  chat.querySelector(".cursor-line")?.remove();

  const line = document.createElement("p");
  line.className = `line ${role}`;

  if (role === "user") {
    const p = document.createElement("span");
    p.className = "prompt";
    p.textContent = `${personaSlug()}@northgate ~ % `;
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

  const cur = document.createElement("p");
  cur.className = "line cursor-line";
  const blk = document.createElement("span");
  blk.className = "cursor";
  cur.appendChild(blk);
  chat.appendChild(cur);

  stick(chat, pinned);
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

  const chat = $("chat");
  let replyLine = null;

  const write = (text) => {
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
    stick(chat, pinned);
  };

  const trace = (cls, text) => {
    if (!showTrace) return;
    const pinned = isPinned(chat);
    chat.querySelector(".cursor-line")?.remove();
    const el = document.createElement("p");
    el.className = `line trace ${cls}`;
    el.textContent = text;
    chat.appendChild(el);
    stick(chat, pinned);
    replyLine = null; // a tool call ends the current prose run
  };

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message, persona: $("persona").value }),
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
          trace("dim", `· ${ev.message}`);
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
    // Re-park the cursor at the foot of the buffer.
    chat.querySelector(".cursor-line")?.remove();
    const cur = document.createElement("p");
    cur.className = "line cursor-line";
    const blk = document.createElement("span");
    blk.className = "cursor";
    cur.appendChild(blk);
    chat.appendChild(cur);
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

  poll();
  setInterval(poll, POLL_MS);
})();
