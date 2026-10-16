// Generic module host. The engine starts one of these per module, so a slow module can never stall the page or the event feed.
import { runBatch } from "./host.js";

let mod = null;
const ready = new Promise((res) => { self.__ready = res; });

self.onmessage = async (e) => {
  const m = e.data;
  if (m.type === "load") {
    mod = await import(m.url);
    self.postMessage({ type: "ready", spec: mod.spec, css: mod.css || "" });
    self.__ready();
    return;
  }
  await ready;
  if (m.type === "batch") self.postMessage({ type: "out", out: runBatch(mod, m.msgs) });
};
