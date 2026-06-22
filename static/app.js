// Calendar UI logic
(function () {
  const leagueColor = {};
  (window.SPORTS_CAL.leagues || []).forEach(l => { leagueColor[l.id] = l.color; });

  const STORAGE_KEY = 'sports-cal-leagues';
  const VIEW_KEY = 'sports-cal-view';
  const TZ_KEY = 'sports-cal-tz';
  const COMPACT_KEY = 'sports-cal-compact';

  // On a hard refresh / reload, wipe the saved view + league filter + compact
  // toggle so the app opens with its standard defaults: 3 Days, all leagues
  // selected, Fit 24h on. Timezone persists since that's a stable user pref.
  const navType = (performance.getEntriesByType('navigation')[0] || {}).type;
  if (navType === 'reload') {
    localStorage.removeItem(VIEW_KEY);
    localStorage.removeItem(STORAGE_KEY);
    localStorage.removeItem(COMPACT_KEY);
  }

  // Compact-grid toggle — squashes slots so 24h fit without scrolling.
  // Wiring the change handler happens AFTER the calendar is instantiated
  // (further down) so we can call calendar.updateSize() to make FC re-measure
  // slot heights and re-position events against them.
  // Default to compact (fit 24h on screen) on first visit. Once the user
  // toggles it off (saves '0'), respect that choice on subsequent loads.
  const compactStored = localStorage.getItem(COMPACT_KEY);
  const compactSaved = compactStored === null ? true : compactStored === '1';
  if (compactSaved) document.body.classList.add('compact-grid');
  const compactToggle = document.getElementById('compact-toggle');
  if (compactToggle) compactToggle.checked = compactSaved;

  function readActive() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) { return null; }
  }
  function writeActive(arr) { localStorage.setItem(STORAGE_KEY, JSON.stringify(arr)); }
  function currentActive() {
    return Array.from(document.querySelectorAll('.league-check'))
      .filter(c => c.checked)
      .map(c => c.dataset.league);
  }

  const saved = readActive();
  if (saved) {
    document.querySelectorAll('.league-check').forEach(c => {
      c.checked = saved.includes(c.dataset.league);
    });
  }

  function hexToRgba(hex, a) {
    if (!hex) return `rgba(99,102,241,${a})`;
    const h = hex.replace('#','');
    const n = parseInt(h.length === 3 ? h.split('').map(c=>c+c).join('') : h, 16);
    const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    return `rgba(${r},${g},${b},${a})`;
  }

  // Scroll the time grid to ~2h before current local time on first paint,
  // so the user lands near "now" instead of midnight.
  function _scrollAnchor() {
    const h = Math.max(0, new Date().getHours() - 2);
    return `${String(h).padStart(2,'0')}:00:00`;
  }

  // Tile layout uses FullCalendar's default percentage-based positioning.

  // Full set of button labels — always passed as one object when we update
  // (FC's setOption('buttonText') REPLACES the whole object, so partial
  // updates would wipe out everything else, leaving the default "list" text
  // on listDay/listWeek). Keep this in sync with headerToolbar.right.
  const BUTTON_TEXT = {
    today: 'Today',
    timeGridDay: 'Day',
    listWeek: 'Agenda',
    threeDay: '3 Days',
    timeGridWeek: 'Week',
    dayGridMonth: 'Month',
  };
  // "Today" label gets re-scoped to the active view (Day/Week/Month/etc.).
  const TODAY_LABELS = {
    listDay:      'Today',
    listWeek:     'Today',
    threeDay:     'Today',
    timeGridDay:  'Today',
    timeGridWeek: 'This Week',
    dayGridMonth: 'This Month',
  };
  // Pill mode disabled — every view uses FullCalendar defaults (full titles,
  // natural tile widths). Flip this back to a dayCount check if you want
  // pill mode in a specific view again.
  function isPillModeView() { return false; }

  // Day / 3-Day shingle. FC's slotEventOverlap doesn't behave like Google
  // Calendar nesting in v6, so we lay tiles out ourselves: each event tile
  // spans the full column minus a left offset scaled to the deepest overlap
  // in its column, so even the back-most tile keeps ~MIN_VISIBLE pixels free
  // on the left for its title.
  // Live-DOM text width measurement. Canvas measureText() doesn't apply
  // letter-spacing and uses fallback font metrics when the web font (Inter)
  // hasn't fully loaded yet — both cause under-estimation, which then makes
  // the last letter of a tile's text overflow into the rounded right edge.
  // A hidden inline-block span uses the real browser layout pipeline so the
  // measurement matches what gets rendered.
  function _measureTextPx(text, titleEl) {
    let span = _measureTextPx._span;
    if (!span) {
      span = document.createElement('span');
      span.style.position = 'absolute';
      span.style.visibility = 'hidden';
      span.style.whiteSpace = 'nowrap';
      span.style.top = '-9999px';
      span.style.left = '-9999px';
      document.body.appendChild(span);
      _measureTextPx._span = span;
    }
    const cs = getComputedStyle(titleEl);
    span.style.fontSize = cs.fontSize;
    span.style.fontWeight = cs.fontWeight;
    span.style.fontFamily = cs.fontFamily;
    span.style.fontStyle = cs.fontStyle;
    span.style.letterSpacing = cs.letterSpacing;
    span.textContent = text;
    return span.getBoundingClientRect().width;
  }

  // Pack each day-column. Tiles are grouped into **overlap clusters** (sets
  // of tiles connected by time-overlap). Inside a cluster:
  //   - sizeToTitle=true: each tile sizes to its own title width + padding
  //     (with a floor of maxTilePx). Cluster pitch = max title width in
  //     cluster + gap, so no horizontal overlap regardless of which tiles
  //     happen to be active at the same moment.
  //   - sizeToTitle=false: all tiles are fixed maxTilePx wide (Week mode).
  // If the cluster's natural pack exceeds column width, every desired width
  // is scaled down uniformly so the cluster fits.
  function _packTilesInColumns(maxTilePx, gap, sizeToTitle) {
    document.querySelectorAll('.fc-timegrid-col-events').forEach(colEvents => {
      const harnesses = Array.from(colEvents.querySelectorAll('.fc-timegrid-event-harness'));
      if (!harnesses.length) return;
      const colWidth = colEvents.getBoundingClientRect().width;
      const items = harnesses.map(h => {
        const r = h.getBoundingClientRect();
        return { h, top: r.top, bottom: r.bottom, level: 0, cluster: -1 };
      });
      // Sort by top ASC, then duration DESC. Tiles starting at the same y
      // (e.g. midnight) but with very different durations — like a tiny MLB
      // carryover (~30 min) sharing a slot with a fresh 2 h WC match — used
      // to give the carryover level 0 (column-left), pushing the long match
      // to the right. With duration-DESC, the dominant event takes the
      // primary left slot and the tail-end carryovers stack to its right.
      items.sort((a, b) => a.top - b.top || (b.bottom - b.top) - (a.bottom - a.top));

      // Level assignment (greedy lowest-unused level among prior overlaps).
      // Tolerance: 2 px slack so a carryover overnighter ending exactly at
      // midnight clusters with a tile starting exactly at midnight (their
      // pixel rects touch but don't strictly overlap by default). Keep this
      // SMALL — a larger tolerance (e.g. 20 px / ~40 min) chains every
      // near-adjacent tile in a busy column (MLB evenings) into one giant
      // cluster, squishing minority-league tiles (NBA/WNBA/UFC/Valorant) to
      // invisible widths.
      const OVERLAP_TOL = 2;
      function _vOverlap(a, b) {
        return a.bottom + OVERLAP_TOL > b.top && a.top < b.bottom + OVERLAP_TOL;
      }
      items.forEach((item, i) => {
        const taken = new Set();
        for (let j = 0; j < i; j++) {
          if (_vOverlap(items[j], item)) taken.add(items[j].level);
        }
        let level = 0;
        while (taken.has(level)) level++;
        item.level = level;
      });

      // Flood-fill clusters of time-overlapping tiles, keeping their members.
      const adj = items.map(() => []);
      for (let i = 0; i < items.length; i++) {
        for (let j = i + 1; j < items.length; j++) {
          if (_vOverlap(items[i], items[j])) { adj[i].push(j); adj[j].push(i); }
        }
      }
      const clusters = [];
      for (let i = 0; i < items.length; i++) {
        if (items[i].cluster !== -1) continue;
        const cid = clusters.length;
        const members = [];
        const stack = [i];
        while (stack.length) {
          const k = stack.pop();
          if (items[k].cluster !== -1) continue;
          items[k].cluster = cid;
          members.push(items[k]);
          adj[k].forEach(n => { if (items[n].cluster === -1) stack.push(n); });
        }
        clusters.push(members);
      }

      // Outer-tile padding (≈8 px both sides) + slack for sub-pixel font
      // metrics and the rounded right edge of the tile.
      const TITLE_PADDING = 18;
      function desiredWidth(item) {
        if (!sizeToTitle) return maxTilePx;
        const titleEl = item.h.querySelector('.fc-event-title');
        const text = titleEl && titleEl.textContent || '';
        if (!titleEl || !text) return maxTilePx;
        const textW = _measureTextPx(text, titleEl);
        return Math.max(maxTilePx, textW + TITLE_PADDING);
      }

      clusters.forEach(members => {
        const count = Math.max.apply(null, members.map(it => it.level)) + 1;
        const desired = members.map(desiredWidth);

        // Each level is a vertical lane. Tiles sharing a level never overlap in
        // time, so the lane only needs to be as wide as its widest title. Sizing
        // lanes independently (instead of one cluster-wide pitch) is what keeps
        // the gaps between tiles even: with a single pitch = maxDesired+gap,
        // every tile narrower than the widest title left a stray (maxDesired -
        // desired) gap before the next lane, which is the "askew / inconsistent
        // margin" look. It also means a long-title minority tile only widens its
        // OWN lane — it never inflates or squishes the others.
        const laneDesired = new Array(count).fill(0);
        members.forEach((item, idx) => {
          laneDesired[item.level] = Math.max(laneDesired[item.level], desired[idx]);
        });
        const naturalTotal =
          laneDesired.reduce((a, b) => a + b, 0) + (count - 1) * gap;

        const laneWidth = new Array(count);
        const laneLeft = new Array(count);
        if (naturalTotal <= colWidth) {
          // Tight pack: every lane is exactly as wide as it needs to be,
          // separated by a uniform `gap`.
          let x = 0;
          for (let L = 0; L < count; L++) {
            laneWidth[L] = laneDesired[L];
            laneLeft[L] = x;
            x += laneWidth[L] + gap;
          }
        } else {
          // Doesn't fit even when tightly packed — fall back to equal-width
          // lanes so the cluster fits the column and no single long title can
          // hog width and squish the rest (busy MLB evenings).
          const uniform = Math.max(1, (colWidth - (count - 1) * gap) / count);
          for (let L = 0; L < count; L++) {
            laneWidth[L] = uniform;
            laneLeft[L] = L * (uniform + gap);
          }
        }

        members.forEach(item => {
          const L = item.level;
          // Fill the lane (not just the title width) so every tile in a lane is
          // the same width and the gap to the next lane is always exactly `gap`.
          // Derive width from *rounded* lane boundaries — left = round(start),
          // right = round(start + laneWidth) — so adjacent integer tile edges
          // line up exactly and fractional lane widths can't leak a ±1px gap.
          const left = Math.round(laneLeft[L]);
          const right = Math.round(laneLeft[L] + laneWidth[L]);
          const w = Math.max(1, right - left);
          item.h.style.left = left + 'px';
          item.h.style.width = w + 'px';
          item.h.style.right = 'auto';
          item.h.style.insetInlineStart = left + 'px';
          item.h.style.insetInlineEnd = 'auto';
          item.h.style.zIndex = String(L + 1);
        });
      });
    });
  }

  // 14px skinny tiles — no titles fit, so Week CSS hides them and there's
  // nothing to grow toward.
  function packWeekTiles() { _packTilesInColumns(14, 1, false); }
  // 50px tiles for Day/3-Day, packed tight (0px gap). Solo tiles grow to fit
  // their title text, capped at column width.
  function packDay3DayTiles() { _packTilesInColumns(50, 0, true); }

  // Events that cross midnight render as two harnesses — one per day column.
  // FC lays each day out independently, so a game appearing at the right side
  // of Mon at 11pm can end up at the left side of Tue at 12am, which reads as
  // visual noise. Pin every secondary segment's left/right inset to whatever
  // the earliest-day segment got, so the same event keeps the same x-position
  // across days.
  function alignCrossDayEvents() {
    const tilesByEvent = new Map();
    document.querySelectorAll(
      '.fc-timegrid-col-events .fc-timegrid-event-harness'
    ).forEach(harness => {
      const eventEl = harness.querySelector('.fc-timegrid-event[data-cal-event-id]');
      if (!eventEl) return;
      const id = eventEl.dataset.calEventId;
      const colEvents = harness.closest('.fc-timegrid-col-events');
      const col = colEvents && colEvents.closest('.fc-timegrid-col');
      const date = col && col.dataset.date;
      if (!colEvents || !date) return;
      if (!tilesByEvent.has(id)) tilesByEvent.set(id, []);
      tilesByEvent.get(id).push({ harness, colEvents, date });
    });
    tilesByEvent.forEach(tiles => {
      if (tiles.length < 2) return;
      tiles.sort((a, b) => a.date < b.date ? -1 : a.date > b.date ? 1 : 0);
      const primary = tiles[0];
      const pRect = primary.harness.getBoundingClientRect();
      const pColRect = primary.colEvents.getBoundingClientRect();
      const leftPx = pRect.left - pColRect.left;
      const widthPx = pRect.width;
      tiles.slice(1).forEach(t => {
        t.harness.style.left = leftPx + 'px';
        t.harness.style.width = widthPx + 'px';
        t.harness.style.right = 'auto';
        t.harness.style.insetInlineStart = leftPx + 'px';
        t.harness.style.insetInlineEnd = 'auto';
      });
    });
  }

  function _resetHarnessStyles() {
    document.querySelectorAll('.fc-timegrid-event-harness').forEach(h => {
      h.style.width = '';
      h.style.maxWidth = '';
      h.style.left = '';
      h.style.right = '';
      h.style.insetInlineStart = '';
      h.style.insetInlineEnd = '';
      h.style.zIndex = '';
    });
  }

  function uniformizeWeekTiles() {
    const view = document.body.dataset.view;
    // Only reset harness inline styles for views we then re-layout ourselves.
    // Other views (Month) leave FC's positioning alone.
    if (view === 'threeDay' || view === 'timeGridDay') {
      _resetHarnessStyles();
      packDay3DayTiles();
      alignCrossDayEvents();
    } else if (view === 'timeGridWeek') {
      _resetHarnessStyles();
      packWeekTiles();
      alignCrossDayEvents();
    }
    document.querySelectorAll('.fc-event-title, .fc-event-time').forEach(el => {
      el.style.display = '';
    });
    document.body.dataset.pill = isPillModeView() ? 'on' : 'off';
    if (!isPillModeView()) return;
    const harnesses = document.querySelectorAll(
      '.fc-timegrid-col-events .fc-timegrid-event-harness'
    );
    if (!harnesses.length) return;
    let minW = Infinity;
    harnesses.forEach(h => {
      const w = h.offsetWidth;
      if (w > 4 && w < minW) minW = w;
    });
    if (!isFinite(minW)) return;
    harnesses.forEach(h => {
      h.style.width = minW + 'px';
      h.style.maxWidth = minW + 'px';
      h.style.insetInlineEnd = 'auto';
      h.style.right = 'auto';
    });
  }

  let _uniformWeekPending = false;
  function scheduleUniformizeWeek() {
    if (_uniformWeekPending) return;
    _uniformWeekPending = true;
    requestAnimationFrame(() => {
      _uniformWeekPending = false;
      uniformizeWeekTiles();
    });
  }

  function updateTodayButtonText(viewType) {
    const todayLabel = TODAY_LABELS[viewType] || 'Today';
    if (window.__calendar) {
      window.__calendar.setOption('buttonText', { ...BUTTON_TEXT, today: todayLabel });
    }
    // Defer to the next frame so FC has finished re-rendering the toolbar
    // BEFORE we patch the DOM — otherwise FC's re-render runs after our patch
    // and appends its label next to ours, producing e.g. "This WeekThis Month".
    // Then explicitly wipe child nodes before writing the new text so any
    // stray text nodes left by FC are gone.
    requestAnimationFrame(() => {
      const todayBtn = document.querySelector('.fc-today-button');
      if (!todayBtn) return;
      while (todayBtn.firstChild) todayBtn.removeChild(todayBtn.firstChild);
      todayBtn.textContent = todayLabel;
    });
  }

  // Calendar — guard the saved view against renames so an obsolete value
  // (e.g. "fourDay" from a previous build) doesn't render blank.
  const VALID_VIEWS = ['threeDay', 'timeGridWeek', 'timeGridDay', 'dayGridMonth', 'listWeek', 'listDay'];
  const savedView = localStorage.getItem(VIEW_KEY);
  // On mobile, default to a single Day so the calendar isn't wider than the
  // viewport. Desktop default stays at 3-Day. A saved view (if valid) wins
  // on either platform.
  const isMobile = window.matchMedia('(max-width: 720px)').matches;
  const defaultView = isMobile ? 'timeGridDay' : 'threeDay';
  const initialView = VALID_VIEWS.includes(savedView) ? savedView : defaultView;

  // Timezone — defaults to the browser's local zone if nothing saved
  const savedTz = localStorage.getItem(TZ_KEY) || 'local';
  const tzSelect = document.getElementById('tz-select');
  if (tzSelect) tzSelect.value = savedTz;

  const el = document.getElementById('calendar');

  // Calendar config is a function so we can rebuild on compact-mode toggle
  // (the only reliable way to change slotDuration in FC v6 — setOption
  // silently no-ops in many configurations). All view-affecting state
  // (current date, current view) is preserved across rebuilds.
  function buildCalendarOptions({ slotDuration, slotLabelInterval, initialView, initialDate }) {
    return {
    timeZone: savedTz,
    initialView,
    initialDate: initialDate || undefined,
    headerToolbar: {
      left: 'prev,next today',
      center: 'title',
      right: 'listWeek,timeGridDay,threeDay,timeGridWeek,dayGridMonth'
    },
    buttonText: { ...BUTTON_TEXT, today: TODAY_LABELS[initialView] || 'Today' },
    height: '100%',
    nowIndicator: true,
    expandRows: true,
    slotMinTime: '00:00:00',
    slotMaxTime: '24:00:00',
    scrollTime: _scrollAnchor(),
    slotDuration,
    slotLabelInterval,
    slotLabelFormat: { hour: 'numeric', meridiem: 'short' },
    allDaySlot: true,
    allDayText: 'all-day',
    eventTimeFormat: { hour: 'numeric', minute: '2-digit', meridiem: 'short' },
    // Position on the time grid implies the time — hide the in-tile clock
    // so the team matchup gets full width.
    displayEventTime: false,
    displayEventEnd: false,
    eventDisplay: 'block',
    slotEventOverlap: false,
    // Sort strictly by start time (then title as a stable tiebreaker), so the
    // agenda and time-grid both lay events out chronologically.
    eventOrder: 'start,title',
    eventMinHeight: 6,   // let very short events (wrap tails, 10-min slivers)
                         // render at their true proportional height instead
                         // of being padded out to look like longer ones
    // A game ending at 1 AM next day shouldn't render on that next day in
    // Month view. nextDayThreshold = 6 AM means anything ending before 6 AM
    // on day N+1 belongs to day N only (no cross-cell banner with a flat
    // left edge on the second day). Late-morning carryovers (>6 AM) still
    // span both days correctly.
    nextDayThreshold: '06:00:00',
    dayMaxEvents: 6,
    weekends: true,
    firstDay: 0,
    views: {
      threeDay: {
        type: 'timeGrid',
        duration: { days: 3 },
        dayHeaderFormat: { weekday: 'short', month: 'short', day: 'numeric' },
      },
      timeGridWeek: { dayHeaderFormat: { weekday: 'short', day: 'numeric' } },
      timeGridDay:  { dayHeaderFormat: { weekday: 'long', month: 'short', day: 'numeric' } },
      dayGridMonth: {
        // Hide leading/trailing days from adjacent months so June only shows
        // June. fixedWeekCount drops the trailing 6th week when not needed;
        // showNonCurrentDates blanks any other-month cells inside kept weeks.
        fixedWeekCount: false,
        showNonCurrentDates: false,
      },
      listWeek: {
        listDayFormat: { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' },
        listDaySideFormat: false,
        noEventsContent: 'No events in this week.',
        // Override the global displayEventTime: false so the agenda shows
        // start times next to each matchup.
        displayEventTime: true,
      },
      listDay: {
        listDayFormat: { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' },
        listDaySideFormat: false,
        noEventsContent: 'No events today.',
        displayEventTime: true,
      },
    },
    events: async (info, success, failure) => {
      try {
        const active = currentActive();
        if (active.length === 0) return success([]);
        const url = new URL('/api/events', window.location.origin);
        url.searchParams.set('start', info.startStr);
        url.searchParams.set('end', info.endStr);
        url.searchParams.set('leagues', active.join(','));
        const res = await fetch(url);
        const data = await res.json();
        success(data);
      } catch (e) { failure(e); }
    },
    eventClick: (info) => {
      info.jsEvent.preventDefault();
      // Agenda items are read-only — no popover, no navigation.
      if (info.view.type === 'listWeek' || info.view.type === 'listDay') return;
      info.jsEvent.stopPropagation();
      showPopover(info.event, info.el);
    },
    // Day-number is the only zoom-into-day handle (navLinks below) — clicking
    // empty cell space should NOT navigate, to avoid accidental zooms.
    navLinks: true,
    navLinkDayClick: 'timeGridDay',
    eventDidMount: (info) => {
      const ep = info.event.extendedProps || {};
      const bg = info.event.backgroundColor;
      const view = info.view.type;

      // Tag every rendered tile with its event id so alignCrossDayEvents()
      // can re-locate the same event across day columns.
      info.el.dataset.calEventId = info.event.id;

      // Tooltip with full title (for narrow time-grid tiles)
      const tip = `${ep.leagueName ? '[' + ep.leagueName + '] ' : ''}${ep.fullTitle || info.event.title}${ep.broadcast ? '\n' + ep.broadcast : ''}`;
      info.el.setAttribute('title', tip);

      // Time grid: solid color, white text
      if (view.startsWith('timeGrid') || view === 'threeDay') {
        if (!info.event.allDay) {
          info.el.style.backgroundColor = bg;
          info.el.style.borderLeft = `none`;
          info.el.style.color = '#FFFFFF';
        }
      }

      // Pill-mode text hiding is handled by CSS keyed off body[data-pill] —
      // doing it via inline styles here was getting stuck across view
      // changes because FC sometimes reuses event DOM nodes.

      // List view: enrich with league pill + subtitle
      if (view === 'listWeek' || view === 'listDay') {
        // Overnight events get rendered on both days they touch (the start
        // day at e.g. 9pm and the next day at "12am — 12:30am"). Hide the
        // continuation segment so the event appears only once, on its start
        // day. `isStart` is true only for the segment that contains the
        // actual event start.
        if (info.isStart === false) {
          info.el.style.display = 'none';
          return;
        }
        const titleCell = info.el.querySelector('.fc-list-event-title');
        if (titleCell && !titleCell.dataset.enriched) {
          titleCell.dataset.enriched = '1';
          const fullTitle = ep.fullTitle || info.event.title;
          const pill = ep.leagueName
            ? `<span class="lst-pill" style="background:${bg};">${ep.leagueName}</span>`
            : '';
          const subParts = [];
          if (ep.subtitle) subParts.push(ep.subtitle);
          if (ep.broadcast) subParts.push(ep.broadcast);
          const sub = subParts.length ? `<div class="lst-sub">${subParts.join(' · ')}</div>` : '';
          titleCell.innerHTML = `${pill}<span class="lst-title">${fullTitle}</span>${sub}`;
        }
        const dot = info.el.querySelector('.fc-list-event-dot');
        if (dot) { dot.style.borderColor = bg; }
      }
    },
    datesSet: (info) => {
      localStorage.setItem(VIEW_KEY, info.view.type);
      document.body.dataset.view = info.view.type;
      updateTodayButtonText(info.view.type);
      scheduleUniformizeWeek();
    },
    viewDidMount: (info) => {
      // Canonical hook for view-class management — fires every time a view
      // is mounted, including initial render and explicit changeView calls.
      document.body.dataset.view = info.view.type;
      updateTodayButtonText(info.view.type);
      scheduleUniformizeWeek();
    },
    eventsSet: () => {
      scheduleUniformizeWeek();
      // FC has finished mounting tiles for the new range. Wait long enough
      // that the fade-OUT animation has had time to complete, then drop the
      // class so the grid fades back IN with the new content. If the user's
      // navigation was very fast (cache hit, ~50ms), this delays the reveal
      // to keep the fade-out smooth; if it was slow (>fade duration), we
      // reveal immediately.
      const elapsed = performance.now() - (window.__kcalNavStart || 0);
      const wait = Math.max(0, 220 - elapsed);
      setTimeout(() => {
        document.body.classList.remove('kcal-navigating');
      }, wait);
    }
    };  // end buildCalendarOptions return
  }

  // ────────────────────────────────────────────────────────────────
  // Compact-grid toggle — fit a full 24h in the viewport, no scrolling.
  //
  // setOption('slotDuration', …) is unreliable in FC v6 — the slot grid
  // doesn't always re-layout. The only mechanism that ALWAYS works is to
  // destroy the calendar instance and rebuild it with the desired
  // slotDuration baked into the constructor.
  // ────────────────────────────────────────────────────────────────
  const NORMAL_SLOT_DURATION   = '00:30:00';
  const NORMAL_LABEL_INTERVAL  = '01:00:00';
  const COMPACT_FC_CHROME_PX   = 130;   // toolbar + day header + allDay strip
  // FC's natural minimum slot height (matches our app.css .fc-timegrid-slot).
  // expandRows only STRETCHES — it can't shrink below this — so we must
  // pick a slotDuration with `numSlots * NATURAL_SLOT_PX <= available`.
  const NATURAL_SLOT_PX        = 42;
  // Slot durations to try, in MINUTES — pick the SMALLEST whose total natural
  // height fits the available viewport. ALL are whole-hour multiples on
  // purpose: a non-hour size (e.g. 90) draws gridlines at :30 past odd hours
  // that can't carry a whole-hour label, so tiles end up next to unlabeled
  // half-hour lines and read as "~30 min off" even when positioned exactly.
  // With only 60/120/180/240, every gridline IS a labeled whole hour.
  const CLEAN_DURATIONS_MIN    = [60, 120, 180, 240];

  let calendar;  // will be assigned by mountCalendar()

  // Format a whole number of minutes as FullCalendar's "HH:MM:00" duration.
  function fmtDuration(min) {
    const hh = String(Math.floor(min / 60)).padStart(2, '0');
    const mm = String(min % 60).padStart(2, '0');
    return `${hh}:${mm}:00`;
  }
  const _gcd = (a, b) => (b ? _gcd(b, a % b) : a);

  function mountCalendar(compactMode, preserveDate) {
    let slotDur, slotLabelInt;
    if (compactMode) {
      const min = pickCompactSlotDurationMin();
      slotDur = fmtDuration(min);
      // The label interval must be a whole number of hours AND a multiple of
      // the slot size. Otherwise a 90-min slot labels at 1:30, 4:30, 7:30…,
      // and slotLabelFormat (hour-only) FLOORS those to "1pm/4pm/7pm" — so the
      // line claiming "4pm" actually sits at 4:30 and every tile looks ~30 min
      // off. lcm(min, 60) is the smallest interval that lands exactly on the
      // hour while still aligning to slot boundaries.
      slotLabelInt = fmtDuration(min / _gcd(min, 60) * 60);
    } else {
      slotDur = NORMAL_SLOT_DURATION;
      slotLabelInt = NORMAL_LABEL_INTERVAL;
    }
    const view = (calendar && calendar.view) ? calendar.view.type : initialView;
    const date = preserveDate && calendar ? calendar.getDate() : undefined;
    if (calendar) calendar.destroy();
    calendar = new FullCalendar.Calendar(el, buildCalendarOptions({
      slotDuration: slotDur,
      slotLabelInterval: slotLabelInt,
      initialView: view,
      initialDate: date,
    }));
    window.__calendar = calendar;
    calendar.render();
    updateTodayButtonText(calendar.view.type);
  }

  // Picks the smallest clean slotDuration whose 24-hour expansion fits in
  // the visible viewport space below the calendar.
  //
  // KEY: use VIEWPORT-relative space (window.innerHeight - calendar's top),
  // not calendar.getBoundingClientRect().height (which is the calendar's
  // currently-rendered height — already overflowing in non-compact mode).
  function pickCompactSlotDurationMin() {
    const calEl = document.getElementById('calendar');
    if (!calEl) return 120;
    const calTop = calEl.getBoundingClientRect().top;
    const avail = Math.max(200, window.innerHeight - calTop - COMPACT_FC_CHROME_PX - 16);
    // Find the smallest clean duration such that numSlots * naturalHeight <= avail.
    for (const min of CLEAN_DURATIONS_MIN) {
      const numSlots = 1440 / min;
      const totalNeeded = numSlots * NATURAL_SLOT_PX;
      if (totalNeeded <= avail) return min;
    }
    return 240;  // ultra-tiny viewport — 4h slots, 6 of them
  }

  function applyCompact(on) {
    document.body.classList.toggle('compact-grid', on);
    localStorage.setItem(COMPACT_KEY, on ? '1' : '0');
    mountCalendar(on, /*preserveDate=*/true);
  }

  // Capture-phase click listener fires BEFORE FullCalendar's own button
  // handler. The CSS fades the grid out smoothly; we only reveal it again
  // AFTER the fade-out has had time to complete AND after FC has settled
  // the new range (eventsSet fires). Safety timeout in case eventsSet
  // never fires (e.g. navigation with no event-data change).
  const FADE_OUT_MS = 220;
  window.__kcalNavStart = 0;
  let _navSafetyTimer = null;
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.fc-button')) return;
    document.body.classList.add('kcal-navigating');
    window.__kcalNavStart = performance.now();
    clearTimeout(_navSafetyTimer);
    _navSafetyTimer = setTimeout(() => {
      document.body.classList.remove('kcal-navigating');
    }, 1500);
  }, /*capture=*/true);

  // Anchor FC's "+N more" popover so its BOTTOM edge sits at the bottom of
  // the day cell that triggered it (popover grows upward instead of bleeding
  // off the bottom of the calendar).
  //
  // Implementation: record the source cell on click, then use a MutationObserver
  // to catch the popover the instant it's added to the DOM. Style overrides
  // use setProperty(..., 'important') because FC applies its own inline
  // top/left and would otherwise win.
  let _pendingMoreLinkCell = null;
  document.addEventListener('click', (e) => {
    const moreLink = e.target.closest('.fc-more-link');
    if (!moreLink) return;
    _pendingMoreLinkCell = moreLink.closest('.fc-daygrid-day');
  });

  function _anchorPopoverToCellBottom(popover, dayCell) {
    // Defer one frame so the popover has its real (post-content) height.
    requestAnimationFrame(() => {
      const cellRect = dayCell.getBoundingClientRect();
      const popRect = popover.getBoundingClientRect();
      let top = Math.max(8, cellRect.bottom - popRect.height);
      let left = Math.min(cellRect.left, window.innerWidth - popRect.width - 8);
      left = Math.max(8, left);
      popover.style.setProperty('position', 'fixed', 'important');
      popover.style.setProperty('top', top + 'px', 'important');
      popover.style.setProperty('left', left + 'px', 'important');
      popover.style.setProperty('right', 'auto', 'important');
      popover.style.setProperty('bottom', 'auto', 'important');
    });
  }

  new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType !== 1) continue;
        const popover = node.classList && node.classList.contains('fc-popover')
          ? node
          : node.querySelector && node.querySelector('.fc-popover');
        if (popover && _pendingMoreLinkCell) {
          _anchorPopoverToCellBottom(popover, _pendingMoreLinkCell);
          _pendingMoreLinkCell = null;
        }
      }
    }
  }).observe(document.body, { childList: true, subtree: true });

  // In Agenda views the "Today" button navigates the date range but doesn't
  // bring today's day-row into view if it isn't already. Hook the click and
  // scroll today's `.fc-list-day` into view once FC has finished rendering.
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.fc-today-button')) return;
    if (!calendar) return;
    const view = calendar.view.type;
    if (view !== 'listWeek' && view !== 'listDay') return;
    setTimeout(() => {
      const today = new Date();
      const dateStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
      const dayRow = document.querySelector(`.fc-list-day[data-date="${dateStr}"]`);
      if (dayRow) dayRow.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 50);
  });

  // Initial mount — uses compactSaved to pick the right slotDuration up-front
  mountCalendar(compactSaved, false);

  if (compactToggle) {
    compactToggle.addEventListener('change', () => applyCompact(compactToggle.checked));
  }

  // Re-pick slot duration on window resize so a bigger window doesn't waste space.
  let _resizeTimer;
  window.addEventListener('resize', () => {
    scheduleUniformizeWeek();
    if (!document.body.classList.contains('compact-grid')) return;
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => applyCompact(true), 200);
  });

  // Timezone change handler
  if (tzSelect) {
    tzSelect.addEventListener('change', () => {
      const newTz = tzSelect.value;
      localStorage.setItem(TZ_KEY, newTz);
      // FullCalendar v6 doesn't have setOption('timeZone'); easiest path is a reload
      window.location.reload();
    });
  }

  // Reflect each group-checkbox's state from its child league-checkboxes:
  // all checked → checked, all unchecked → unchecked, mixed → indeterminate.
  function syncGroupCheckboxes() {
    document.querySelectorAll('.group-check').forEach(gc => {
      const group = gc.dataset.group;
      const children = document.querySelectorAll(
        `.league-check[data-group="${group}"]`
      );
      const total = children.length;
      const checked = Array.from(children).filter(c => c.checked).length;
      if (checked === 0) {
        gc.checked = false;
        gc.indeterminate = false;
      } else if (checked === total) {
        gc.checked = true;
        gc.indeterminate = false;
      } else {
        gc.checked = false;
        gc.indeterminate = true;
      }
    });
  }

  // League filter wiring
  document.querySelectorAll('.league-check').forEach(c => {
    c.addEventListener('change', () => {
      writeActive(currentActive());
      syncGroupCheckboxes();
      calendar.refetchEvents();
    });
  });
  // Group checkbox: toggle every league in the group.
  document.querySelectorAll('.group-check').forEach(gc => {
    gc.addEventListener('change', () => {
      const group = gc.dataset.group;
      const target = gc.checked;
      document.querySelectorAll(
        `.league-check[data-group="${group}"]`
      ).forEach(c => { c.checked = target; });
      writeActive(currentActive());
      calendar.refetchEvents();
    });
  });
  // Group toggle: expand/collapse the sublist.
  document.querySelectorAll('.group-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const groupEl = btn.closest('.league-group');
      const open = groupEl.dataset.open !== 'false';
      groupEl.dataset.open = open ? 'false' : 'true';
    });
  });
  document.getElementById('all-leagues').addEventListener('click', () => {
    document.querySelectorAll('.league-check').forEach(c => c.checked = true);
    writeActive(currentActive());
    syncGroupCheckboxes();
    calendar.refetchEvents();
  });
  document.getElementById('no-leagues').addEventListener('click', () => {
    document.querySelectorAll('.league-check').forEach(c => c.checked = false);
    writeActive(currentActive());
    syncGroupCheckboxes();
    calendar.refetchEvents();
  });
  // Initial sync — saved state might have left groups partially checked.
  syncGroupCheckboxes();

  // Mobile sidebar drawer toggle. Hamburger button is only rendered on the
  // calendar page; CSS hides it >720px so this no-ops on desktop.
  const sidebarToggle = document.getElementById('sidebar-toggle');
  if (sidebarToggle) {
    sidebarToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = document.body.classList.toggle('sidebar-open');
      sidebarToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    // Backdrop click (anything outside the drawer + toggle) dismisses the
    // drawer. Inputs inside the drawer must keep working, so we only close
    // when the target is genuinely outside.
    document.addEventListener('click', (e) => {
      if (!document.body.classList.contains('sidebar-open')) return;
      if (e.target.closest('.sidebar') || e.target.closest('#sidebar-toggle')) return;
      document.body.classList.remove('sidebar-open');
      sidebarToggle.setAttribute('aria-expanded', 'false');
    });
  }

  // Refresh button — streams per-league progress over SSE and drives the bar.
  const refreshBtn = document.getElementById('refresh-btn');
  const refreshStatus = document.getElementById('refresh-status');
  const refreshProgress = document.getElementById('refresh-progress');
  const refreshBar = document.getElementById('refresh-bar');

  function reloadCacheBusted() {
    const url = new URL(window.location.href);
    url.searchParams.set('_', Date.now().toString());
    window.location.replace(url.toString());
  }

  function refreshFailed(msg) {
    refreshProgress.hidden = true;
    refreshProgress.classList.remove('indeterminate');
    refreshStatus.textContent = 'Error: ' + msg;
    refreshBtn.disabled = false;
    refreshBtn.textContent = 'Refresh data';
  }

  refreshBtn.addEventListener('click', () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Refreshing...';
    refreshStatus.textContent = '';
    refreshBar.style.width = '';
    refreshProgress.hidden = false;
    // Animate immediately; flip to determinate once the first league lands.
    refreshProgress.classList.add('indeterminate');

    const es = new EventSource('/api/refresh/stream');
    let finished = false;

    es.onmessage = (ev) => {
      let d;
      try { d = JSON.parse(ev.data); } catch { return; }

      if (d.type === 'progress') {
        refreshProgress.classList.remove('indeterminate');
        const pct = d.total ? Math.round((d.done / d.total) * 100) : 0;
        refreshBar.style.width = pct + '%';
        refreshStatus.textContent = `${d.done} / ${d.total} leagues…`;
      } else if (d.type === 'done') {
        finished = true;
        es.close();
        refreshProgress.classList.remove('indeterminate');
        refreshBar.style.width = '100%';
        refreshStatus.textContent = `Fetched ${d.total_events} events — reloading…`;
        // Brief pause so the filled bar is visible, then cache-busting reload.
        setTimeout(reloadCacheBusted, 400);
      } else if (d.type === 'error') {
        finished = true;
        es.close();
        refreshFailed(d.message || 'refresh failed');
      }
    };

    es.onerror = () => {
      if (finished) return;  // normal close after 'done'
      es.close();
      refreshFailed('connection lost');
    };
  });

  // Popover
  const popover = document.getElementById('event-popover');
  function showPopover(event, anchorEl) {
    const ep = event.extendedProps || {};
    document.getElementById('popover-league').textContent = ep.leagueName || ep.league || '';
    document.getElementById('popover-league').style.background = leagueColor[ep.league] || '#6B7280';
    document.getElementById('popover-title').textContent = ep.fullTitle || event.title || '';
    document.getElementById('popover-subtitle').textContent = ep.subtitle || '';

    const start = event.start;
    const end = event.end;
    // Format in the SAME timezone the calendar grid is using, so the popover
    // never disagrees with the row the tile sits in.
    const tzOpts = savedTz !== 'local' ? { timeZone: savedTz } : {};
    const dayFmt = { weekday: 'short', month: 'short', day: 'numeric', ...tzOpts };
    const timeFmt = { hour: 'numeric', minute: '2-digit', ...tzOpts };
    let whenStr;
    if (event.allDay) {
      // All-day events are floating dates (the API emits date-only YYYY-MM-DD).
      // FullCalendar builds event.start as midnight-on-that-date IN the
      // calendar's timezone, so format with savedTz (tzOpts) to read the date
      // back correctly in every zone. Using UTC here would shift it a day for
      // zones east of UTC (e.g. Tokyo midnight = 15:00Z the prior day).
      const endDay = end ? new Date(end.getTime() - 86400000) : null;
      const sameDay = endDay && endDay.toLocaleDateString('en-US', tzOpts) === start.toLocaleDateString('en-US', tzOpts);
      whenStr = endDay && !sameDay
        ? `${start.toLocaleDateString('en-US', dayFmt)} – ${endDay.toLocaleDateString('en-US', dayFmt)}`
        : start.toLocaleDateString('en-US', dayFmt);
    } else {
      whenStr = end
        ? `${start.toLocaleDateString('en-US', dayFmt)} · ${start.toLocaleTimeString('en-US', timeFmt)} – ${end.toLocaleTimeString('en-US', timeFmt)}`
        : `${start.toLocaleDateString('en-US', dayFmt)} · ${start.toLocaleTimeString('en-US', timeFmt)}`;
    }
    document.getElementById('popover-when').textContent = whenStr;

    setRow('popover-venue-row', ep.venue);
    document.getElementById('popover-venue').textContent = ep.venue || '';

    setRow('popover-note-row', ep.note);
    document.getElementById('popover-note').textContent = ep.note || '';

    const link = document.getElementById('popover-link');
    if (ep.url) { link.href = ep.url; link.style.display = ''; }
    else { link.style.display = 'none'; }

    popover.classList.remove('hidden');
    const anchor = anchorEl.getBoundingClientRect();
    const pop = popover.getBoundingClientRect();
    const margin = 8;
    let left = anchor.right + margin;
    let top = anchor.top;
    if (left + pop.width > window.innerWidth - margin) left = anchor.left - pop.width - margin;
    if (left < margin) left = margin;
    if (top + pop.height > window.innerHeight - margin) top = window.innerHeight - pop.height - margin;
    if (top < margin) top = margin;
    popover.style.left = left + 'px';
    popover.style.top = top + 'px';
  }
  function setRow(rowId, value) {
    const row = document.getElementById(rowId);
    if (!row) return;
    row.style.display = value ? '' : 'none';
  }
  document.getElementById('popover-close').addEventListener('click', () => popover.classList.add('hidden'));
  document.addEventListener('click', (e) => {
    if (popover.classList.contains('hidden')) return;
    if (popover.contains(e.target)) return;
    if (e.target.closest('.fc-event')) return;
    if (e.target.closest('.fc-list-event')) return;
    if (e.target.closest('.fc-more-link')) return;
    popover.classList.add('hidden');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') popover.classList.add('hidden');
  });
})();
