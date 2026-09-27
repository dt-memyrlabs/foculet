// Foculet Bridge — MV3 service worker.
//
// Long-polls the local Foculet bridge server (http://127.0.0.1:18721)
// for commands and executes the Chrome-side half of Foculet's tab
// parking: reporting window/tab state, tearing a tab off into its own
// window, and closing tabs. Nothing leaves the machine.

const BASE = "http://127.0.0.1:18721";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let polling = false;

async function run(cmd) {
  const a = cmd.args || {};
  switch (cmd.action) {
    case "chrome_state": {
      // one-shot windows + tabs state for Foculet's tab watcher
      const wins = await chrome.windows.getAll();
      const alltabs = await chrome.tabs.query({});
      return {
        data: {
          windows: wins.map((w) => ({
            id: w.id, left: w.left, top: w.top,
            width: w.width, height: w.height, focused: w.focused,
          })),
          tabs: alltabs.map((t) => ({
            id: t.id, windowId: t.windowId, active: t.active,
            title: t.title, url: t.url,
          })),
        },
      };
    }

    case "detach_tab": {
      // args: {tabId} — tears the tab into its own new window, state preserved.
      // Idempotent: if the tab is already alone in its window, returns it.
      const t = await chrome.tabs.get(a.tabId);
      const cur = await chrome.windows.get(t.windowId, { populate: true });
      if (cur.tabs.length === 1) {
        return { data: { windowId: cur.id, tabId: t.id, already: true } };
      }
      const w = await chrome.windows.create({ tabId: a.tabId });
      return { data: { windowId: w.id, tabId: a.tabId } };
    }

    case "close_tabs": {
      // args: {tabIds} — tolerant: already-closed tabs are just skipped
      const ids = a.tabIds || [];
      const existing = [];
      for (const id of ids) {
        try { await chrome.tabs.get(id); existing.push(id); } catch (e) {}
      }
      if (existing.length) await chrome.tabs.remove(existing);
      return { data: { closed: existing.length } };
    }

    default:
      return { ok: false, error: "unknown action: " + cmd.action };
  }
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    while (true) {
      try {
        const r = await fetch(BASE + "/poll");
        const cmd = await r.json();
        if (cmd && cmd.id) {
          let res;
          try {
            res = await run(cmd);
          } catch (e) {
            res = { ok: false, error: String(e) };
          }
          await fetch(BASE + "/result", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              id: cmd.id,
              ok: res.ok !== false,
              data: res.data,
              error: res.error,
            }),
          });
        }
      } catch (e) {
        await sleep(3000);
      }
    }
  } finally {
    polling = false;
  }
}

// MV3 service workers die when idle; the alarm keeps this one responsive.
chrome.alarms.onAlarm.addListener(() => poll());
chrome.runtime.onStartup.addListener(() => poll());
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("keepalive", { periodInMinutes: 1 });
  poll();
});
chrome.alarms.create("keepalive", { periodInMinutes: 1 });
