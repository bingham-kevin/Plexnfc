import { useMemo, useState } from "react";
import { posterURL, type Card, type CardsState, type Movie, type MusicItem, type Overview,
  type Scan } from "./api";
import { MediaScanSetup } from "./MediaCards";
import { Art, iconFor } from "./Music";
import { useCardAction, useCards, useFilters, useMovies } from "./hooks";

type Source = "collection" | "label" | "source" | "manual";
export type Section = "movies" | "music" | "books";

/** Which section a card belongs to: a movie, music, or an audiobook. */
export const sectionOf = (c: Card): Section =>
  !c.music ? "movies" : c.music.id?.startsWith("abs|") ? "books" : "music";

/** Blank NFC cards linked by their ID: nothing is written on the cards. Choose the movies,
 * albums or books, start a scan, and hold each one's card on a Movie Remote in turn. */
export function Cards({ overview, section = "movies" }: { overview: Overview; section?: Section }) {
  const cards = useCards();
  const state = cards.data;
  return (
    <>
      {cards.isError && <p className="err">{cards.error.message}</p>}
      {state?.scan ? (
        <ScanView scan={state.scan} canManage={overview.can_manage} />
      ) : (
        <>
          {state?.unknown && overview.can_manage && <UnknownCard uid={state.unknown.uid} section={section} />}
          {section === "movies" ? <ScanSetup canManage={overview.can_manage} />
            : <MediaScanSetup section={section} canManage={overview.can_manage} cards={state?.cards ?? []} />}
          {state && <LinkedCards state={state} canManage={overview.can_manage} section={section} />}
        </>
      )}
    </>
  );
}

/** Choose the movies to scan cards for: a collection, a label, or by hand. */
function ScanSetup({ canManage }: { canManage: boolean }) {
  const [source, setSource] = useState<Source>("collection");
  const [collection, setCollection] = useState("");
  const [label, setLabel] = useState("");
  const [from, setFrom] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [skipLinked, setSkipLinked] = useState(true);
  const filters = useFilters();
  const movies = useMovies(source === "collection" ? collection : "", source === "label" ? label : "",
                           source === "source" ? from : "");
  const start = useCardAction<Movie[]>(() => "cards/scan",
                                       (list) => ({ movies: list.map((m) => ({ key: m.key, title: m.title, year: m.year })) }));

  const chosenFilter = { collection, label, source: from, manual: "manual" }[source];
  const choices = (["collection", "label", "source", "manual"] as const)
    .filter((s) => s !== "source" || !!filters.data?.sources.length);
  const all = movies.data?.movies ?? [];
  const queue = useMemo(() => {
    if (!chosenFilter) return [];
    const list = source === "manual" ? all.filter((m) => picked.has(m.key)) : all;
    return skipLinked ? list.filter((m) => m.cards.length === 0) : list;
  }, [all, source, picked, skipLinked, chosenFilter]);
  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? all.filter((m) => `${m.title} ${m.year ?? ""}`.toLowerCase().includes(q)) : all;
  }, [all, search]);
  const toggle = (key: string) => setPicked((p) => {
    const next = new Set(p);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  return (
    <section className="card">
      <h2>Scan cards</h2>
      <p className="muted">Link blank NFC cards to movies without writing anything on them. Choose the movies, start the
        scan, then hold each movie's card on a Movie Remote when it comes up here. Once a card is read, the next movie
        comes up.</p>

      <div className="toolbar">
        <div className="segmented" role="radiogroup" aria-label="Choose movies by">
          {choices.map((s) => (
            <button key={s} role="radio" aria-checked={source === s} className={source === s ? "on" : ""}
                    onClick={() => setSource(s)}>
              {{ collection: "Collection", label: "Label", source: "Source", manual: "Pick movies" }[s]}
            </button>
          ))}
        </div>
        {source === "collection" && (
          <select value={collection} onChange={(e) => setCollection(e.target.value)} aria-label="Collection"
                  disabled={filters.isPending}>
            <option value="">{filters.isPending ? "Loading collections…" : "Choose a collection"}</option>
            {filters.data?.collections.map((c) => (
              <option key={c.title} value={c.title}>{c.title}{c.count != null ? ` (${c.count})` : ""}</option>
            ))}
          </select>
        )}
        {source === "label" && (
          <select value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Label"
                  disabled={filters.isPending}>
            <option value="">{filters.isPending ? "Loading labels…" : "Choose a label"}</option>
            {filters.data?.labels.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        )}
        {source === "source" && (
          <select value={from} onChange={(e) => setFrom(e.target.value)} aria-label="Source">
            <option value="">Choose a source</option>
            {filters.data?.sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        )}
        <label className="row">
          <input type="checkbox" checked={skipLinked} onChange={(e) => setSkipLinked(e.target.checked)} />
          Skip movies that already have a card
        </label>
      </div>
      {(source === "collection" || source === "label") && filters.data
        && !filters.data[source === "label" ? "labels" : "collections"].length && (
        <p className="muted">Your library has no {source === "label" ? "labels" : "collections"}.</p>
      )}

      {source === "manual" && (
        <>
          <div className="toolbar">
            <input type="search" placeholder="Search titles" value={search} onChange={(e) => setSearch(e.target.value)}
                   aria-label="Search titles" />
            <span className="muted">{picked.size} picked</span>
            <button onClick={() => setPicked(new Set([...picked, ...shown.map((m) => m.key)]))}>Pick all shown</button>
            <button disabled={!picked.size} onClick={() => setPicked(new Set())}>Clear</button>
          </div>
          <div className="pick-list">
            {movies.isPending && <p className="muted">Loading the library…</p>}
            {shown.map((m) => (
              <label key={m.key} className={`pick ${picked.has(m.key) ? "on" : ""}`}>
                <input type="checkbox" checked={picked.has(m.key)} onChange={() => toggle(m.key)} />
                <img className="thumb" src={posterURL(m.key, 80, 120)} alt="" loading="lazy" />
                <span><b>{m.title}</b> <span className="muted">{m.year ?? ""}</span></span>
                {m.cards.length > 0 && <span className="pill good" title={m.cards.join(", ")}>Has a card</span>}
              </label>
            ))}
          </div>
        </>
      )}

      {movies.isError && chosenFilter && <p className="err">{movies.error.message}</p>}
      {source !== "manual" && chosenFilter && movies.data && !all.length && (
        <p className="err">Plex has no movies in this {source === "label" ? "label" : "collection"}. Add them to it in
          Plex, or use <b>Pick movies</b>.</p>
      )}
      <div className="row scan-start">
        <button className="primary" disabled={!canManage || !queue.length || start.isPending}
                onClick={() => start.mutate(queue)}>
          {queue.length ? `Scan ${queue.length} card${queue.length === 1 ? "" : "s"}` : "Scan cards"}
        </button>
        {chosenFilter && source !== "manual" && movies.data && (
          <span className="muted">
            {all.length} movie{all.length === 1 ? "" : "s"}
            {skipLinked && all.length !== queue.length && `, ${all.length - queue.length} already have a card`}
          </span>
        )}
      </div>
    </section>
  );
}

/** The scan in progress: the movie whose card to hold on the reader, and what the last card did. */
function ScanView({ scan, canManage }: { scan: Scan; canManage: boolean }) {
  const step = useCardAction<"skip" | "back" | "stop">((a) => `cards/scan/${a}`);
  const recent = scan.last && Date.now() / 1000 - scan.last.at < 6;
  const progress = scan.total ? (scan.index / scan.total) * 100 : 0;

  return (
    <section className="card scan">
      <div className="row">
        <h2>Scanning cards</h2>
        <span className="muted">{Math.min(scan.index + 1, scan.total)} of {scan.total} · {scan.linked} linked</span>
      </div>
      <div className="progress" aria-hidden><div style={{ width: `${progress}%` }} /></div>

      {scan.current ? (
        <div className="scan-body">
          {scan.current.music ? (
            <MusicArt key={scan.current.key} item={scan.current.music} className="scan-poster music" />
          ) : (
            <img key={scan.current.key} className="scan-poster" src={posterURL(scan.current.key, 300, 450)} alt="" />
          )}
          <div>
            <p className="muted">Hold this {!scan.current.music ? "movie" : scan.current.music.id?.startsWith("abs|") ? "book"
              : "music"}'s card on a Movie Remote or an NFC reader in Home Assistant</p>
            <h3 className="scan-title">{scan.current.title}</h3>
            {scan.next && <p className="muted">Next: {scan.next.title}</p>}
            {scan.last && (
              <p key={scan.last.at} className={`scan-last ${recent ? "fresh" : ""}`}>
                ✓ Linked <b>{scan.last.title}</b> to card <code>{scan.last.uid}</code>
                {scan.last.moved_from && <> (it was {scan.last.moved_from})</>}
              </p>
            )}
            <div className="row">
              <button disabled={!canManage || scan.index === 0 || step.isPending} onClick={() => step.mutate("back")}>
                ‹ Back
              </button>
              <button disabled={!canManage || step.isPending} onClick={() => step.mutate("skip")}>Skip ›</button>
              <button className="danger" disabled={!canManage || step.isPending} onClick={() => step.mutate("stop")}>
                Stop scanning
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="scan-body done">
          <div>
            <h3 className="scan-title">All done</h3>
            <p>{scan.linked} card{scan.linked === 1 ? "" : "s"} linked.</p>
            {scan.last && <p className="muted">Last: {scan.last.title} (<code>{scan.last.uid}</code>)</p>}
            <div className="row">
              <button disabled={!canManage || step.isPending} onClick={() => step.mutate("back")}>‹ Back</button>
              <button className="primary" disabled={!canManage || step.isPending} onClick={() => step.mutate("stop")}>
                Finish
              </button>
            </div>
          </div>
        </div>
      )}
      <p className="muted">While scanning, cards held on a Movie Remote, or on an NFC reader in Home Assistant, are linked rather than played.</p>
    </section>
  );
}

/** A card was read that isn't linked to anything: pick its movie. */
function UnknownCard({ uid, section }: { uid: string; section: Section }) {
  const movies = useMovies("");
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState<"movie" | "music" | "book">(
    section === "music" ? "music" : section === "books" ? "book" : "movie");
  const link = useCardAction<string>(() => "cards/link", (key) => ({ uid, key }));
  const matches = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? (movies.data?.movies ?? []).filter((m) => m.title.toLowerCase().includes(q)).slice(0, 8) : [];
  }, [movies.data, search]);

  return (
    <section className="card attention">
      <h2>Card not linked</h2>
      <p>A reader just read card <code>{uid}</code>, which isn't linked to anything. Find what it should play:</p>
      {(
        <div className="segmented" role="radiogroup" aria-label="Link it to">
          {(["movie", "music", "book"] as const).map((k) => (
            <button key={k} role="radio" aria-checked={kind === k} className={kind === k ? "on" : ""}
                    onClick={() => setKind(k)}>
              {{ movie: "A movie", music: "Music", book: "An audiobook" }[k]}
            </button>
          ))}
        </div>
      )}
      {kind !== "movie" ? (
        <p className="muted">Find the {kind === "music" ? "music" : "book"} under{" "}
          <a href={kind === "music" ? "#music/library" : "#books/library"}>{kind === "music" ? "Music" : "Books"} ›
          Library</a>, open it, and use <b>Link card {uid}</b>.</p>
      ) : (
        <>
      <input type="search" placeholder="Search titles" value={search} autoFocus
             onChange={(e) => setSearch(e.target.value)} aria-label="Find the card's movie" />
      <div className="matches">
        {matches.map((m) => (
          <button key={m.key} disabled={link.isPending} onClick={() => link.mutate(m.key)}>
            <img className="thumb" src={posterURL(m.key, 80, 120)} alt="" loading="lazy" />
            <span>{m.title} <span className="muted">{m.year ?? ""}</span></span>
          </button>
        ))}
      </div>
        </>
      )}
    </section>
  );
}

function LinkedCards({ state, canManage, section }: { state: CardsState; canManage: boolean; section: Section }) {
  const [search, setSearch] = useState("");
  const remove = useCardAction<string>((uid) => `cards/${encodeURIComponent(uid)}/remove`);
  const q = search.trim().toLowerCase();
  const mine = state.cards.filter((c) => sectionOf(c) === section);
  const shown = mine.filter((c) => !q || `${c.title} ${c.uid}`.toLowerCase().includes(q));

  return (
    <section className="card">
      <h2>Linked cards</h2>
      {!mine.length ? (
        <p className="muted">No {section === "movies" ? "movie" : section === "music" ? "music" : "audiobook"} cards linked yet.</p>
      ) : (
        <>
          <div className="toolbar">
            <input type="search" placeholder="Search titles or card IDs" value={search}
                   onChange={(e) => setSearch(e.target.value)} aria-label="Search linked cards" />
            <span className="muted">{mine.length} card{mine.length === 1 ? "" : "s"}</span>
          </div>
          <div className="table-wrap">
            <table>
              <tbody>
                {shown.map((c) => (
                  <tr key={c.uid}>
                    <td>
                      {c.music ? <MusicArt item={c.music} className="thumb music" />
                        : <img className="thumb" src={posterURL(c.key, 80, 120)} alt="" loading="lazy" />}
                    </td>
                    <td>
                      <b>{c.title}</b>
                      {c.music && <div className="muted">{section === "books" ? "📖" : "♪"} {c.music.media_type}{c.speaker_name ? ` on ${c.speaker_name}` : ""}</div>}
                    </td>
                    <td><code>{c.uid}</code></td>
                    <td className="row end">
                      {canManage && (
                        <button disabled={remove.isPending}
                                onClick={() => confirm(`Unlink this card from ${c.title}?`) && remove.mutate(c.uid)}>
                          Unlink
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

/** Music Assistant's artwork for an item, or a note if it has none. */
function MusicArt({ item, className }: { item: MusicItem; className: string }) {
  return <Art image={item.image} className={className} icon={iconFor(item.id)} />;
}
