import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { get, post, type BooksState, type LibItem, type OpenedItem, type Overview } from "./api";
import { useCards } from "./hooks";
import { Art, CardScan, LinkCard, Speakers, Tile, Zones, time, useMusicAction, useSpeakerSelection } from "./Music";
import { useToast } from "./toast";

const KINDS = { book: "Books", author: "Authors", series: "Series" } as const;
type Kind = keyof typeof KINDS;

export const useBooks = () => useQuery({ queryKey: ["books"], queryFn: () => get<BooksState>("books") });

/** Books › Library: audiobooks from Audiobookshelf, played on the same speakers as music. */
export function BooksLibraryPage({ overview }: { overview: Overview }) {
  const canManage = overview.can_manage;
  const [selected, choose] = useSpeakerSelection();
  const cards = useCards();
  const books = useBooks();
  const abs = books.data?.audiobookshelf;
  return (
    <>
      {cards.data?.scan && <CardScan state={cards.data} canManage={canManage} />}
      <Zones canManage={canManage} />
      <Speakers selected={selected} onChange={choose} canManage={canManage} />
      {books.data && !abs?.configured ? (
        <section className="card">
          <h2>Library</h2>
          <p className="muted">Connect Audiobookshelf under <a href="#books/sources">Books › Sources</a> to see your
            audiobooks here.</p>
        </section>
      ) : (
        <BookLibrary selected={selected} canManage={canManage} unknown={cards.data?.unknown?.uid ?? null} />
      )}
    </>
  );
}

function BookLibrary({ selected, canManage, unknown }: { selected: string[]; canManage: boolean; unknown: string | null }) {
  const [kind, setKind] = useState<Kind>("book");
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(60);
  const [open, setOpen] = useState<string[]>([]);
  useEffect(() => {
    const t = setTimeout(() => setQuery(typed.trim()), 350);
    return () => clearTimeout(t);
  }, [typed]);
  useEffect(() => setLimit(60), [kind, query]);
  const items = useQuery({
    queryKey: ["music-items", "abs", kind, query, limit],
    queryFn: () => get<{ items: LibItem[] }>(`music/browse?source=abs&kind=${kind}&q=${encodeURIComponent(query)}&limit=${limit}`),
    placeholderData: (previous) => previous,
    retry: false,
  });
  const top = open[open.length - 1];
  return (
    <section className="card">
      <h2>Library</h2>
      {top ? (
        top.startsWith("abs|book|")
          ? <BookView id={top} selected={selected} canManage={canManage} unknown={unknown} onBack={() => setOpen(open.slice(0, -1))} />
          : <ShelfView id={top} onOpen={(id) => setOpen([...open, id])} onBack={() => setOpen(open.slice(0, -1))} />
      ) : (
        <>
          <div className="toolbar">
            <div className="segmented" role="radiogroup" aria-label="Show">
              {(Object.keys(KINDS) as Kind[]).map((k) => (
                <button key={k} role="radio" aria-checked={kind === k} className={kind === k ? "on" : ""}
                        onClick={() => setKind(k)}>{KINDS[k]}</button>
              ))}
            </div>
            <input type="search" value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="Search books"
                   placeholder={`Search ${KINDS[kind].toLowerCase()}`} />
          </div>
          {items.isError && <p className="err">{items.error.message}</p>}
          {items.isFetching && !items.data && <p className="muted">Loading…</p>}
          {items.data && !items.data.items.length && <p className="muted">Nothing found.</p>}
          <div className="music-grid">
            {items.data?.items.map((item) => <Tile key={item.id} item={item} onOpen={() => setOpen([item.id])} />)}
          </div>
          {!query && items.data && items.data.items.length >= limit && (
            <p className="row end"><button onClick={() => setLimit(limit + 60)}>More</button></p>
          )}
        </>
      )}
    </section>
  );
}

/** An author's or a series' books. */
function ShelfView({ id, onOpen, onBack }: { id: string; onOpen: (id: string) => void; onBack: () => void }) {
  const opened = useQuery({
    queryKey: ["music-item", id],
    queryFn: () => get<OpenedItem>(`music/item?id=${encodeURIComponent(id)}`),
    retry: false,
  });
  const item = opened.data?.item;
  return (
    <>
      <p><button className="link" onClick={onBack}>‹ Back</button></p>
      {opened.isError && <p className="err">{opened.error.message}</p>}
      {opened.isPending && <p className="muted">Loading…</p>}
      {item && (
        <div className="item-head">
          <Art image={item.image} className={`item-art${item.kind === "author" ? " round" : ""}`} icon="📖" />
          <div>
            <div className="muted small">{item.kind}</div>
            <h3>{item.name}</h3>
            {item.artist && <div className="muted">{item.artist}</div>}
          </div>
        </div>
      )}
      <div className="music-grid">
        {opened.data?.albums?.map((b) => <Tile key={b.id} item={b} onOpen={() => onOpen(b.id)} />)}
      </div>
    </>
  );
}

/** One audiobook: where you are in it, its chapters, and playing it. */
function BookView({ id, selected, canManage, unknown, onBack }:
  { id: string; selected: string[]; canManage: boolean; unknown: string | null; onBack: () => void }) {
  const opened = useQuery({
    queryKey: ["music-item", id],
    queryFn: () => get<OpenedItem>(`music/item?id=${encodeURIComponent(id)}`),
    retry: false,
  });
  const play = useMusicAction<{ position?: number }>("music/play",
    (v) => ({ ids: [id], speakers: selected, option: "replace", ...v }), () => "Playing");
  const book = opened.data?.item;
  const progress = opened.data?.progress;
  const started = !!progress && !progress.finished && progress.position > 5;
  const noSpeaker = selected.length === 0;
  const chapters = opened.data?.chapters ?? [];
  return (
    <>
      <p><button className="link" onClick={onBack}>‹ Back</button></p>
      {opened.isError && <p className="err">{opened.error.message}</p>}
      {opened.isPending && <p className="muted">Loading…</p>}
      {book && (
        <div className="item-head">
          <Art image={book.image} className="item-art" icon="📖" />
          <div>
            <div className="muted small">audiobook</div>
            <h3>{book.name}</h3>
            {book.subtitle && <div className="muted">{book.subtitle}</div>}
            <div className="muted">
              {[book.artist, book.narrator && `read by ${book.narrator}`, book.album, time(book.duration)].filter(Boolean).join(" · ")}
            </div>
            {progress && (
              <div className="book-progress">
                <div className="progress" aria-hidden>
                  <div style={{ width: `${book.duration ? Math.min(100, (progress.position / book.duration) * 100) : 0}%` }} />
                </div>
                <span className="muted small">
                  {progress.finished ? "Finished" : `${time(progress.position)} of ${time(book.duration)}`}
                </span>
              </div>
            )}
            {canManage && (
              <div className="row wrap actions">
                <button className="primary" disabled={noSpeaker || play.isPending} title={noSpeaker ? "Choose a speaker first" : ""}
                        onClick={() => play.mutate({})}>{started ? `Resume at ${time(progress!.position)}` : "Play"}</button>
                {started && (
                  <button disabled={noSpeaker || play.isPending} onClick={() => play.mutate({ position: 0 })}>
                    From the start</button>
                )}
                <LinkCard item={book} unknown={unknown} />
              </div>
            )}
            {book.description && <p className="muted small book-description">{book.description}</p>}
          </div>
        </div>
      )}
      {chapters.length > 0 && (
        <ol className="tracks">
          {chapters.map((c, i) => {
            const here = !!progress && !progress.finished && progress.position >= c.start && progress.position < c.end;
            return (
              <li key={i} className={here ? "on" : ""}>
                <span className="muted num">{i + 1}</span>
                <span className="grow">{c.title}{here && <span className="muted"> · you're here</span>}</span>
                <span className="muted small">{time(c.end - c.start)}</span>
                {canManage && (
                  <button className="link" disabled={noSpeaker} title="Play from this chapter"
                          onClick={() => play.mutate({ position: c.start })}>▶</button>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </>
  );
}

/** Books › Sources: the Audiobookshelf connection. */
export function BooksSourcesPage({ overview }: { overview: Overview }) {
  const books = useBooks();
  const client = useQueryClient();
  const toast = useToast();
  const abs = books.data?.audiobookshelf;
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  useEffect(() => { if (abs?.url) setUrl(abs.url); }, [abs?.url]);
  const save = useMutation({
    mutationFn: (body: { url: string; token?: string }) => post<BooksState>("books/audiobookshelf", body),
    onSuccess: (state, body) => {
      client.setQueryData(["books"], state);
      client.invalidateQueries({ queryKey: ["music-items"] });
      setToken("");
      toast(body.url ? "Connected to Audiobookshelf" : "Audiobookshelf disconnected");
    },
    onError: (error) => toast(error.message, "error"),
  });
  return (
    <section className="card">
      <h2>Audiobookshelf</h2>
      <div className="row">
        {abs?.configured
          ? abs.message
            ? <span className="pill bad" title={abs.message}><span className="dot" /> Can't reach it</span>
            : <span className="pill good"><span className="dot" /> Connected{abs.user ? ` as ${abs.user}` : ""}</span>
          : <span className="pill"><span className="dot" /> Not connected</span>}
        {abs?.libraries.map((l) => <span key={l.id} className="pill">{l.name}</span>)}
      </div>
      {abs?.message && <p className="err">{abs.message}</p>}
      {overview.can_manage ? (
        <>
          <p className="muted">Audiobooks come from your Audiobookshelf server. Give Marquee its address (such as
            http://nas.local:13378) and an API key: in Audiobookshelf, open <b>Settings › API Keys</b> and add one called
            Marquee. The key is kept on this server only. Books play on the same speakers as music, pick up where you
            left off, and Audiobookshelf hears how far you got.</p>
          <form onSubmit={(e) => { e.preventDefault(); save.mutate(token ? { url, token } : { url }); }}>
            <p className="row nowrap">
              <input value={url} onChange={(e) => setUrl(e.target.value)} required className="grow" autoComplete="off"
                     aria-label="Audiobookshelf's address" placeholder="http://nas.local:13378" />
            </p>
            <p className="row nowrap">
              <input value={token} onChange={(e) => setToken(e.target.value)} required={!abs?.configured}
                     type="password" autoComplete="off" className="grow" aria-label="Audiobookshelf API key"
                     placeholder={abs?.configured ? "Keep the key there is" : "API key"} />
              <button className="primary" disabled={save.isPending}>{save.isPending ? "Checking…" : "Connect"}</button>
            </p>
          </form>
          {abs?.configured && (
            <p className="row end">
              <button className="danger" disabled={save.isPending}
                      onClick={() => confirm("Disconnect Audiobookshelf? Book cards stop working until it's connected again.")
                        && save.mutate({ url: "" })}>Disconnect</button>
            </p>
          )}
        </>
      ) : (
        <p className="muted">Open the control panel on the server to change this.</p>
      )}
    </section>
  );
}
