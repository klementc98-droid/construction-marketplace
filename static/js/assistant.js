/* ==========================================================================
   XTISE — the assistant widget
   --------------------------------------------------------------------------
   Same posture as crew.js: no framework, no build step, opt-in from markup.
   Nothing here is load-bearing. If this file fails to parse, the launcher does
   nothing and the rest of the app is untouched — the assistant answers
   questions, it does not do anything on anyone's behalf.

   Deliberately its own file rather than another section of crew.js: this is
   the one behaviour on the site that talks to a paid API, and keeping it
   separate means it is obvious what to remove if that ever stops being worth
   it.

   It used to have a second job — walking somebody through a form and handing
   the answers to the real one. That is gone; forms ask one question per screen
   themselves now. What is left is the part a form cannot do.
   ========================================================================== */

(function () {
  "use strict";

  var doc = document;
  var panel = doc.querySelector("[data-assistant]");
  var launcher = doc.querySelector("[data-assistant-open]");
  if (!panel || !launcher) return;

  var els = {
    chat:  panel.querySelector("[data-asst-chat]"),
    log:   panel.querySelector("[data-asst-log]"),
    form:  panel.querySelector("[data-asst-form]"),
    input: panel.querySelector("[data-asst-input]"),
    send:  panel.querySelector("[data-asst-send]")
  };

  var busy = false;
  var open = false;

  /* --- plumbing ---------------------------------------------------------- */

  function csrf() {
    var match = doc.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().catch(function () { return {}; });
    });
  }

  /* --- rendering --------------------------------------------------------- */

  /* textContent, never innerHTML. The strings here come back from a language
     model that is repeating things a user typed, which is exactly the shape of
     input that must never be parsed as markup. */
  function bubble(role, text) {
    var row = doc.createElement("div");
    row.className = "asst-msg asst-" + role;
    row.textContent = text;
    els.log.appendChild(row);
    els.log.scrollTop = els.log.scrollHeight;
    return row;
  }

  function thinking() {
    var row = bubble("bot", "");
    row.classList.add("asst-wait");
    row.setAttribute("aria-label", "Thinking");
    row.innerHTML = "<i></i><i></i><i></i>";
    return row;
  }

  /* The starter questions, for somebody who does not know what to ask.

     Only ever one set on screen: pressing one, or typing instead, clears them.
     Leaving a stale row above the newest answer is how someone ends up
     pressing a question that has already been answered. */
  function clearOptions() {
    var old = els.log.querySelector(".asst-opts");
    if (old) old.remove();
  }

  function renderOptions(list) {
    clearOptions();
    if (!list || !list.length) return;

    var wrap = doc.createElement("div");
    wrap.className = "asst-opts";

    list.forEach(function (option) {
      var chip = doc.createElement("button");
      chip.type = "button";
      chip.className = "asst-opt";
      chip.textContent = option.label || option.value;
      chip.addEventListener("click", function () {
        if (busy) return;
        clearOptions();
        say(option.value);
      });
      wrap.appendChild(chip);
    });

    els.log.appendChild(wrap);
    els.log.scrollTop = els.log.scrollHeight;
  }

  function setBusy(state) {
    busy = state;
    els.input.disabled = state;
    els.send.disabled = state;
  }

  /* --- opening ----------------------------------------------------------- */

  /* Opening starts the conversation. There is nothing to choose first: a menu
     with one item on it is a menu that exists to be got past. */
  function show() {
    panel.hidden = false;
    open = true;
    launcher.setAttribute("aria-expanded", "true");
    doc.body.classList.add("asst-open");

    setBusy(true);
    post("/assistant/start/")
      .then(function (data) {
        els.chat.hidden = false;
        els.form.hidden = false;
        if (data.reply) bubble("bot", data.reply);
        renderOptions(data.options);
        els.input.focus();
      })
      .finally(function () { setBusy(false); });
  }

  function hide() {
    panel.hidden = true;
    open = false;
    launcher.setAttribute("aria-expanded", "false");
    doc.body.classList.remove("asst-open");
    post("/assistant/close/");
    els.log.innerHTML = "";
    els.chat.hidden = true;
    els.form.hidden = true;
  }

  /* --- one turn ---------------------------------------------------------- */

  function say(text) {
    clearOptions();
    bubble("user", text);
    var wait = thinking();
    setBusy(true);

    post("/assistant/say/", { text: text })
      .then(function (data) {
        wait.remove();
        bubble("bot", data.reply || "Sorry — something went wrong.");
        renderOptions(data.options);
      })
      .catch(function () {
        wait.remove();
        bubble("bot", "Sorry — I couldn't reach the assistant just now.");
      })
      .finally(function () {
        setBusy(false);
        els.input.focus();
      });
  }

  /* --- wiring ------------------------------------------------------------ */

  launcher.addEventListener("click", function () {
    if (open) hide(); else show();
  });

  panel.querySelector("[data-asst-close]").addEventListener("click", hide);

  els.form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = els.input.value.trim();
    if (!text || busy) return;
    els.input.value = "";
    say(text);
  });

  doc.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && open) hide();
  });
})();
