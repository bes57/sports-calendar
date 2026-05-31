// Calendar UI logic
(function () {
  const leagueColor = {};
  (window.SPORTS_CAL.leagues || []).forEach(l => { leagueColor[l.id] = l.color; });

  const STORAGE_KEY = 'sports-cal-leagues';
  const VIEW_KEY = 'sports-cal-view';
  const TZ_KEY = 'sports-cal-tz';
  const COMPACT_KEY = 'sports-cal-compact';

  // Compact-grid toggle — squashes slots so 24h fit without scrolling.
  // Wiring the change handler happens AFTER the calendar is instantiated
  // (further down) so we can call calendar.updateSize() to make FC re-measure
  // slot heights and re-position events against them.
  const compactSaved = localStorage.getItem(COMPACT_KEY) === '1';
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
    threeDay:     'Today',
    timeGridDay:  'Today',
    listWeek:     'This Week',
    timeGridWeek: 'This Week',
    dayGridMonth: 'This Month',
  };
  // Pill mode disabled — every view uses FullCalendar defaults (full titles,
  // natural tile widths). Flip this back to a dayCount check if you want
  // pill mode in a specific view again.
  function isPillModeView() { return false; }

  function uniformizeWeekTiles() {
    // Always reset every harness first AND clear any leftover inline
    // display:none on title/time text (from older code paths).
    document.querySelectorAll('.fc-timegrid-event-harness').forEach(h => {
      h.style.width = '';
      h.style.maxWidth = '';
      h.style.insetInlineEnd = '';
      h.style.right = '';
    });
    document.querySelectorAll('.fc-event-title, .fc-event-time').forEach(el => {
      el.style.display = '';
    });
    // Update body attribute so CSS knows whether pill mode is on.
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
    // Always pass the FULL BUTTON_TEXT — FC's setOption('buttonText')
    // replaces the entire object, so a partial would lose Day/Agenda/etc.
    if (!window.__calendar) return;
    const todayLabel = TODAY_LABELS[viewType] || 'Today';
    window.__calendar.setOption('buttonText', { ...BUTTON_TEXT, today: todayLabel });
  }

  // Calendar — guard the saved view against renames so an obsolete value
  // (e.g. "fourDay" from a previous build) doesn't render blank.
  const VALID_VIEWS = ['threeDay', 'timeGridWeek', 'timeGridDay', 'dayGridMonth', 'listWeek', 'listDay'];
  const savedView = localStorage.getItem(VIEW_KEY);
  const initialView = VALID_VIEWS.includes(savedView) ? savedView : 'threeDay';

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
      right: 'timeGridDay,listWeek,threeDay,timeGridWeek,dayGridMonth'
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
    eventMinHeight: 6,   // let very short events (wrap tails, 10-min slivers)
                         // render at their true proportional height instead
                         // of being padded out to look like longer ones
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
        noEventsContent: 'No events in this week.'
      },
      listDay: {
        listDayFormat: { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' },
        listDaySideFormat: false,
        noEventsContent: 'No events today.'
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
      info.jsEvent.stopPropagation();
      showPopover(info.event, info.el);
    },
    // In month view, clicking an empty area of a day cell zooms into that day.
    // The day-number nav-link (navLinks: true) provides the same shortcut.
    dateClick: (info) => {
      if (info.view.type === 'dayGridMonth') {
        calendar.changeView('timeGridDay', info.date);
      }
    },
    navLinks: true,
    navLinkDayClick: 'timeGridDay',
    eventDidMount: (info) => {
      const ep = info.event.extendedProps || {};
      const bg = info.event.backgroundColor;
      const view = info.view.type;

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
      scheduleUniformizeWeek();
    },
    eventsSet: () => {
      scheduleUniformizeWeek();
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
  // Clean slot durations to try, in MINUTES. We pick the SMALLEST one whose
  // total natural height fits the available viewport.
  const CLEAN_DURATIONS_MIN    = [15, 30, 60, 90, 120, 180, 240];

  let calendar;  // will be assigned by mountCalendar()

  function mountCalendar(compactMode, preserveDate) {
    const slotDur = compactMode ? pickCompactSlotDuration() : NORMAL_SLOT_DURATION;
    const slotLabelInt = compactMode ? slotDur : NORMAL_LABEL_INTERVAL;
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
  function pickCompactSlotDuration() {
    const calEl = document.getElementById('calendar');
    if (!calEl) return '02:00:00';
    const calTop = calEl.getBoundingClientRect().top;
    const avail = Math.max(200, window.innerHeight - calTop - COMPACT_FC_CHROME_PX - 16);
    // Find the smallest clean duration such that numSlots * naturalHeight <= avail.
    for (const min of CLEAN_DURATIONS_MIN) {
      const numSlots = 1440 / min;
      const totalNeeded = numSlots * NATURAL_SLOT_PX;
      if (totalNeeded <= avail) {
        const hh = String(Math.floor(min / 60)).padStart(2, '0');
        const mm = String(min % 60).padStart(2, '0');
        return `${hh}:${mm}:00`;
      }
    }
    return '04:00:00';  // ultra-tiny viewport — 4h slots, 6 of them
  }

  function applyCompact(on) {
    document.body.classList.toggle('compact-grid', on);
    localStorage.setItem(COMPACT_KEY, on ? '1' : '0');
    mountCalendar(on, /*preserveDate=*/true);
  }

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

  // League filter wiring
  document.querySelectorAll('.league-check').forEach(c => {
    c.addEventListener('change', () => {
      writeActive(currentActive());
      calendar.refetchEvents();
    });
  });
  document.getElementById('all-leagues').addEventListener('click', () => {
    document.querySelectorAll('.league-check').forEach(c => c.checked = true);
    writeActive(currentActive());
    calendar.refetchEvents();
  });
  document.getElementById('no-leagues').addEventListener('click', () => {
    document.querySelectorAll('.league-check').forEach(c => c.checked = false);
    writeActive(currentActive());
    calendar.refetchEvents();
  });

  // Refresh button
  const refreshBtn = document.getElementById('refresh-btn');
  const refreshStatus = document.getElementById('refresh-status');
  refreshBtn.addEventListener('click', async () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Refreshing...';
    refreshStatus.textContent = '';
    try {
      const res = await fetch('/api/refresh/sync', { method: 'POST' });
      const j = await res.json();
      refreshStatus.textContent = `Fetched ${j.total} events — reloading...`;
      // Cache-busting hard reload so any updated CSS/JS comes through too
      const url = new URL(window.location.href);
      url.searchParams.set('_', Date.now().toString());
      window.location.replace(url.toString());
    } catch (e) {
      refreshStatus.textContent = 'Error: ' + e.message;
      refreshBtn.disabled = false;
      refreshBtn.textContent = 'Refresh data';
    }
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
