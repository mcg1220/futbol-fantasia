// World Cup draft randomizer — avatar picker + canvas scramble animation.
// Used both as a no-consequence sandbox (/draft-randomizer-poc, no
// WC_REAL_MODE global) and as the real Main Draft randomizer (/draft, where
// WC_REAL_MODE = true and a successful run POSTs the result's token to
// /api/world-cup-sim/lock-in to actually persist draft_order).

let wcPicks = {}; // manager_id -> emoji
let wcResult = null; // full server payload for the current run
let wcLockInToken = null; // token from the server for this run, consumed on lock-in
let wcEmojiByManager = {};
let wcTallyByManager = {}; // manager_id -> main-game final_tally (goals) only
let wcDisplayTally = {}; // manager_id -> main-game goals + any tiebreaker goals, for the standings/reveal display
let wcSortedPlayers = []; // main_game players sorted by final_tally desc (rank order)
let wcTiebreakerQueue = []; // [{startRank, endRank, managerIds, tally}]
let wcTiebreakerIdx = 0;
let wcResolvedOrder = {}; // rank (int) -> manager_id, filled in progressively
let wcPauseToggle = null; // set by the currently-running phase animation

(function injectWorldCupStyles() {
  const style = document.createElement('style');
  style.textContent = `
    @keyframes wcConfettiFall { from { transform: translateY(0) rotate(0deg); opacity:0.9; } to { transform: translateY(420px) rotate(360deg); opacity:0; } }
    @keyframes wcCountdownFlash { 0%, 49% { color: #ff3b3b; } 50%, 100% { color: inherit; } }
    .wc-countdown-warning { animation: wcCountdownFlash 1s steps(1, start) infinite; font-weight: 800; }
  `;
  document.head.appendChild(style);
})();

// ── Picker phase ─────────────────────────────────────────────────────────────

function initWorldCupPicker() {
  WC_MANAGERS.forEach(mid => {
    const grid = document.querySelector(`.wc-emoji-grid[data-manager-id="${mid}"]`);
    WC_EMOJI_POOL.forEach(emoji => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'wc-emoji-btn';
      btn.textContent = emoji;
      btn.dataset.emoji = emoji;
      btn.style.cssText = 'font-size:20px;padding:6px 0;border:2px solid transparent;border-radius:8px;background:var(--bg-secondary,#222);cursor:pointer';
      btn.onclick = () => pickAvatar(mid, emoji);
      grid.appendChild(btn);
    });
  });
  refreshPickerUI();
}

function pickAvatar(managerId, emoji) {
  if (wcPicks[managerId] === emoji) {
    delete wcPicks[managerId];
  } else {
    wcPicks[managerId] = emoji;
  }
  refreshPickerUI();
}

function refreshPickerUI() {
  const usedEmoji = new Set(Object.values(wcPicks));
  WC_MANAGERS.forEach(mid => {
    const grid = document.querySelector(`.wc-emoji-grid[data-manager-id="${mid}"]`);
    const mine = wcPicks[mid];
    grid.querySelectorAll('.wc-emoji-btn').forEach(btn => {
      const e = btn.dataset.emoji;
      const takenByOther = usedEmoji.has(e) && mine !== e;
      btn.disabled = takenByOther;
      btn.style.opacity = takenByOther ? '0.3' : '1';
      btn.style.borderColor = mine === e ? 'var(--pl-magenta,#E90052)' : 'transparent';
      btn.style.background = mine === e ? 'rgba(233,0,82,0.15)' : 'var(--bg-secondary,#222)';
    });
  });

  const startBtn = document.getElementById('wc-start-btn');
  const allPicked = WC_MANAGERS.every(mid => wcPicks[mid]);
  startBtn.disabled = !allPicked;
}

async function startWorldCupSim() {
  const errorEl = document.getElementById('wc-picker-error');
  errorEl.style.display = 'none';
  const startBtn = document.getElementById('wc-start-btn');
  startBtn.disabled = true;

  try {
    const resp = await fetch('/api/world-cup-sim/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ avatars: wcPicks }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || 'Simulation failed.');
    }

    wcResult = data;
    wcLockInToken = data.token || null;
    wcEmojiByManager = {};
    wcTallyByManager = {};
    wcDisplayTally = {};
    data.main_game.players.forEach(p => {
      wcEmojiByManager[p.manager_id] = p.emoji;
      wcTallyByManager[p.manager_id] = p.final_tally;
      wcDisplayTally[p.manager_id] = p.final_tally;
    });

    document.getElementById('wc-picker-phase').style.display = 'none';
    document.getElementById('wc-game-phase').style.display = 'block';
    runPhaseAnimation({
      kind: 'main',
      label: '⚽ The Scramble',
      data: wcResult.main_game,
      playerIds: wcResult.main_game.players.map(p => p.manager_id),
      pickOffset: 0,
    }, showStandingsPhase);
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.style.display = 'block';
    startBtn.disabled = false;
  }
}

// ── Geometry + small helpers ─────────────────────────────────────────────────

const WC_CANVAS_W = 900;
const WC_CANVAS_H = 480;
const WC_GOAL_X = WC_CANVAS_W / 2;
const WC_GOAL_Y = 60;
const WC_SCRUM_Y = 300;
const GRANT_HOME = { x: WC_GOAL_X, y: WC_GOAL_Y };

// Total real-world playback time each phase is scaled to hit, regardless of
// how many events the RNG produced this run.
const MAIN_GAME_TARGET_MS = 58_000;
const TIEBREAKER_TARGET_MS = 20_000;

function lerp(a, b, t) { return a + (b - a) * t; }
function lerpPoint(p1, p2, t) { return { x: lerp(p1.x, p2.x, t), y: lerp(p1.y, p2.y, t) }; }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function shuffleArray(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function wcScrumPositions(managerIds) {
  const n = managerIds.length;
  const radiusX = Math.min(320, 60 + n * 30);
  const positions = {};
  managerIds.forEach((mid, i) => {
    const angle = (i / n) * Math.PI * 2;
    positions[mid] = {
      x: WC_GOAL_X + Math.cos(angle) * radiusX,
      y: WC_SCRUM_Y + Math.sin(angle) * 90,
    };
  });
  return positions;
}

function randomDribbleSpot(from) {
  const x = clamp(from.x + (Math.random() - 0.5) * 160, 90, WC_CANVAS_W - 90);
  const y = clamp(from.y + (Math.random() - 0.5) * 100, WC_SCRUM_Y - 120, WC_SCRUM_Y + 140);
  return { x, y };
}

// Re-deals the same ring of slot coordinates to a freshly-shuffled manager
// order. WHO gets the ball next is decided entirely server-side (uniform
// random among non-holders — see world_cup_sim.py's _build_events, which has
// no concept of pixel geometry at all), so starting position never actually
// affects a player's odds. This just re-randomizes the *visual* layout after
// every goal so no manager can ever appear to "own" a favored spot.
function reshuffleHome(playerIds, home) {
  const slots = playerIds.map(id => home[id]);
  const shuffledIds = shuffleArray(playerIds);
  const newHome = {};
  shuffledIds.forEach((id, i) => { newHome[id] = slots[i]; });
  return newHome;
}

function formatClock(ms) {
  const totalSeconds = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

// ── Sound effects (Web Audio, no external assets) ────────────────────────────

let wcAudioCtx = null;
function wcGetAudioCtx() {
  if (!wcAudioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    wcAudioCtx = new Ctx();
  }
  if (wcAudioCtx.state === 'suspended') wcAudioCtx.resume();
  return wcAudioCtx;
}

function playTone(freq, delaySec, durSec, type, peakGain) {
  const ctx = wcGetAudioCtx();
  if (!ctx) return;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = type || 'sine';
  osc.frequency.value = freq;
  const t0 = ctx.currentTime + delaySec;
  gain.gain.setValueAtTime(0, t0);
  gain.gain.linearRampToValueAtTime(peakGain || 0.15, t0 + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.001, t0 + durSec);
  osc.connect(gain).connect(ctx.destination);
  osc.start(t0);
  osc.stop(t0 + durSec + 0.05);
}

function playGoalSound() {
  playTone(523.25, 0, 0.12, 'triangle', 0.18);
  playTone(659.25, 0.09, 0.16, 'triangle', 0.18);
  playTone(783.99, 0.18, 0.24, 'triangle', 0.2);
}

function playSaveSound() {
  playTone(300, 0, 0.12, 'square', 0.22);
  playTone(160, 0.09, 0.24, 'square', 0.24);
}

function playWinSound() {
  [523.25, 659.25, 783.99, 1046.5].forEach((f, i) => playTone(f, i * 0.14, 0.3, 'triangle', 0.2));
}

function playTickSound() {
  playTone(880, 0, 0.08, 'square', 0.2);
}

// ── Goal particle effects ────────────────────────────────────────────────────

function spawnGoalParticles(d, pos, now) {
  const emojis = ['✨', '🎉', '⭐'];
  for (let i = 0; i < 10; i++) {
    const angle = Math.random() * Math.PI * 2;
    const speed = 60 + Math.random() * 90;
    d.particles.push({
      x: pos.x, y: pos.y,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed - 40,
      spawnTime: now,
      life: 700 + Math.random() * 300,
      emoji: emojis[Math.floor(Math.random() * emojis.length)],
    });
  }
}

function groupIntoSequences(events) {
  const sequences = [];
  let current = [];
  events.forEach(e => {
    current.push(e);
    if (e.type === 'shot') { sequences.push(current); current = []; }
  });
  return sequences;
}

// ── Step builder: turns event sequences into a choreographed tween timeline ──

function buildSteps(sequences, playerIds, initialHome) {
  let home = initialHome;
  const finalPos = {};
  playerIds.forEach(id => { finalPos[id] = { ...home[id] }; });
  let ballPos = { x: WC_GOAL_X, y: WC_SCRUM_Y };
  let holder = null;

  const steps = [];

  sequences.forEach(seq => {
    const hops = seq.slice(0, -1);
    const shotEvent = seq[seq.length - 1];

    hops.forEach(hop => {
      const newHolder = hop.manager_id;

      // Contest flourish: 1-2 other players lunge toward the ball and get shaken off.
      const others = playerIds.filter(id => id !== newHolder && id !== holder);
      const defenders = shuffleArray(others).slice(0, Math.random() < 0.5 ? 1 : 2);
      if (defenders.length) {
        const lungeTo = {};
        const lungeFrom = {};
        defenders.forEach(id => {
          lungeFrom[id] = { ...finalPos[id] };
          lungeTo[id] = lerpPoint(finalPos[id], ballPos, 0.5);
        });
        steps.push({ dur: 300, movers: defenders.map(id => ({ id, from: lungeFrom[id], to: lungeTo[id] })) });
        steps.push({ dur: 250, movers: defenders.map(id => ({ id, from: lungeTo[id], to: home[id] })) });
        defenders.forEach(id => { finalPos[id] = { ...home[id] }; });
      }

      // Win & carry: the new holder closes on the ball, then dribbles it to a new spot.
      const holderFrom = { ...finalPos[newHolder] };
      steps.push({
        dur: 350,
        movers: [{ id: newHolder, from: holderFrom, to: { ...ballPos } }],
        ball: { from: { ...ballPos }, to: { ...ballPos } },
      });
      finalPos[newHolder] = { ...ballPos };
      holder = newHolder;

      const dribbleTo = randomDribbleSpot(ballPos);
      steps.push({
        dur: 350,
        movers: [{ id: newHolder, from: { ...ballPos }, to: dribbleTo }],
        ball: { from: { ...ballPos }, to: dribbleTo },
      });
      finalPos[newHolder] = { ...dribbleTo };
      ballPos = { ...dribbleTo };
    });

    // The shot itself: holder breaks toward the box, then Grant reacts.
    const shooter = shotEvent.manager_id;
    const boxSpot = { x: WC_GOAL_X + (Math.random() * 80 - 40), y: WC_GOAL_Y + 70 };
    steps.push({
      dur: 400,
      movers: [{ id: shooter, from: { ...ballPos }, to: boxSpot }],
      ball: { from: { ...ballPos }, to: boxSpot },
    });
    finalPos[shooter] = { ...boxSpot };
    ballPos = { ...boxSpot };

    const outcome = shotEvent.outcome;
    const netSpot = { x: WC_GOAL_X, y: WC_GOAL_Y + 10 };
    const grantTo = outcome === 'save'
      ? { ...netSpot }
      : { x: GRANT_HOME.x + (Math.random() < 0.5 ? -55 : 55), y: GRANT_HOME.y + 10 };
    steps.push({
      dur: 320,
      movers: [],
      ball: { from: { ...ballPos }, to: netSpot },
      grant: { from: { ...GRANT_HOME }, to: grantTo },
      resolve: { manager_id: shooter, outcome },
    });
    ballPos = { x: WC_GOAL_X, y: WC_SCRUM_Y };
    holder = null;

    // A brief hold on the moment — longer for goals — so scoring registers
    // as a real event instead of blurring straight into the next play.
    steps.push({ dur: outcome === 'goal' ? 650 : 300, movers: [] });

    // Re-randomize who's assigned to which slot before drifting back, so
    // positions never stay fixed across goals (see reshuffleHome above).
    home = reshuffleHome(playerIds, home);

    // Everyone eases partway back toward their (newly reassigned) home slot; Grant resets.
    const driftMovers = playerIds.map(id => ({
      id, from: { ...finalPos[id] }, to: lerpPoint(finalPos[id], home[id], 0.5),
    }));
    driftMovers.forEach(m => { finalPos[m.id] = { ...m.to }; });
    steps.push({ dur: 400, movers: driftMovers, grant: { from: grantTo, to: { ...GRANT_HOME } } });
  });

  return steps;
}

// ── Step runner: plays the timeline via requestAnimationFrame ───────────────

function runSteps(steps, ctx, d, onDone, countdownEl, pauseBtn) {
  const totalMs = steps.reduce((sum, s) => sum + s.dur, 0);
  let idx = 0;
  let stepStart = 0;
  let elapsedBeforeCurrentStep = 0;
  let paused = false;
  let lastNow = 0;
  let awaitingResumeBaseline = false;
  let lastTickSecond = null;

  function togglePause() {
    paused = !paused;
    if (pauseBtn) pauseBtn.textContent = paused ? '▶ Resume' : '⏸ Pause';
    if (!paused) {
      awaitingResumeBaseline = true;
      requestAnimationFrame(frame);
    }
  }
  wcPauseToggle = togglePause;
  if (pauseBtn) { pauseBtn.disabled = false; pauseBtn.textContent = '⏸ Pause'; }

  function frame(now) {
    if (paused) return;
    if (awaitingResumeBaseline) {
      stepStart += (now - lastNow);
      awaitingResumeBaseline = false;
    }
    lastNow = now;

    if (idx >= steps.length) {
      drawFrame(ctx, d, now);
      if (countdownEl) {
        countdownEl.textContent = '⏱ 0:00';
        countdownEl.classList.remove('wc-countdown-warning');
      }
      if (pauseBtn) pauseBtn.disabled = true;
      wcPauseToggle = null;
      onDone();
      return;
    }
    if (stepStart === 0) stepStart = now;
    const step = steps[idx];
    const t = Math.min(1, (now - stepStart) / step.dur);

    if (countdownEl) {
      const elapsed = elapsedBeforeCurrentStep + Math.min(step.dur, now - stepStart);
      const remainingMs = totalMs - elapsed;
      countdownEl.textContent = `⏱ ${formatClock(remainingMs)}`;
      const remainingSeconds = Math.ceil(remainingMs / 1000);
      if (remainingSeconds <= 10 && remainingSeconds >= 0) {
        countdownEl.classList.add('wc-countdown-warning');
        if (remainingSeconds !== lastTickSecond && remainingSeconds >= 1) {
          playTickSound();
        }
      } else {
        countdownEl.classList.remove('wc-countdown-warning');
      }
      lastTickSecond = remainingSeconds;
    }

    step.movers.forEach(m => { d.livePos[m.id] = lerpPoint(m.from, m.to, t); });
    if (step.ball) d.ballPos = lerpPoint(step.ball.from, step.ball.to, t);
    if (step.grant) d.grantPos = lerpPoint(step.grant.from, step.grant.to, t);

    drawFrame(ctx, d, now);

    if (t >= 1) {
      elapsedBeforeCurrentStep += step.dur;
      step.movers.forEach(m => { d.livePos[m.id] = { ...m.to }; });
      if (step.ball) d.ballPos = { ...step.ball.to };
      if (step.grant) d.grantPos = { ...step.grant.to };
      if (step.resolve) {
        const { manager_id, outcome } = step.resolve;
        if (outcome === 'goal') {
          d.tallies[manager_id] = (d.tallies[manager_id] || 0) + 1;
          renderLiveStandings(d);
          d.flashText = `⚽ GOAL — ${WC_MANAGER_NAMES[manager_id]}!`;
          d.flashUntil = now + 1200;
          spawnGoalParticles(d, step.ball.to, now);
          playGoalSound();
        } else {
          d.flashText = '🧤 SAVED!';
          d.flashUntil = now + 800;
          playSaveSound();
        }
      }
      idx++;
      stepStart = 0;
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

function goalsLabel(tally) {
  return `${tally} goal${tally === 1 ? '' : 's'}`;
}

function renderLiveStandings(d) {
  d.liveStandingsEl.innerHTML = '';
  const ranked = d.playerIds.slice().sort((a, b) => (d.tallies[b] || 0) - (d.tallies[a] || 0));
  ranked.forEach((mid, i) => {
    d.liveStandingsEl.appendChild(buildDraftOrderRow(d.pickOffset + i, {
      managerId: mid, emoji: wcEmojiByManager[mid], tally: d.tallies[mid] || 0,
    }));
  });
}

function drawFrame(ctx, d, now) {
  ctx.clearRect(0, 0, WC_CANVAS_W, WC_CANVAS_H);

  ctx.fillStyle = '#1b5e20';
  ctx.fillRect(0, 0, WC_CANVAS_W, WC_CANVAS_H);
  ctx.strokeStyle = 'rgba(255,255,255,0.4)';
  ctx.lineWidth = 3;
  ctx.strokeRect(20, 20, WC_CANVAS_W - 40, WC_CANVAS_H - 40);
  ctx.strokeRect(WC_GOAL_X - 90, 20, 180, 90);

  ctx.textAlign = 'center';
  ctx.font = '32px sans-serif';
  ctx.fillText('🧤', d.grantPos.x, d.grantPos.y + 10);
  ctx.font = '12px sans-serif';
  ctx.fillStyle = '#fff';
  ctx.fillText('Grant', GRANT_HOME.x, GRANT_HOME.y + 34);

  d.playerIds.forEach(id => {
    const pos = d.livePos[id];
    ctx.font = '28px sans-serif';
    ctx.fillText(wcEmojiByManager[id] || '⚽', pos.x, pos.y + 8);
    ctx.font = 'bold 11px sans-serif';
    ctx.fillStyle = '#fff';
    ctx.fillText(WC_MANAGER_NAMES[id], pos.x, pos.y + 24);
  });

  ctx.font = '18px sans-serif';
  ctx.fillText('⚪', d.ballPos.x, d.ballPos.y);

  d.particles = d.particles.filter(p => now - p.spawnTime < p.life);
  d.particles.forEach(p => {
    const age = now - p.spawnTime;
    const tt = age / 1000;
    const px = p.x + p.vx * tt;
    const py = p.y + p.vy * tt + 0.5 * 260 * tt * tt;
    ctx.globalAlpha = Math.max(0, 1 - age / p.life);
    ctx.font = '16px sans-serif';
    ctx.fillText(p.emoji, px, py);
    ctx.globalAlpha = 1;
  });

  if (d.flashText && now < d.flashUntil) {
    ctx.font = 'bold 22px sans-serif';
    ctx.fillStyle = '#FFEB3B';
    ctx.fillText(d.flashText, WC_CANVAS_W / 2, WC_CANVAS_H - 30);
  }
}

function runPhaseAnimation(phase, onComplete) {
  const canvas = document.getElementById('wc-canvas');
  const ctx = canvas.getContext('2d');
  document.getElementById('wc-phase-banner').textContent = phase.label;
  const countdownEl = document.getElementById('wc-countdown');
  const pauseBtn = document.getElementById('wc-pause-btn');
  const liveStandingsEl = document.getElementById('wc-live-standings');

  // Shuffle who starts in which ring slot each phase, on top of the
  // per-goal reshuffling inside buildSteps — no manager is ever anchored
  // to the same visual starting spot run to run.
  const home = wcScrumPositions(shuffleArray(phase.playerIds));
  const d = {
    playerIds: phase.playerIds,
    pickOffset: phase.pickOffset || 0,
    livePos: {},
    ballPos: { x: WC_GOAL_X, y: WC_SCRUM_Y },
    grantPos: { ...GRANT_HOME },
    tallies: {},
    particles: [],
    flashText: '',
    flashUntil: 0,
    liveStandingsEl,
  };
  phase.playerIds.forEach(id => { d.livePos[id] = { ...home[id] }; d.tallies[id] = 0; });
  renderLiveStandings(d);

  const sortedEvents = phase.data.events.slice().sort((a, b) => a.t_ms - b.t_ms);
  const sequences = groupIntoSequences(sortedEvents);
  const steps = buildSteps(sequences, phase.playerIds, home);

  // Scale every step's duration so the whole phase always plays out in
  // roughly the same real-world time, regardless of how many events this
  // run's RNG happened to produce.
  const target = phase.kind === 'tiebreaker' ? TIEBREAKER_TARGET_MS : MAIN_GAME_TARGET_MS;
  const natural = steps.reduce((sum, s) => sum + s.dur, 0);
  const scale = natural > 0 ? target / natural : 1;
  steps.forEach(s => { s.dur = Math.max(70, Math.round(s.dur * scale)); });

  runSteps(steps, ctx, d, () => {
    if (phase.kind === 'tiebreaker' && phase.data.sudden_death) {
      const sd = phase.data.sudden_death;
      document.getElementById('wc-phase-banner').textContent =
        `⚡ Sudden Death — ${WC_MANAGER_NAMES[sd.scorer]} scores the golden goal!`;
      setTimeout(onComplete, 1800);
    } else {
      setTimeout(onComplete, 500);
    }
  }, countdownEl, pauseBtn);
}

// ── Standings + staged tiebreaker flow ───────────────────────────────────────

function computeTieClusters(players) {
  const sorted = players.slice().sort((a, b) => b.final_tally - a.final_tally);
  const clusters = [];
  let i = 0;
  while (i < sorted.length) {
    let j = i + 1;
    while (j < sorted.length && sorted[j].final_tally === sorted[i].final_tally) j++;
    if (j - i >= 2) {
      clusters.push({
        startRank: i, endRank: j - 1,
        managerIds: sorted.slice(i, j).map(p => p.manager_id),
        tally: sorted[i].final_tally,
      });
    }
    i = j;
  }
  return { sorted, clusters };
}

// ── Shared draft-order-list row rendering (matches the real draft's
//    "Draft Order Locked In" card styling in style.css) ──────────────────────

function buildDraftOrderRow(rank, content) {
  const row = document.createElement('div');
  row.className = 'draft-order-item';

  const num = document.createElement('span');
  num.className = 'draft-order-num';
  num.textContent = String(rank + 1);
  row.appendChild(num);

  const avatar = document.createElement('div');
  avatar.className = 'team-avatar team-avatar-sm team-avatar-placeholder';
  avatar.style.fontSize = '15px';
  avatar.textContent = content.emoji || '❓';
  row.appendChild(avatar);

  const text = document.createElement('span');
  if (content.managerId != null) {
    text.textContent = `${WC_MANAGER_NAMES[content.managerId]} — ${WC_MANAGER_TEAMS[content.managerId]} `;
    const goals = document.createElement('span');
    goals.style.cssText = 'color:var(--text-muted);font-size:12px';
    const tally = content.tally != null ? content.tally : wcDisplayTally[content.managerId];
    goals.textContent = `(${goalsLabel(tally)})`;
    text.appendChild(goals);
  } else {
    text.style.cssText = 'color:var(--text-muted);font-style:italic';
    text.textContent = content.placeholderText;
    row.style.opacity = '0.75';
  }
  row.appendChild(text);

  return row;
}

function showStandingsPhase() {
  document.getElementById('wc-game-phase').style.display = 'none';
  document.getElementById('wc-standings-title').textContent = '📋 Draft Order So Far';
  document.getElementById('wc-standings-phase').style.display = 'block';

  const { sorted, clusters } = computeTieClusters(wcResult.main_game.players);
  wcSortedPlayers = sorted;
  wcTiebreakerQueue = clusters;
  wcTiebreakerIdx = 0;
  wcResolvedOrder = {};

  sorted.forEach((p, rank) => {
    const inCluster = clusters.some(c => rank >= c.startRank && rank <= c.endRank);
    if (!inCluster) wcResolvedOrder[rank] = p.manager_id;
  });

  renderStandingsList();
  advanceTiebreakerQueue();
}

function renderStandingsList() {
  const list = document.getElementById('wc-standings-list');
  list.innerHTML = '';
  wcSortedPlayers.forEach((p, rank) => {
    const cluster = wcTiebreakerQueue.find(c => rank >= c.startRank && rank <= c.endRank);
    let row;
    if (wcResolvedOrder[rank] != null) {
      const mid = wcResolvedOrder[rank];
      row = buildDraftOrderRow(rank, { managerId: mid, emoji: wcEmojiByManager[mid] });
    } else if (cluster) {
      row = buildDraftOrderRow(rank, {
        placeholderText: `Tiebreaker — picks ${cluster.startRank + 1}-${cluster.endRank + 1} (tied at ${goalsLabel(cluster.tally)})`,
      });
    }
    if (row) list.appendChild(row);
  });
}

function advanceTiebreakerQueue() {
  if (wcTiebreakerIdx >= wcTiebreakerQueue.length) {
    document.getElementById('wc-stakes-card').style.display = 'none';
    setTimeout(lockInDraftOrder, 800);
    return;
  }

  const cluster = wcTiebreakerQueue[wcTiebreakerIdx];
  const card = document.getElementById('wc-stakes-card');
  card.style.display = 'block';
  document.getElementById('wc-stakes-title').textContent =
    `Tiebreaker — picks ${cluster.startRank + 1}-${cluster.endRank + 1}`;
  document.getElementById('wc-stakes-players').textContent =
    cluster.managerIds.map(id => `${wcEmojiByManager[id]} ${WC_MANAGER_NAMES[id]}`).join('  vs  ');

  const btn = document.getElementById('wc-run-tiebreaker-btn');
  btn.disabled = false;
  btn.onclick = () => runStagedTiebreaker(cluster);
}

function runStagedTiebreaker(cluster) {
  document.getElementById('wc-run-tiebreaker-btn').disabled = true;
  document.getElementById('wc-standings-phase').style.display = 'none';
  document.getElementById('wc-game-phase').style.display = 'block';

  const tb = wcResult.tiebreakers[wcTiebreakerIdx];
  runPhaseAnimation({
    kind: 'tiebreaker',
    label: `🔁 Tiebreaker — picks ${cluster.startRank + 1}-${cluster.endRank + 1}`,
    data: tb,
    playerIds: tb.player_ids,
    pickOffset: cluster.startRank,
  }, () => {
    const resolvedSlice = wcResult.draft_order.slice(cluster.startRank, cluster.endRank + 1);
    resolvedSlice.forEach((mid, i) => { wcResolvedOrder[cluster.startRank + i] = mid; });

    // Fold the tiebreaker's own goals into the displayed total so the
    // standings number actually explains why the order came out this way
    // (otherwise two players who were tied 0-0 in regulation but split by
    // the tiebreaker would still both show "0 goals" here).
    Object.entries(tb.final_tallies).forEach(([midStr, t]) => {
      const mid = Number(midStr);
      wcDisplayTally[mid] = (wcDisplayTally[mid] || 0) + t;
    });
    if (tb.sudden_death) {
      // Sudden death can still leave the tiebreaker tallies tied at the top;
      // nudge the actual golden-goal scorer so the number matches the order.
      wcDisplayTally[tb.sudden_death.scorer] = (wcDisplayTally[tb.sudden_death.scorer] || 0) + 1;
    }

    document.getElementById('wc-game-phase').style.display = 'none';
    document.getElementById('wc-standings-phase').style.display = 'block';
    renderStandingsList();
    wcTiebreakerIdx++;
    advanceTiebreakerQueue();
  });
}

// Once every rank is resolved, the "Draft Order So Far" panel becomes the
// final locked-in view in place — no separate reveal screen to rebuild into,
// which is what caused the old "shows then redisplays from the top" glitch.
function lockInDraftOrder() {
  document.getElementById('wc-standings-title').textContent = '✅ Draft Order Locked In';
  document.getElementById('wc-run-again-btn').style.display = 'inline-block';
  celebrateWinner();

  if (typeof WC_REAL_MODE !== 'undefined' && WC_REAL_MODE && wcLockInToken) {
    persistDraftOrder(wcLockInToken);
  }
}

async function persistDraftOrder(token) {
  try {
    const resp = await fetch('/api/world-cup-sim/lock-in', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Could not lock in the draft order.');
    setTimeout(() => location.reload(), 2500);
  } catch (err) {
    alert(err.message);
  }
}

function celebrateWinner() {
  playWinSound();

  const panel = document.getElementById('wc-standings-phase');
  const confettiWrap = document.createElement('div');
  confettiWrap.style.cssText = 'position:relative;height:0;overflow:visible;';
  const colors = ['#E90052', '#FFD700', '#38003c', '#04f5ff', '#00ff87'];
  for (let i = 0; i < 28; i++) {
    const piece = document.createElement('div');
    const color = colors[Math.floor(Math.random() * colors.length)];
    const left = Math.random() * 100;
    const delay = Math.random() * 0.4;
    const duration = 1.6 + Math.random() * 0.9;
    const size = 6 + Math.random() * 5;
    piece.style.cssText = `position:absolute;top:-20px;left:${left}%;width:${size}px;height:${size * 0.4}px;background:${color};opacity:0.9;transform:rotate(${Math.random() * 360}deg);animation:wcConfettiFall ${duration}s ease-in ${delay}s forwards;`;
    confettiWrap.appendChild(piece);
  }
  panel.insertBefore(confettiWrap, panel.firstChild);
  setTimeout(() => confettiWrap.remove(), 2800);

  const firstRow = document.querySelector('#wc-standings-list .draft-order-item');
  if (firstRow) {
    firstRow.style.transition = 'box-shadow 0.3s, border-color 0.3s';
    firstRow.style.borderColor = 'var(--pl-magenta,#E90052)';
    firstRow.style.boxShadow = '0 0 0 2px rgba(233,0,82,0.35)';
  }
}
