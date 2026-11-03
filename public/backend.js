/* Adapter: bildet die claude.ai-Laufzeit (window.claude.use) nach, die index.html
   erwartet – aber gegen das eigene Backend (server.py).
   - db:        doc(path).set/update/delete/onSnapshot, collection("claims").onSnapshot
                Live-Updates per Polling von /api/state alle 3 s
   - user:      id(), canEdit() (= Admin), can(), profiles(), Anmeldung und Kontenverwaltung
   - assets:    upload(file) – nur für Admins
   - downloads: save({filename, data})
   Anmeldung: HttpOnly-Cookie vom Server, die Seite sieht das Token nie. */
(() => {
  async function call(method, path, body, extraHeaders) {
    let r;
    try {
      r = await fetch(path, {
        method, headers: body instanceof Blob ? extraHeaders : { "Content-Type": "application/json", ...extraHeaders },
        body: body === undefined ? undefined : body instanceof Blob ? body : JSON.stringify(body),
      });
    } catch (e) {
      const err = new Error("Server nicht erreichbar."); err.code = "network"; err.serverMsg = err.message; throw err;
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { const err = new Error(j.error || "Fehler"); err.code = j.code || "error"; err.serverMsg = j.error; throw err; }
    return j;
  }

  // --- Zustand + Polling
  let state = null, version = null, polling = null;
  const subs = new Set();
  const snapDoc = (coll, id) => {
    const data = coll === "settings" ? (id === "main" ? state.settings : undefined) : state.claims[id];
    return { id, exists: data !== undefined, data: () => data, metadata: { fromCache: false } };
  };
  function emit() { for (const f of subs) { try { f(); } catch (e) { console.error(e); } } }
  async function poll() {
    try {
      const j = await call("GET", "/api/state" + (version !== null ? "?v=" + version : ""));
      if (!j.unchanged) { state = j; version = j.version; emit(); }
    } catch (e) {
      if (e.code === "login") return location.reload();   // Anmeldung abgelaufen oder Konto gesperrt
      for (const f of subs) f.err && f.err(e);
    }
  }
  function startPolling() {
    if (polling) return;
    poll(); polling = setInterval(poll, 3000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  }
  const refresh = () => { version = null; return poll(); };

  function docRef(path) {
    const [coll, id] = path.split("/");
    const api = "/api/doc/" + path;
    return {
      async get() { if (!state) await poll(); return snapDoc(coll, id); },
      async set(d) { await call("PUT", api, d); await refresh(); },
      async update(d) { await call("PATCH", api, d); await refresh(); },
      async delete() { await call("DELETE", api); await refresh(); },
      onSnapshot(next, onErr) {
        const f = () => state && next(snapDoc(coll, id)); f.err = onErr;
        subs.add(f); if (state) setTimeout(f, 0); startPolling();
        return () => subs.delete(f);
      },
    };
  }
  function collection(coll) {
    return {
      doc: id => docRef(coll + "/" + id),
      onSnapshot(next, onErr) {
        const f = () => state && next({ docs: Object.keys(state.claims).map(id => snapDoc(coll, id)) }); f.err = onErr;
        subs.add(f); if (state) setTimeout(f, 0); startPolling();
        return () => subs.delete(f);
      },
    };
  }

  let me = null;
  const getMe = () => me || (me = call("GET", "/api/me"));

  const user = {
    id: async () => (await getMe()).id,
    canEdit: async () => (await getMe()).isAdmin,
    can: async () => true,
    profiles: async () => {                        // Kontonamen sieht nur der Admin
      if (!(await getMe()).isAdmin) return {};
      const out = {};
      for (const u of await user.accounts()) out[u.id] = { name: u.name };
      return out;
    },
    me: getMe,
    login: data => call("POST", "/api/login", data),
    logout: () => call("POST", "/api/logout"),
    changePassword: (old, neu) => call("POST", "/api/password", { old, new: neu }),
    accounts: async () => (await call("GET", "/api/users")).users,
    names: async () => (await call("GET", "/api/names")).names,
    updateAccount: (id, patch) => call("PATCH", "/api/users/" + id, patch),
    leaveTeam: async cid => { await call("POST", "/api/claims/" + cid + "/leave"); await refresh(); },
    moveClaim: async (cid, uid) => { await call("POST", "/api/claims/" + cid + "/owner", { uid }); await refresh(); },
  };
  const assets = {
    async upload(file) { return call("POST", "/api/map", file, { "Content-Type": file.type || "application/octet-stream" }); },
  };
  const downloads = {
    async save({ filename, data }) {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([data], { type: "text/csv;charset=utf-8" }));
      a.download = filename; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    },
  };

  window.claude = {
    async use(name) {
      if (name === "db") return { doc: docRef, collection };
      if (name === "user") return user;
      if (name === "assets") return (await getMe()).isAdmin ? assets : null;
      if (name === "downloads") return downloads;
      return null;
    },
  };
})();
