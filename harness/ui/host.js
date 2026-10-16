// Runs inside a module's Worker. A module is `{spec, css, update(msg) -> out}`; every message in and out is a plain JSON
// document. The host feeds it batches and merges the results, so one worker round trip covers any burst of events.
// Out document: {view?, calls?: [{id, method, path, body?}], streams?: [{id, path} | {id, close: true}],
//                emit?: [{to, msg}], focus?: moduleId, toast?: text}
export function merge(outs) {
  const o = {};
  for (const r of outs) {
    if (!r || typeof r !== "object") continue;
    if (r.view !== undefined) o.view = r.view;
    for (const k of ["calls", "streams", "emit"]) if (Array.isArray(r[k]) && r[k].length) (o[k] ||= []).push(...r[k]);
    if (r.focus) o.focus = r.focus;
    if (r.toast) o.toast = r.toast;
  }
  return o;
}

export function runBatch(mod, msgs) {
  const outs = [];
  for (const m of msgs) {
    try { outs.push(mod.update(m)); } catch (e) { outs.push({ toast: mod.spec.id + ": " + e.message }); }
  }
  return merge(outs);
}
