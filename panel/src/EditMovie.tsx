import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { get, post, posterURL, type FolderMovie, type Movie, type TmdbResult } from "./api";
import { Dialog } from "./Dialog";
import { useSources } from "./hooks";
import { useToast } from "./toast";

/** Match a folder movie with TMDB, or change its details by hand. Nothing is written to the share. */
export function EditMovie({ movie, onClose, onSaved }: { movie: Movie; onClose: () => void; onSaved: () => void }) {
  const [fileId, setFileId] = useState(movie.files[0]?.id ?? "");
  const client = useQueryClient();
  const toast = useToast();
  const sources = useSources();
  const details = useQuery({
    queryKey: ["folder-movie", fileId],
    queryFn: () => get<{ movie: FolderMovie }>(`files/${fileId}`),
  });
  const m = details.data?.movie;
  const [version, setVersion] = useState(0);

  const save = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: object; done: string }) =>
      post<{ ok: boolean; movie: FolderMovie }>(`files/${fileId}/${path}`, body),
    onSuccess: (result, { done }) => {
      client.setQueryData(["folder-movie", fileId], { movie: result.movie });
      for (const key of ["movies", "filters"]) client.invalidateQueries({ queryKey: [key] });
      setVersion((v) => v + 1);
      onSaved();
      toast(done);
    },
    onError: (error) => toast(error.message, "error"),
  });

  return (
    <Dialog title={`Edit ${movie.title}`} onClose={onClose} wide>
      {movie.files.length > 1 && (
        <label className="row">File
          <select value={fileId} onChange={(e) => setFileId(e.target.value)}>
            {movie.files.map((f) => <option key={f.id} value={f.id}>{f.source}</option>)}
          </select>
        </label>
      )}
      {details.isPending && <p className="muted">Loading…</p>}
      {details.isError && <p className="err">{details.error.message}</p>}
      {m && (
        <>
          <div className="edit-head">
            <img className="edit-poster" src={posterURL(m.key, 160, 240, `${m.matched}${m.ids.tmdb ?? ""}${version}`)} alt="" />
            <div>
              <p><b>{m.title}</b>{m.year ? ` (${m.year})` : ""}</p>
              <p className="muted"><code className="url">{m.path}</code> in {m.source}</p>
              <p>
                {m.matched
                  ? <span className="pill good"><span className="dot" /> Matched with TMDB{m.matched === "auto" ? " (automatically)" : ""}</span>
                  : m.ids.tmdb || m.ids.imdb
                    ? <span className="pill good"><span className="dot" /> IDs from its .nfo or name</span>
                    : m.key.startsWith("t-")
                      ? <span className="pill bad"><span className="dot" /> Not matched</span>
                      : <span className="pill good"><span className="dot" /> Same movie as in your other sources</span>}
                {m.ids.tmdb && <> <a href={`https://www.themoviedb.org/movie/${m.ids.tmdb}`} target="_blank"
                                     rel="noreferrer">TMDB {m.ids.tmdb}</a></>}
              </p>
              <p className="muted">Its file says: {m.file.title}{m.file.year ? ` (${m.file.year})` : ""}</p>
              {m.edited && (
                <button disabled={save.isPending}
                        onClick={() => confirm("Forget the match and changes, and go back to what the file says?")
                          && save.mutate({ path: "reset", done: "Back to what the file says" })}>
                  Reset to the file
                </button>
              )}
            </div>
          </div>

          <h3>Match with TMDB</h3>
          {sources.data && !sources.data.tmdb
            ? <p className="muted">Add a TMDB API key on the Sources tab to look movies up.</p>
            : <TmdbSearch key={fileId} title={m.file.title || m.title} year={m.file.year ?? m.year}
                          current={m.ids.tmdb} busy={save.isPending}
                          onUse={(r) => save.mutate({ path: "match", body: { tmdb_id: r.id },
                                                      done: `Matched with ${r.title}${r.year ? ` (${r.year})` : ""}` })} />}

          <h3>Details</h3>
          <Details key={`${fileId}-${version}`} movie={m} busy={save.isPending}
                   onSave={(body) => save.mutate({ path: "edit", body, done: "Saved" })} />
        </>
      )}
    </Dialog>
  );
}

function TmdbSearch({ title, year, current, busy, onUse }: {
  title: string; year: number | null; current?: string; busy: boolean; onUse: (r: TmdbResult) => void;
}) {
  const [query, setQuery] = useState({ q: title, year: year ? String(year) : "" });
  const [typed, setTyped] = useState(query);
  useEffect(() => { setTyped(query); }, [query]);
  const search = useQuery({
    queryKey: ["tmdb", query.q, query.year],
    queryFn: () => get<{ results: TmdbResult[] }>(
      `match/search?q=${encodeURIComponent(query.q)}${query.year ? `&year=${query.year}` : ""}`),
    enabled: !!query.q.trim(),
    retry: false,
    staleTime: 10 * 60_000,
  });
  return (
    <>
      <form className="row wrap" onSubmit={(e) => { e.preventDefault(); setQuery(typed); }}>
        <input value={typed.q} onChange={(e) => setTyped({ ...typed, q: e.target.value })} aria-label="Title"
               placeholder="Title" />
        <input value={typed.year} onChange={(e) => setTyped({ ...typed, year: e.target.value })} aria-label="Year"
               placeholder="Year" inputMode="numeric" size={6} />
        <button>Search</button>
      </form>
      {search.isFetching && <p className="muted">Looking…</p>}
      {search.isError && <p className="err">{search.error.message}</p>}
      {search.data && !search.data.results.length && <p className="muted">TMDB has nothing by that name.</p>}
      <div className="tmdb-results">
        {search.data?.results.map((r) => (
          <div key={r.id} className={`tmdb-result${String(r.id) === current ? " on" : ""}`}>
            {r.poster ? <img src={r.poster} alt="" loading="lazy" /> : <div className="no-poster" />}
            <div>
              <b>{r.title}</b> {r.year && <span className="muted">({r.year})</span>}
              <p className="muted clamp">{r.summary}</p>
              {String(r.id) === current
                ? <span className="pill good"><span className="dot" /> Its match</span>
                : <button className="primary" disabled={busy} onClick={() => onUse(r)}>Use this</button>}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

function Details({ movie, busy, onSave }: { movie: FolderMovie; busy: boolean; onSave: (body: object) => void }) {
  const [title, setTitle] = useState(movie.title);
  const [year, setYear] = useState(movie.year ? String(movie.year) : "");
  const [summary, setSummary] = useState(movie.summary);
  const [collections, setCollections] = useState(movie.collections.join(", "));
  const [labels, setLabels] = useState(movie.labels.join(", "));
  const [tmdb, setTmdb] = useState(movie.ids.tmdb ?? "");
  return (
    <form className="fields" onSubmit={(e) => {
      e.preventDefault();
      onSave({ title, year, summary, collections, labels, tmdb });
    }}>
      <label>Title <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={200} /></label>
      <label>Year <input value={year} onChange={(e) => setYear(e.target.value)} inputMode="numeric"
                         pattern="\d{4}" placeholder="1979" /></label>
      <label>Summary <textarea value={summary} onChange={(e) => setSummary(e.target.value)} rows={3} /></label>
      <label>Collections <input value={collections} onChange={(e) => setCollections(e.target.value)}
                                placeholder="Separated by commas" /></label>
      <label>Labels <input value={labels} onChange={(e) => setLabels(e.target.value)}
                           placeholder="Separated by commas" /></label>
      <label>TMDB ID <input value={tmdb} onChange={(e) => setTmdb(e.target.value)} inputMode="numeric"
                            pattern="\d*" placeholder="348" />
        <span className="muted">Changing the ID can change the movie's tag address. Tags and cards made from its file name keep working.</span>
      </label>
      <button className="primary" disabled={busy}>Save</button>
    </form>
  );
}
