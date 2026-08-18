/* ==========================================================================
   Construction's Finest — front-end behaviour
   --------------------------------------------------------------------------
   Progressive enhancement only. Every feature here is an improvement on a page
   that already works without it: the feed paginates by link, the check-in form
   posts without coordinates, the theme falls back to the OS setting. Nothing
   is required for the app to function, so a JS error costs polish and never a
   job.

   No framework and no build step. Each behaviour is opt-in from the markup
   through a `data-` attribute, so a template that does not ask for one does
   not pay for it.
   ========================================================================== */

(function () {
  "use strict";

  var doc = document;
  var root = doc.documentElement;

  /* Small helpers — enough to avoid repeating querySelectorAll plumbing. */
  function $(sel, ctx) { return (ctx || doc).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || doc).querySelectorAll(sel)); }
  function on(el, evt, fn, opts) { if (el) el.addEventListener(evt, fn, opts); }

  var reducedMotion = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false };


  /* ======================================================================
     Theme
     ----------------------------------------------------------------------
     The stored value is only ever "light" or "dark"; removing it hands
     control back to the OS. The initial paint is handled by a tiny inline
     script in <head> — by the time this file runs the page is already
     visible, so setting the attribute here would flash.
     ====================================================================== */

  var THEME_KEY = "crew-theme";

  /* Three themes, cycled in this order by the one button. Luxe is near-black
     and gold and is a deliberate choice rather than an OS-derived one, so it
     is never what an untouched toggle resolves to — only a press reaches it. */
  var THEMES = ["light", "dark", "luxe"];

  /* Which browser colour-scheme each one is. Luxe is a dark theme wearing
     different paint; telling the browser "luxe" would mean telling it nothing
     and form controls would come back white. */
  var SCHEME = { light: "light", dark: "dark", luxe: "dark" };

  function storedTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
  }

  function activeTheme() {
    var chosen = root.getAttribute("data-theme");
    if (chosen) return chosen;
    return window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  /* `persist` is deliberately opt-in. The inline bootstrap sets data-theme on
     every load, so persisting here would silently pin the first resolved value
     and the page would stop following the OS for someone who never touched the
     toggle. Only an actual press is a choice. */
  function applyTheme(theme, persist) {
    root.setAttribute("data-theme", theme);
    root.style.colorScheme = SCHEME[theme] || theme;

    if (persist) {
      try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* private mode */ }
    }

    // Keep the browser chrome (iOS status bar, Android address bar) in step.
    var meta = $('meta[name="theme-color"]');
    if (meta && doc.body) {
      var bg = getComputedStyle(doc.body).getPropertyValue("--bg").trim();
      if (bg) meta.setAttribute("content", bg);
    }

    $$("[data-theme-toggle]").forEach(function (btn) {
      var next = nextTheme(theme);
      btn.setAttribute("aria-label", "Switch to " + next + " theme");
      btn.setAttribute("title", "Switch to " + next + " theme");
    });
  }

  /* An unrecognised value — an old key, a hand-edited one — lands on light
     rather than throwing, which is the same answer a first-time visitor gets. */
  function nextTheme(theme) {
    var i = THEMES.indexOf(theme);
    return THEMES[(i + 1) % THEMES.length] || THEMES[0];
  }

  function initTheme() {
    var toggles = $$("[data-theme-toggle]");
    if (!toggles.length) return;

    applyTheme(activeTheme(), false);

    toggles.forEach(function (btn) {
      on(btn, "click", function () {
        applyTheme(nextTheme(activeTheme()), true);
      });
    });

    // Follow the OS while the reader has not overridden it.
    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var follow = function () {
        if (!storedTheme()) applyTheme(mq.matches ? "dark" : "light", false);
      };
      if (mq.addEventListener) mq.addEventListener("change", follow);
      else if (mq.addListener) mq.addListener(follow);
    }
  }


  /* ======================================================================
     Header
     ----------------------------------------------------------------------
     The header's bottom rule only appears once there is content behind it,
     so the top of a page reads as one uninterrupted surface.
     ====================================================================== */

  function initHeader() {
    var header = $("header.top");
    if (!header) return;

    var ticking = false;
    var update = function () {
      header.classList.toggle("is-scrolled", window.scrollY > 4);
      ticking = false;
    };

    on(window, "scroll", function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(update);
    }, { passive: true });

    update();
  }


  /* ======================================================================
     Flash messages
     ----------------------------------------------------------------------
     Confirmations are noise once read, so they retire themselves. Errors and
     warnings stay until dismissed — those are the ones somebody needs to act
     on, and a message that vanishes mid-sentence is worse than none.
     ====================================================================== */

  function dismiss(msg) {
    msg.classList.add("is-leaving");
    window.setTimeout(function () { msg.remove(); }, reducedMotion.matches ? 0 : 200);
  }

  function wireMessage(msg) {
    if (!$(".msg-close", msg)) {
      var button = doc.createElement("button");
      button.type = "button";
      button.className = "msg-close";
      button.setAttribute("aria-label", "Dismiss");
      button.innerHTML = "&times;";
      on(button, "click", function () { dismiss(msg); });
      msg.appendChild(button);
    }

    if (msg.classList.contains("success")) {
      var timer = window.setTimeout(function () { dismiss(msg); }, 6000);
      // Reading it should not start a countdown you can lose.
      on(msg, "mouseenter", function () { window.clearTimeout(timer); });
      on(msg, "focusin", function () { window.clearTimeout(timer); });
    }
  }

  function initMessages() {
    $$(".msg").forEach(wireMessage);
  }

  /* Raise a banner from the client side, in the same place and the same shape
     as the ones Django renders. Used when something fails without a page load
     — going offline mid-submit — so a failure there looks like every other
     failure the reader has seen, rather than an alert() from another app.

     Inserted into the same aria-live region the server's messages use where
     there is one, so it is announced without a second live region competing. */
  function notify(text, kind) {
    var main = $("main.wrap");
    if (!main) return;

    var region = $('[role="status"]', main);
    if (!region) {
      region = doc.createElement("div");
      region.setAttribute("role", "status");
      region.setAttribute("aria-live", "polite");
      main.insertBefore(region, main.firstChild);
    }

    var msg = doc.createElement("div");
    msg.className = "msg " + (kind || "info");
    msg.textContent = text;
    region.appendChild(msg);
    wireMessage(msg);
    msg.scrollIntoView({ block: "nearest" });
  }


  /* ======================================================================
     Submit guard
     ----------------------------------------------------------------------
     Funding escrow, sending an application and awarding a job are all
     single-shot POSTs. A double tap on a slow connection is the expensive
     kind of mistake, so the button locks and says it is working.

     Opt out with data-no-guard on the form (the check-in form does, because
     it re-submits itself once geolocation resolves).
     ====================================================================== */

  function initSubmitGuard() {
    $$("form").forEach(function (form) {
      if (form.hasAttribute("data-no-guard")) return;

      on(form, "submit", function (event) {
        // Leave invalid forms alone — the browser is about to block this and
        // the user still has to press the button again.
        if (form.checkValidity && !form.checkValidity()) return;

        // Offline is the one failure worth catching before it happens. Letting
        // it through gives the browser's own "can't reach this site" page,
        // which loses everything typed into the form. Stopping here keeps the
        // page — and everything on it — exactly where it was.
        if (navigator.onLine === false) {
          event.preventDefault();
          notify(
            "You're offline — nothing was sent. Your answers are still here; " +
            "try again once you're back on a connection.",
            "error"
          );
          return;
        }

        window.setTimeout(function () {
          $$('button[type="submit"], .btn[type="submit"]', form).forEach(function (btn) {
            if (btn.classList.contains("danger")) return;   // destructive: stay legible
            btn.classList.add("is-busy");

            // "Saving…" beside the spinner, where the template says what the
            // verb should be. Skipped for a button holding an icon, since
            // setting textContent would delete the icon along with the words.
            var busy = btn.getAttribute("data-busy-label");
            if (busy && !btn.children.length) btn.textContent = busy;
          });
        }, 0);
      });
    });
  }



  /* ======================================================================
     Endless feed
     ----------------------------------------------------------------------
     The sentinel at the bottom of each batch carries the next page number;
     when it comes into view we fetch that page's rows and swap it for them.
     The server renders identical markup either way, so a card looks the same
     whether it arrived with the page or afterwards — no client-side
     templating and no state to keep in sync.
     ====================================================================== */

  function initFeed() {
    var feed = $("[data-feed]");
    if (!feed || !("IntersectionObserver" in window) || !("fetch" in window)) return;

    var loading = false;

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) load(entry.target);
      });
    }, { rootMargin: "700px 0px" });   // start early so it feels seamless

    function watch() {
      var sentinel = $(".feed-sentinel", feed);
      if (sentinel) observer.observe(sentinel);
    }

    function load(sentinel) {
      if (loading) return;
      loading = true;
      observer.unobserve(sentinel);

      var url = new URL(window.location.href);
      url.searchParams.set("page", sentinel.dataset.next);
      url.searchParams.set("partial", "1");

      fetch(url.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (response) {
          if (!response.ok) throw new Error(response.status);
          return response.text();
        })
        .then(function (html) {
          sentinel.insertAdjacentHTML("beforebegin", html);
          sentinel.remove();
          loading = false;
          watch();
        })
        .catch(function () {
          // Leave a way back rather than a dead page: the skeletons become a
          // button the reader can press.
          loading = false;
          sentinel.innerHTML =
            '<button class="btn secondary" type="button">Couldn’t load more — retry</button>';
          on($("button", sentinel), "click", function () {
            sentinel.innerHTML =
              '<div class="feed-sentinel-note"><span class="spinner"></span>' +
              '<span class="muted">Loading…</span></div>';
            load(sentinel);
          });
        });
    }

    watch();
  }


  /* ======================================================================
     Check-in location
     ----------------------------------------------------------------------
     Best-effort and never blocking: if the browser refuses, times out, or the
     worker declines, the form submits without coordinates and the check-in
     still counts. Location is a rough corroboration, not a gate.
     ====================================================================== */

  function initGeoForms() {
    $$("form[data-geo]").forEach(function (form) {
      if (!navigator.geolocation) return;

      var sent = false;

      on(form, "submit", function (event) {
        if (sent) return;
        event.preventDefault();

        var button = $('button[type="submit"]', form);
        if (button) button.classList.add("is-busy");

        var go = function () { sent = true; form.submit(); };
        var timer = window.setTimeout(go, 4000);   // hard ceiling on the wait

        var put = function (id, value) {
          var field = doc.getElementById(id);
          if (field) field.value = value;
        };

        navigator.geolocation.getCurrentPosition(
          function (pos) {
            window.clearTimeout(timer);
            put("lat", pos.coords.latitude);
            put("lng", pos.coords.longitude);
            put("acc", Math.round(pos.coords.accuracy || 0));
            go();
          },
          function () { window.clearTimeout(timer); go(); },
          { enableHighAccuracy: true, timeout: 3500, maximumAge: 60000 }
        );
      });
    });
  }


  /* ======================================================================
     Confirm destructive actions
     ----------------------------------------------------------------------
     Cancelling a job or withdrawing an application cannot be undone from the
     UI. One question, phrased in the template.
     ====================================================================== */

  function initConfirms() {
    $$("[data-confirm]").forEach(function (el) {
      on(el, "click", function (event) {
        if (!window.confirm(el.getAttribute("data-confirm"))) {
          event.preventDefault();
          event.stopPropagation();
        }
      });
    });
  }


  /* ======================================================================
     Date lists
     ----------------------------------------------------------------------
     A set of days is stored as one comma-separated string, and typing
     "2026-08-04, 2026-08-11" by hand is a chore that also invites typos. This
     replaces that input with a calendar the reader taps days on, plus a row of
     removable chips showing what is picked.

     Drawn here rather than handed to <input type="date"> because a native
     picker closes on the first choice. Booking somebody for three days would
     be three rounds of open-pick-close, which is exactly the friction this
     field exists to remove. The panel stays open until it is dismissed.

     The original input is never replaced, only hidden — it stays the value
     that submits, so the server parses exactly what it always did. With the
     script off it is still a perfectly usable text box, which is why the
     field keeps its placeholder and its help text.

     `data-date-list` carries the server's today, in the app's timezone. Using
     the browser's clock instead would let someone on a device set to
     yesterday offer a day the server will reject.
     ====================================================================== */

  function initDateLists() {
    $$("input[data-date-list]").forEach(function (input) {
      var floor = input.getAttribute("data-date-list") || "";
      var lang = doc.documentElement.getAttribute("lang") || undefined;
      /* One day rather than a set — a counter-offer moves the date, it does
         not collect dates. Same calendar, two rules changed: picking replaces,
         and the panel closes on the pick. */
      var one = input.hasAttribute("data-date-one");

      var words = {};
      try {
        words = JSON.parse(input.getAttribute("data-date-list-i18n") || "{}");
      } catch (e) { /* fall through to the defaults below */ }
      function word(key, fallback) { return words[key] || fallback; }

      /* --- structure --- */

      var wrap = doc.createElement("div");
      wrap.className = "datecal";

      var chips = doc.createElement("div");
      chips.className = "datelist-chips";

      var toggle = doc.createElement("button");
      toggle.type = "button";
      toggle.className = "datecal-toggle";
      toggle.setAttribute("aria-expanded", "false");

      var panel = doc.createElement("div");
      panel.className = "datecal-panel";
      panel.hidden = true;

      var head = doc.createElement("div");
      head.className = "datecal-head";
      var prev = doc.createElement("button");
      prev.type = "button";
      prev.className = "datecal-nav";
      prev.innerHTML = "&lsaquo;";
      prev.setAttribute("aria-label", word("prev", "Previous month"));
      var title = doc.createElement("strong");
      title.className = "datecal-month";
      title.setAttribute("aria-live", "polite");
      var next = doc.createElement("button");
      next.type = "button";
      next.className = "datecal-nav";
      next.innerHTML = "&rsaquo;";
      next.setAttribute("aria-label", word("next", "Next month"));
      head.appendChild(prev);
      head.appendChild(title);
      head.appendChild(next);

      var week = doc.createElement("div");
      week.className = "datecal-week";

      var grid = doc.createElement("div");
      grid.className = "datecal-grid";

      var foot = doc.createElement("div");
      foot.className = "datecal-foot";
      var done = doc.createElement("button");
      done.type = "button";
      done.className = "btn datecal-done";
      done.textContent = word("done", "Done");
      foot.appendChild(done);

      panel.appendChild(head);
      panel.appendChild(week);
      panel.appendChild(grid);
      panel.appendChild(foot);

      // The field's own label points at the input we are about to hide, so it
      // moves to the control that replaces it — otherwise clicking "Which
      // days?" focuses something nobody can see.
      var lbl = input.id ? $('label[for="' + input.id + '"]') : null;
      if (lbl) {
        toggle.id = input.id + "-toggle";
        lbl.setAttribute("for", toggle.id);
      } else {
        toggle.setAttribute("aria-label", word("open", "Pick days"));
      }

      input.classList.add("sr-only");
      input.setAttribute("tabindex", "-1");
      input.setAttribute("aria-hidden", "true");
      input.parentNode.insertBefore(wrap, input.nextSibling);
      wrap.appendChild(chips);
      wrap.appendChild(toggle);
      wrap.appendChild(panel);

      /* --- the value --- */

      function read() {
        return input.value
          .split(",")
          .map(function (s) { return s.trim(); })
          .filter(function (s) { return s && (!floor || s >= floor); })
          .filter(function (s, i, all) { return all.indexOf(s) === i; })
          .sort();
      }

      function write(list) {
        input.value = list.join(", ");
        renderChips();
        if (!panel.hidden) drawGrid();
      }

      function iso(d) {
        var m = d.getMonth() + 1;
        var day = d.getDate();
        return d.getFullYear() + "-" + (m < 10 ? "0" : "") + m + "-" + (day < 10 ? "0" : "") + day;
      }

      function label(value) {
        var parts = value.split("-");
        // Constructed from parts rather than `new Date(iso)`, which parses a
        // bare ISO date as UTC and can render the day before in the Americas.
        var d = new Date(+parts[0], +parts[1] - 1, +parts[2]);
        return isNaN(d) ? value
          : d.toLocaleDateString(lang, { weekday: "short", day: "numeric", month: "short" });
      }

      /* --- the calendar --- */

      // First of the month currently on screen. Starts on the month holding
      // the earliest picked day, so reopening the panel returns to where the
      // work was, not to today.
      var picked = read();
      var view = new Date();
      if (picked.length) {
        var p = picked[0].split("-");
        view = new Date(+p[0], +p[1] - 1, 1);
      } else {
        view = new Date(view.getFullYear(), view.getMonth(), 1);
      }

      function weekdayNames() {
        // Monday first. Derived from a known Monday so the names follow the
        // page's language rather than being hardcoded English.
        var names = [];
        var base = new Date(2024, 0, 1);          // a Monday
        for (var i = 0; i < 7; i++) {
          var d = new Date(base.getFullYear(), base.getMonth(), base.getDate() + i);
          names.push(d.toLocaleDateString(lang, { weekday: "narrow" }));
        }
        return names;
      }

      function drawWeekdays() {
        week.textContent = "";
        weekdayNames().forEach(function (name) {
          var cell = doc.createElement("span");
          cell.textContent = name;
          week.appendChild(cell);
        });
      }

      function drawGrid() {
        grid.textContent = "";
        title.textContent = view.toLocaleDateString(lang, {
          month: "long", year: "numeric"
        });

        var chosen = read();
        var first = new Date(view.getFullYear(), view.getMonth(), 1);
        // getDay() is Sunday-based; shift so Monday is column 0.
        var lead = (first.getDay() + 6) % 7;
        var days = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();

        for (var b = 0; b < lead; b++) {
          grid.appendChild(doc.createElement("span"));
        }

        for (var n = 1; n <= days; n++) {
          var date = new Date(view.getFullYear(), view.getMonth(), n);
          var value = iso(date);
          var cell = doc.createElement("button");
          cell.type = "button";              // never submits the form it sits in
          cell.className = "datecal-day";
          cell.textContent = String(n);
          cell.dataset.date = value;

          if (floor && value < floor) {
            cell.disabled = true;
          } else {
            var isOn = chosen.indexOf(value) !== -1;
            cell.setAttribute("aria-pressed", isOn ? "true" : "false");
            if (isOn) cell.classList.add("on");
          }
          if (value === iso(new Date())) cell.classList.add("today");
          grid.appendChild(cell);
        }
      }

      // One listener for the whole grid rather than one per cell: the grid is
      // redrawn on every pick, and re-binding forty buttons each time is how a
      // calendar starts feeling slow on a phone.
      on(grid, "click", function (event) {
        var cell = event.target.closest ? event.target.closest(".datecal-day") : null;
        if (!cell || cell.disabled) return;
        var value = cell.dataset.date;

        if (one) {
          // Replace, never toggle. Re-tapping the chosen day is a confirmation,
          // not a request to leave the field empty — the chip's x is how it is
          // cleared, the same as a native date input, which also has no way to
          // unset by tapping the day again.
          var prevOn = $(".datecal-day.on", grid);
          if (prevOn && prevOn !== cell) {
            prevOn.classList.remove("on");
            prevOn.setAttribute("aria-pressed", "false");
          }
          cell.classList.add("on");
          cell.setAttribute("aria-pressed", "true");
          input.value = value;
          renderChips();
          close();
          toggle.focus();
          return;
        }

        var list = read();
        var at = list.indexOf(value);
        var isOn = at === -1;
        if (isOn) list.push(value); else list.splice(at, 1);

        // Toggled in place, deliberately not by redrawing the grid. Redrawing
        // replaces the button being clicked while its own click is still
        // bubbling, and a detached node is inside nothing — so the
        // outside-click handler below saw it as a click outside the calendar
        // and shut the panel on the first date. Which is precisely the
        // behaviour this widget exists to get rid of.
        cell.classList.toggle("on", isOn);
        cell.setAttribute("aria-pressed", isOn ? "true" : "false");

        input.value = list.sort().join(", ");
        renderChips();
        // Deliberately no close here. Booking three days should be three taps,
        // not three rounds of open-pick-close.
      });

      on(prev, "click", function () {
        view = new Date(view.getFullYear(), view.getMonth() - 1, 1);
        drawGrid();
      });
      on(next, "click", function () {
        view = new Date(view.getFullYear(), view.getMonth() + 1, 1);
        drawGrid();
      });

      /* --- open and close --- */

      function open() {
        panel.hidden = false;
        toggle.setAttribute("aria-expanded", "true");
        drawGrid();
      }

      function close() {
        panel.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
      }

      on(toggle, "click", function () {
        if (panel.hidden) open(); else close();
      });
      on(done, "click", function () { close(); toggle.focus(); });

      on(doc, "click", function (event) {
        if (panel.hidden) return;
        // Two ways a click is "not outside". The obvious one is that it landed
        // inside the calendar. The other is that it landed on a node we have
        // since thrown away: picking a day redraws the grid, so by the time
        // the click reaches the document the button that was tapped has been
        // replaced and is no longer in `wrap` — or anywhere in the document.
        // Reading that as an outside click is what made the panel shut on the
        // first date, which is the whole thing this widget exists to avoid.
        if (wrap.contains(event.target)) return;
        if (!doc.contains(event.target)) return;
        close();
      });
      on(doc, "keydown", function (event) {
        if (event.key === "Escape" && !panel.hidden) { close(); toggle.focus(); }
      });

      /* --- chips and the button's own label --- */

      function renderChips() {
        var list = read();

        chips.textContent = "";
        if (!list.length) {
          var none = doc.createElement("span");
          none.className = "datelist-none";
          none.textContent = word("none", "No days picked yet.");
          chips.appendChild(none);
        } else {
          list.forEach(function (value) {
            var chip = doc.createElement("button");
            chip.type = "button";
            chip.className = "datelist-chip";
            chip.innerHTML = "<span></span><i aria-hidden=\"true\">&times;</i>";
            $("span", chip).textContent = label(value);
            chip.setAttribute(
              "aria-label",
              word("remove", "Remove") + " " + label(value)
            );
            on(chip, "click", function () {
              write(read().filter(function (d) { return d !== value; }));
            });
            chips.appendChild(chip);
          });
        }

        // The count is the useful half of this label when there is a set to
        // keep track of, and noise when there is exactly one day by design.
        toggle.textContent = list.length
          ? word("more", "Add or remove days") + (one ? "" : " (" + list.length + ")")
          : word("open", "Pick days");
      }

      drawWeekdays();
      renderChips();
    });
  }


  /* ======================================================================
     Boot
     ====================================================================== */

  function boot() {
    initTheme();
    initHeader();
    initMessages();
    initSubmitGuard();
    initFeed();
    initGeoForms();
    initConfirms();
    initDateLists();
  }

  if (doc.readyState === "loading") on(doc, "DOMContentLoaded", boot);
  else boot();
})();
