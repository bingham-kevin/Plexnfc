import { useState } from "react";
import type { Overview, Source } from "./api";
import { useSourceAction, useSources } from "./hooks";

/** Where movies come from: Plex, Jellyfin servers and folders of movie files. */
export function Sources({ overview }: { overview: Overview }) {
  const sources = useSources();
  const usePlex = useSourceAction<boolean>(() => "sources/plex", (enabled) => ({ enabled }),
                                           (enabled) => (enabled ? "Using Plex's movies" : "Leaving out Plex's movies"));
  const data = sources.data;
  const canManage = overview.can_manage;

  return (
    <>
      <section className="card">
        <h2>Movie sources</h2>
        <p className="muted">Movies can come from Plex, Jellyfin servers and folders of movie files, all at once. Each
          movie keeps its own tag address, so tags and cards keep working whichever source a movie is in.</p>
        {sources.isPending && <p className="muted">Loading…</p>}
        {sources.isError && <p className="err">{sources.error.message}</p>}
        {data && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Source</th><th>Where</th><th>Movies</th><th /></tr></thead>
              <tbody>
                <tr>
                  <td><b>Plex</b> <span className="muted">library {data.plex.library}</span></td>
                  <td>
                    {data.plex.signed_in
                      ? <span className="pill good"><span className="dot" /> Signed in</span>
                      : <span className="pill bad"><span className="dot" /> Not signed in</span>}
                  </td>
                  <td>{!data.plex.enabled ? <span className="muted">Left out</span>
                    : data.plex.signed_in ? "Used" : <span className="muted">Once signed in</span>}</td>
                  <td className="row end">
                    {canManage && !data.plex.signed_in && <a className="button" href="plex/login">Sign in with Plex</a>}
                    {canManage && (data.plex.enabled || data.sources.length > 0) && (
                      <button disabled={usePlex.isPending} onClick={() => usePlex.mutate(!data.plex.enabled)}>
                        {data.plex.enabled ? "Leave out" : "Use"}
                      </button>
                    )}
                  </td>
                </tr>
                {data.sources.map((s) => <SourceRow key={s.id} source={s} canManage={canManage} />)}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {canManage && <AddJellyfin />}
      {canManage && <AddFolder />}
    </>
  );
}

function SourceRow({ source, canManage }: { source: Source; canManage: boolean }) {
  const refresh = useSourceAction<void>(() => `sources/${source.id}/refresh`, undefined,
                                        (_, r) => `${source.name}: ${r.count} movies`);
  const remove = useSourceAction<void>(() => `sources/${source.id}/remove`, undefined, () => `Removed ${source.name}`);
  const where = source.kind === "jellyfin"
    ? `${source.url}${source.library ? ` · ${source.library}` : ""}${source.user ? ` · signed in as ${source.user}` : ""}`
    : source.path;
  return (
    <tr>
      <td><b>{source.name}</b> <span className="muted">{source.kind === "jellyfin" ? "Jellyfin" : "Folder"}</span></td>
      <td>
        <code className="url">{where}</code>
        {source.error && <p className="err">{source.error}</p>}
      </td>
      <td>{source.count ?? "—"}</td>
      <td className="row end">
        {canManage && (
          <button disabled={refresh.isPending} onClick={() => refresh.mutate()}
                  title="Read its movies again now, e.g. after adding files">
            {refresh.isPending ? "Reading…" : "Rescan"}
          </button>
        )}
        {canManage && (
          <button className="danger" disabled={remove.isPending}
                  onClick={() => confirm(`Remove ${source.name}? Tags and cards for its movies stop working until it's added again.`)
                    && remove.mutate()}>Remove</button>
        )}
      </td>
    </tr>
  );
}

/** A Jellyfin server: its address, and an API key or a user's sign-in. */
function AddJellyfin() {
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
    <section className="card">
      <h2>Add a Jellyfin server</h2>
      <form className="fields" onSubmit={(e) => {
        e.preventDefault();
        add.mutate(undefined, {
          onSuccess: () => { setUrl(""); setName(""); setLibrary(""); setPassword(""); setApiKey(""); },
        });
      }}>
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
                                   autoComplete="current-password" /></label>
            <p className="muted">The password goes to Jellyfin to sign in and isn't kept: Marquee Server keeps the sign-in
              Jellyfin hands back.</p>
          </>
        ) : (
          <label>API key <input value={apiKey} onChange={(e) => setApiKey(e.target.value)} required
                                autoComplete="off" />
            <span className="muted">Make one in Jellyfin's Dashboard, under API Keys.</span></label>
        )}
        <label>Library <input value={library} onChange={(e) => setLibrary(e.target.value)}
                              placeholder="Every movie library" /></label>
        <label>Name <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Jellyfin"
                           maxLength={40} /></label>
        <button className="primary" disabled={add.isPending}>{add.isPending ? "Connecting…" : "Add"}</button>
      </form>
    </section>
  );
}

/** A folder of movie files on this server: a local path, or a mounted network share. */
function AddFolder() {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const add = useSourceAction<void>(() => "sources", () => ({ kind: "folder", path, name }),
                                    () => "Added the folder");
  return (
    <section className="card">
      <h2>Add a folder</h2>
      <p className="muted">A folder of movie files on this server, and the folders inside it. For a network share (SMB or
        NFS), mount it on the server first and give the path it's mounted at: on a Mac, something like
        <code>/Volumes/Movies</code>; in Home Assistant, a share added under Settings › System › Storage shows up in
        <code>/media</code>.</p>
      <form className="fields" onSubmit={(e) => {
        e.preventDefault();
        add.mutate(undefined, { onSuccess: () => { setPath(""); setName(""); } });
      }}>
        <label>Path <input value={path} onChange={(e) => setPath(e.target.value)} required
                           placeholder="/Volumes/Movies" /></label>
        <label>Name <input value={name} onChange={(e) => setName(e.target.value)} placeholder="The folder's name"
                           maxLength={40} /></label>
        <button className="primary" disabled={add.isPending}>{add.isPending ? "Reading the folder…" : "Add"}</button>
      </form>
      <p className="muted">Movies are named from their files or folders (<code>Alien (1979)/Alien (1979).mkv</code>), or a
        <code>movie.nfo</code> beside them, which can also give collections and tags. Posters come from a
        <code>poster.jpg</code> beside the movie or the cover inside the file. Marquee plays MP4, M4V and MOV files as they
        are; other files (MKV, AVI…) need ffmpeg on the server. Folder movies play in Marquee only.</p>
    </section>
  );
}
