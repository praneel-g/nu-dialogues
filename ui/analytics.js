// Anonymous usage counts through GoatCounter (https://www.goatcounter.com):
// no cookies and no personal data. Counts one visit per page load, plus
// events for the features people use. Does nothing until GOATCOUNTER_CODE
// is set, and GoatCounter itself ignores localhost and bots.
//
// Shares the GoatCounter site with nu-dining, so every path starts with
// "nu-dialogues/" to keep the two apart on the dashboard.
(() => {
  // The site code chosen when signing up: https://<code>.goatcounter.com
  const GOATCOUNTER_CODE = "praneel";
  const PREFIX = "nu-dialogues";
  const SCRIPT = {
    src: "https://gc.zgo.at/count.v5.js",
    integrity: "sha384-atnOLvQb9t+jTSipvd75X2yginT4PjVbqDdlJAmxMm+wYElFmeR6EmLP5bYeoRVQ",
  };

  const queue = []; // events sent before the script has loaded
  let ready = false;
  const send = count => (ready ? window.goatcounter.count(count) : queue.push(count));

  window.analytics = {
    /** Count an event, such as "apply/10830" or "filter/nupaths". */
    event(name, title) {
      if (!GOATCOUNTER_CODE) return;
      const path = `${PREFIX}/${name}`;
      send({ path, title: title || path, event: true });
    },
    /** Load GoatCounter and count this visit. */
    init() {
      if (!GOATCOUNTER_CODE) return;
      const note = document.getElementById("analytics-note");
      if (note) note.hidden = false;
      const script = document.createElement("script");
      script.async = true;
      script.src = SCRIPT.src;
      script.integrity = SCRIPT.integrity;
      script.crossOrigin = "anonymous";
      script.dataset.goatcounter = `https://${GOATCOUNTER_CODE}.goatcounter.com/count`;
      // Counted by hand below: filters live in the URL hash, and one page shouldn't split into many.
      script.dataset.goatcounterSettings = JSON.stringify({ no_onload: true });
      script.addEventListener("load", () => {
        ready = true;
        window.goatcounter.count({ path: `/${PREFIX}/`, title: document.title });
        for (const count of queue.splice(0)) window.goatcounter.count(count);
      });
      document.head.append(script);
    },
  };
})();
