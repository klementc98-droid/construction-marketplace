/* ==========================================================================
   Construction's Finest — the assistant widget
   --------------------------------------------------------------------------
   Same posture as crew.js: no framework, no build step, opt-in from markup.
   Nothing here is load-bearing. If this file fails to parse, the launcher does
   nothing and every form in the app still fills in by hand — which is the only
   way any of them worked until now.

   Deliberately its own file rather than another section of crew.js: this is
   the one behaviour on the site that talks to a paid API, and keeping it
   separate means it is obvious what to remove if that ever stops being worth
   it.
   ========================================================================== */

(function () {
  "use strict";

  var doc = document;
  var panel = doc.querySelector("[data-assistant]");
  var launcher = doc.querySelector("[data-assistant-open]");
  if (!panel || !launcher) return;

  var els = {
    sub:     panel.querySelector("[data-asst-sub]"),
    choose:  panel.querySelector("[data-asst-choose]"),
    forms:   panel.querySelector("[data-asst-forms]"),
    chat:    panel.querySelector("[data-asst-chat]"),
    log:     panel.querySelector("[data-asst-log]"),
    form:    panel.querySelector("[data-asst-form]"),
    input:   panel.querySelector("[data-asst-input]"),
    send:    panel.querySelector("[data-asst-send]")
  };

  var busy = false;
  var loadedForms = false;

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

  /* The end of a form conversation: a real anchor into the prefilled form.
     An <a> rather than a button so it behaves like every other link on the
     site — long-press, open in a new tab, and a status bar showing where it
     goes. */
  function handoff(url) {
    var link = doc.createElement("a");
    link.className = "btn asst-handoff";
    link.href = url;
    link.textContent = "Open the filled-in form";
    els.log.appendChild(link);
    els.log.scrollTop = els.log.scrollHeight;
    link.focus();
  }

  function setBusy(state) {
    busy = state;
    els.input.disabled = state;
    els.send.disabled = state;
  }

  /* --- opening ----------------------------------------------------------- */

  function open() {
    panel.hidden = false;
    launcher.setAttribute("aria-expanded", "true");
    doc.body.classList.add("asst-open");
    loadForms();
  }

  function close() {
    panel.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
    doc.body.classList.remove("asst-open");
    post("/assistant/close/");
    reset();
  }

  function reset() {
    els.log.innerHTML = "";
    els.choose.hidden = false;
    els.chat.hidden = true;
    els.form.hidden = true;
    els.forms.hidden = true;
    els.sub.textContent = "How can I help?";
  }

  /* Which forms this user can actually be helped with depends on the profiles
     they hold, so the server decides and we render what comes back. */
  function loadForms() {
    if (loadedForms) return;
    loadedForms = true;
    fetch("/assistant/config/", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        (data.forms || []).forEach(function (form) {
          var button = doc.createElement("button");
          button.type = "button";
          button.className = "asst-form-pick";
          button.textContent = form.noun;
          button.addEventListener("click", function () { start("form", form.key); });
          els.forms.appendChild(button);
        });
        if (!(data.forms || []).length) {
          var note = doc.createElement("p");
          note.className = "asst-lede";
          note.textContent = "Add a profile first and I can help fill it in.";
          els.forms.appendChild(note);
        }
      })
      .catch(function () { loadedForms = false; });
  }

  /* --- the branch point -------------------------------------------------- */

  function start(branch, formKey) {
    setBusy(true);
    post("/assistant/start/", { branch: branch, form_key: formKey })
      .then(function (data) {
        els.choose.hidden = true;
        els.chat.hidden = false;
        els.form.hidden = false;
        els.sub.textContent =
          branch === "qa" ? "Questions about the app" : "Filling in a form";
        if (data.reply) bubble("bot", data.reply);
        els.input.focus();
      })
      .finally(function () { setBusy(false); });
  }

  function say(text) {
    bubble("user", text);
    var wait = thinking();
    setBusy(true);

    post("/assistant/say/", { text: text })
      .then(function (data) {
        wait.remove();
        bubble("bot", data.reply || "Sorry — something went wrong.");

        /* The handoff. The conversation is over; the real form takes it from
           here, and corrections happen there by typing into the fields.

           A link the user presses, not an automatic jump. The page moving on
           its own gives no moment to finish reading, and no way back if it
           moves at the wrong time — on a phone, mid-sentence, it just looks
           like the app lost their place. */
        if (data.redirect) {
          els.form.hidden = true;
          handoff(data.redirect);
        }
      })
      .catch(function () {
        wait.remove();
        bubble("bot", "Sorry — I couldn't reach the assistant. The form still works as normal.");
      })
      .finally(function () {
        setBusy(false);
        els.input.focus();
      });
  }

  /* --- wiring ------------------------------------------------------------ */

  launcher.addEventListener("click", function () {
    if (panel.hidden) open(); else close();
  });

  panel.querySelector("[data-asst-close]").addEventListener("click", close);

  panel.querySelector("[data-asst-branch='form']").addEventListener("click", function () {
    els.forms.hidden = false;
  });

  panel.querySelector("[data-asst-branch='qa']").addEventListener("click", function () {
    start("qa");
  });

  /* "Fill it in by chat", from a form page. The branch and the form are both
     already known — the user is standing on the form — so this opens straight
     into the conversation rather than replaying two questions they have just
     answered by navigating here. Still the same single component and the same
     one branch point; this is a shortcut through it, not a second entry. */
  Array.prototype.forEach.call(
    doc.querySelectorAll("[data-assistant-form]"),
    function (button) {
      button.addEventListener("click", function () {
        open();
        start("form", button.getAttribute("data-assistant-form"));
      });
    }
  );

  els.form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = els.input.value.trim();
    if (!text || busy) return;
    els.input.value = "";
    say(text);
  });

  doc.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !panel.hidden) close();
  });
})();
