import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { MatchJob, Overview, PlexSource, Source, Sources as SourcesData } from "./api";
import { Dialog } from "./Dialog";
import { FolderPicker } from "./FolderPicker";
import { useSourceAction, useSources } from "./hooks";

type Kind = "jellyfin" | "folder" | "plex";
const KIND_NAMES: Record<Kind, string> = { jellyfin: "Jellyfin", folder: "Folder", plex: "Plex" };

/** Where movies come from (Plex, Jellyfin servers, folders of movie files) in the order they're used. */
export function Sources({ overview }: { overview: Overview }) {
  const sources = useSources();
  const reorder = useSourceAction<string[]>(() => "sources/order", (order) => ({ order }), () => "Order saved");
  const [adding, setAdding] = useState(false);
  const [tmdbOpen, setTmdbOpen] = useState(false);
  const data = sources.data;
  const canManage = overview.can_manage;
  // Plex is listed while it's used (or nothing else is); "Add source" brings it back once removed
  const shown = data?.ordered.filter((s) => s.kind !== "plex" || (s as PlexSource).enabled || !data.sources.length) ?? [];
  const move = (index: number, by: number) => {
    if (!data) return;
    const ids = shown.map((s) => s.id);
    [ids[index], ids[index + by]] = [ids[index + by], ids[index]];
    reorder.mutate([...ids, ...data.order.filter((id) => !ids.includes(id))]);
  };

  return (
    <section className="card">
      <div className="card-head">
        <h2>Movie sources</h2>
        {canManage && <button className="primary" onClick={() => setAdding(true)}>+ Add source</button>}
      </div>
      <p className="muted">A movie in several sources is one movie with one tag address. It plays from the first source
        here that has it and that the player can play, and from the next if that one doesn't answer.</p>
      {sources.isPending && <p className="muted">Loading…</p>}
      {sources.isError && <p className="err">{sources.error.message}</p>}
      {data && (
        <div className="table-wrap">
          <table className="sources">
            <thead><tr><th>#</th><th>Source</th><th>Movies</th><th /></tr></thead>
            <tbody>
              {shown.map((s, i) => (
                <tr key={s.id}>
                  <td className="order">
                    {i + 1}
                    {canManage && shown.length > 1 && (
                      <span className="row">
                        <button className="link" aria-label="Use it sooner" disabled={i === 0 || reorder.isPending}
                                onClick={() => move(i, -1)}>▲</button>
                        <button className="link" aria-label="Use it later"
                                disabled={i === shown.length - 1 || reorder.isPending} onClick={() => move(i, 1)}>▼</button>
                      </span>
                    )}
                  </td>
                  {s.kind === "plex"
                    ? <PlexRow plex={s as PlexSource} canManage={canManage} others={data.sources.length > 0} />
                    : <SourceRow source={s as Source} canManage={canManage} tmdb={data.tmdb}
                                 job={data.matching?.[s.id]} />}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && canManage && (
        <div className="row tmdb-line">
          <span className="muted">Matching folder movies with TMDB:</span>
          {data.tmdb
            ? <span className="pill good"><span className="dot" /> Set up</span>
            : <span className="pill"><span className="dot" /> Not set up</span>}
          <button className="link" onClick={() => setTmdbOpen(true)}>{data.tmdb ? "Change" : "Set up"}</button>
        </div>
      )}
      {adding && data && <AddSource sources={data} onClose={() => setAdding(false)} />}
      {tmdbOpen && data && <TmdbKey set={data.tmdb} onClose={() => setTmdbOpen(false)} />}
    </section>
  );
}

function PlexRow({ plex, canManage, others }: { plex: PlexSource; canManage: boolean; others: boolean }) {
  const usePlex = useSourceAction<boolean>(() => "sources/plex", (enabled) => ({ enabled }),
                                           (enabled) => (enabled ? "Using Plex's movies" : "Removed Plex"));
  return (
    <>
      <td>
        <b>Plex</b> <span className="kind">Plex</span>
        <div className="where">
          {plex.signed_in
            ? <span className="pill good"><span className="dot" /> Signed in · library {plex.library}</span>
            : <span className="pill bad"><span className="dot" /> Not signed in</span>}
        </div>
      </td>
      <td>{plex.signed_in ? "—" : <span className="muted">Once signed in</span>}</td>
      <td className="actions"><div className="row end">
        {canManage && !plex.signed_in && <a className="button" href="plex/login">Sign in with Plex</a>}
        {canManage && others && (
          <button className="danger" disabled={usePlex.isPending}
                  onClick={() => confirm("Stop using Plex's movies? You can add Plex back with Add source.")
                    && usePlex.mutate(false)}>Remove</button>
        )}
      </div></td>
    </>
  );
}

function SourceRow({ source, canManage, tmdb, job }:
  { source: Source; canManage: boolean; tmdb: boolean; job?: MatchJob }) {
  const matchAll = useSourceAction<void>(() => `sources/${source.id}/match`, undefined,
                                         (_, r) => r.matching?.[source.id]?.total
                                           ? `Matching ${source.name}'s movies with TMDB…`
                                           : `${source.name}: every movie is matched`);
  // When matching finishes, the movie list has new titles and posters
  const client = useQueryClient();
  const running = useRef(false);
  useEffect(() => {
    if (running.current && job && !job.running) {
      for (const key of ["movies", "filters"]) client.invalidateQueries({ queryKey: [key] });
    }
    running.current = !!job?.running;
  }, [job, client]);
  const refresh = useSourceAction<void>(() => `sources/${source.id}/refresh`, undefined,
                                        (_, r) => `${source.name}: ${r.count} movies`);
  const remove = useSourceAction<void>(() => `sources/${source.id}/remove`, undefined, () => `Removed ${source.name}`);
  const where = source.kind === "jellyfin"
    ? `${source.url}${source.library ? ` · ${source.library}` : ""}${source.user ? ` · ${source.user}` : ""}`
    : source.path;
  return (
    <>
      <td>
        <b>{source.name}</b> <span className="kind">{KIND_NAMES[source.kind]}</span>
        <div className="where muted">{where}</div>
        {source.error && <p className="err">{source.error}</p>}
        {job && (job.running
          ? <p className="muted">Matching with TMDB: {job.done} of {job.total} ({job.matched} matched)</p>
          : <p className="muted">Matched {job.matched} of {job.total} with TMDB.
            {job.matched < job.total && " Match the rest from the Tags tab."}</p>)}
        {job?.error && <p className="err">{job.error}</p>}
      </td>
      <td>{source.count ?? "—"}</td>
      <td className="actions"><div className="row end">
        {canManage && source.kind === "folder" && tmdb && (
          <button disabled={matchAll.isPending || job?.running} onClick={() => matchAll.mutate()}
                  title="Match its movies that have no ID yet, when TMDB has one with the same title and year">
            {job?.running ? "Matching…" : "Match all"}
          </button>
        )}
        {canManage && (
          <button disabled={refresh.isPending} onClick={() => refresh.mutate()}
                  title="Read its movies again now, e.g. after adding files">
            {refresh.isPending ? "Reading…" : "Rescan"}
          </button>
        )}
        {canManage && (
          <button className="danger" disabled={remove.isPending}
                  onClick={() => confirm(`Remove ${source.name}? Its movies stay playable from the other sources that have them.`)
                    && remove.mutate()}>Remove</button>
        )}
      </div></td>
    </>
  );
}

/** Add a source: choose its kind, then fill in what that kind needs. */
function AddSource({ sources, onClose }: { sources: SourcesData; onClose: () => void }) {
  const [kind, setKind] = useState<Kind>("folder");
  return (
    <Dialog title="Add a source" onClose={onClose}>
      <div className="fields">
        <label>Type
          <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            <option value="folder">Network or local folder</option>
            <option value="jellyfin">Jellyfin server</option>
            <option value="plex">Plex</option>
          </select>
        </label>
      </div>
      <div className="add-form">
        {kind === "folder" && <AddFolder onDone={onClose} />}
        {kind === "jellyfin" && <AddJellyfin onDone={onClose} />}
        {kind === "plex" && <AddPlex plex={sources.plex} onDone={onClose} />}
      </div>
    </Dialog>
  );
}

function AddPlex({ plex, onDone }: { plex: PlexSource; onDone: () => void }) {
  const usePlex = useSourceAction<void>(() => "sources/plex", () => ({ enabled: true }), () => "Using Plex's movies");
  if (plex.enabled && plex.signed_in) {
    return <p className="muted">Plex is already a source (library {plex.library}). Marquee Server uses one Plex
      account.</p>;
  }
  return (
    <>
      <p className="muted">Movies from the Plex library <b>{plex.library}</b>.</p>
      <div className="row">
        {!plex.enabled && (
          <button className="primary" disabled={usePlex.isPending}
                  onClick={() => usePlex.mutate(undefined, { onSuccess: onDone })}>Add Plex</button>
        )}
        {!plex.signed_in && <a className={`button${plex.enabled ? " primary" : ""}`} href="plex/login">Sign in with Plex</a>}
      </div>
    </>
  );
}

/** A Jellyfin server: its address, and an API key or a user's sign-in. */
function AddJellyfin({ onDone }: { onDone: () => void }) {
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [library, setLibrary] = useState("");
  const [how, setHow] = useState<"signin" | "key">("signin");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [apiKey, setApiKey] = useState("");
  const add = useSourceAction<void>(() => "sources", () => ({
    kind: "jellyfin", url, name, library,
    ...(how === "key" ? { api_key: apiKey } : { username, password }),
  }), () => "Added the Jellyfin server");

  return (
    <form className="fields" onSubmit={(e) => { e.preventDefault(); add.mutate(undefined, { onSuccess: onDone }); }}>
      <label>Address <input value={url} onChange={(e) => setUrl(e.target.value)} required type="url"
                            placeholder="http://192.168.1.20:8096" /></label>
      <div className="segmented" role="radiogroup" aria-label="Connect with">
        {(["signin", "key"] as const).map((h) => (
          <button key={h} type="button" role="radio" aria-checked={how === h} className={how === h ? "on" : ""}
                  onClick={() => setHow(h)}>
            {h === "signin" ? "Sign in" : "API key"}
          </button>
        ))}
      </div>
      {how === "signin" ? (
        <>
          <label>User name <input value={username} onChange={(e) => setUsername(e.target.value)} required
                                  autoComplete="username" /></label>
          <label>Password <input value={password} onChange={(e) => setPassword(e.target.value)} type="password"
                                 autoComplete="current-password" />
            <span className="muted">Only passed on to Jellyfin; the sign-in it hands back is kept, not the password.</span>
          </label>
        </>
      ) : (
        <label>API key <input value={apiKey} onChange={(e) => setApiKey(e.target.value)} required autoComplete="off" />
          <span className="muted">From Jellyfin's Dashboard › API Keys.</span></label>
      )}
      <label>Library <input value={library} onChange={(e) => setLibrary(e.target.value)}
                            placeholder="Every movie library" /></label>
      <label>Name <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Jellyfin"
                         maxLength={40} /></label>
      <button className="primary" disabled={add.isPending}>{add.isPending ? "Connecting…" : "Add"}</button>
    </form>
  );
}

/** A folder of movie files on this server: a local path, or a mounted network share. */
function AddFolder({ onDone }: { onDone: () => void }) {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const add = useSourceAction<void>(() => "sources", () => ({ kind: "folder", path, name }),
                                    () => "Added the folder");
  return (
    <>
      <form className="fields" onSubmit={(e) => { e.preventDefault(); add.mutate(undefined, { onSuccess: onDone }); }}>
        <label>Folder
          <span className="row nowrap">
            <input value={path} onChange={(e) => setPath(e.target.value)} required placeholder="/Volumes/Movies" />
            <button type="button" onClick={() => setBrowsing(true)}>Browse…</button>
          </span>
          <span className="muted">Mount a network share on the server first (on a Mac, Finder › Go › Connect to
            Server); it then shows up under Browse.</span>
        </label>
        <label>Name <input value={name} onChange={(e) => setName(e.target.value)} placeholder="The folder's name"
                           maxLength={40} /></label>
        <button className="primary" disabled={add.isPending}>{add.isPending ? "Reading the folder…" : "Add"}</button>
      </form>
      <details className="help">
        <summary>How movies are named</summary>
        <p className="muted">From their files or folders (<code>Alien (1979)/Alien (1979).mkv</code>), or a
          <code>movie.nfo</code> beside them. Posters come from a <code>poster.jpg</code> beside the movie or the cover
          inside the file, or from TMDB once matched. MP4, M4V and MOV play as they are; MKV, AVI and others need ffmpeg
          on the server. Folder movies play in Marquee only.</p>
      </details>
      {browsing && <FolderPicker start={path} onClose={() => setBrowsing(false)}
                                 onPick={(p) => { setPath(p); setBrowsing(false); }} />}
    </>
  );
}

/** A TMDB API key, for matching folder movies and giving them posters and details. */
function TmdbKey({ set, onClose }: { set: boolean; onClose: () => void }) {
  const [key, setKey] = useState("");
  const save = useSourceAction<string>(() => "sources/tmdb", (k) => ({ key: k }),
                                       (k) => (k ? "TMDB key saved" : "TMDB key removed"));
  return (
    <Dialog title="Matching folder movies" onClose={onClose}>
      <p className="muted">Folder movies are matched with TMDB (The Movie Database, which Plex and Jellyfin use too) for
        their proper titles, posters, summaries and collections. That needs a free API key: sign up at themoviedb.org
        and copy the API key from Settings › API. Matches are kept on this server, not written to your files.</p>
      {set && <p className="row"><span className="pill good"><span className="dot" /> A key is saved</span></p>}
      <form className="row nowrap" onSubmit={(e) => {
        e.preventDefault();
        save.mutate(key, { onSuccess: () => { setKey(""); onClose(); } });
      }}>
        <input value={key} onChange={(e) => setKey(e.target.value)} required autoComplete="off" className="grow"
               aria-label="TMDB API key" placeholder={set ? "A new TMDB API key" : "TMDB API key"} />
        <button className="primary" disabled={save.isPending}>{save.isPending ? "Checking…" : "Save"}</button>
      </form>
      {set && (
        <p className="row end dialog-foot">
          <button className="danger" disabled={save.isPending}
                  onClick={() => confirm("Remove the TMDB key? Matches made already stay.")
                    && save.mutate("", { onSuccess: onClose })}>Remove key</button>
        </p>
      )}
      <p className="muted">Then use <b>Match all</b> on a folder, or <b>Match…</b> beside a movie on the Tags tab.</p>
    </Dialog>
  );
}
