import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { get, type Card, type LibItem, type OpenedItem } from "./api";
import { useCardAction } from "./hooks";
import { Art, iconFor, useMusicSources, useSpeakerSelection, useSpeakers } from "./Music";

type Mode = "album" | "playlist" | "artist" | "book" | "author" | "series";
const MODES: Record<"music" | "books", [Mode, string][]> = {
  music: [["album", "Albums"], ["playlist", "Playlists"], ["artist", "An artist's albums"]],
  books: [["book", "All books"], ["author", "An author's"], ["series", "A series"]],
};

/** Choose albums (or playlists) or audiobooks and a speaker, then scan a card for each in turn. */
export function MediaScanSetup({ section, canManage, cards }:
  { section: "music" | "books"; canManage: boolean; cards: Card[] }) {
  const books = section === "books";
  const sources = useMusicSources();
  const speakers = useSpeakers();
  const [chosenSpeakers] = useSpeakerSelection();
  const browsable = (sources.data?.sources ?? []).filter((s) => !s.search_only);
  const [source, setSource] = useState("");
  const [mode, setMode] = useState<Mode>(books ? "book" : "album");
  const [parent, setParent] = useState("");  // the artist, author or series
  const [speaker, setSpeaker] = useState("");
  const [search, setSearch] = useState("");
  const [skipLinked, setSkipLinked] = useState(true);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const from = books ? "abs" : source;
  const needsParent = mode === "artist" || mode === "author" || mode === "series";

  useEffect(() => { if (!books && !source && browsable.length) setSource(browsable[0].id); }, [browsable.length]);
  useEffect(() => {
    if (!speaker && chosenSpeakers.length) setSpeaker(chosenSpeakers[0]);
  }, [chosenSpeakers.length]);
  useEffect(() => setParent(""), [mode, source]);

  // The artists, authors or series to choose from
  const parents = useQuery({
    queryKey: ["music-items", from, mode, "", 500],
    queryFn: () => get<{ items: LibItem[] }>(`music/browse?source=${encodeURIComponent(from)}&kind=${mode}&limit=500`),
    enabled: !!from && needsParent,
    retry: false,
  });
  const items = useQuery({
    queryKey: ["scan-items", from, mode, parent],
    queryFn: async () => needsParent
      ? (await get<OpenedItem>(`music/item?id=${encodeURIComponent(parent)}`)).albums ?? []
      : (await get<{ items: LibItem[] }>(`music/browse?source=${encodeURIComponent(from)}&kind=${mode}&limit=1000`)).items,
    enabled: !!from && (!needsParent || !!parent),
    retry: false,
  });
  const all = items.data ?? [];
  // Everything is picked when the list changes; untick what you don't want
  useEffect(() => setPicked(new Set(all.map((i) => i.id))), [items.data]);

  const hasCard = useMemo(() => new Set(cards.map((c) => c.music?.id).filter(Boolean)), [cards]);
  const queue = all.filter((i) => picked.has(i.id) && !(skipLinked && hasCard.has(i.id)));
  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? all.filter((i) => `${i.name} ${i.artist}`.toLowerCase().includes(q)) : all;
  }, [all, search]);
  const toggle = (id: string) => setPicked((p) => {
    const next = new Set(p);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const start = useCardAction<LibItem[]>(() => "cards/scan", (list) => ({
    movies: list.map((i) => ({ music: { id: i.id, name: i.name, artist: i.artist, image: i.image }, speaker })),
  }));
  const thing = books ? "book" : "album";
  const speakerList = speakers.data?.speakers.filter((s) => !s.synced_to && !s.limited) ?? [];

  return (
    <section className="card">
      <h2>Scan cards</h2>
      <p className="muted">Link blank NFC cards to {books ? "audiobooks" : "albums or playlists"} without writing anything on
        them. Choose them and the speaker they play on, start the scan, then hold each {thing}'s card on a Movie Remote when
        it comes up here.{books && " A book card picks up where you left off."}</p>

      <div className="toolbar">
        {!books && (
          <select value={source} onChange={(e) => setSource(e.target.value)} aria-label="Source">
            {!browsable.length && <option value="">No music sources</option>}
            {browsable.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        )}
        <div className="segmented" role="radiogroup" aria-label="Choose">
          {MODES[section].map(([m, label]) => (
            <button key={m} role="radio" aria-checked={mode === m} className={mode === m ? "on" : ""}
                    onClick={() => setMode(m)}>{label}</button>
          ))}
        </div>
        {needsParent && (
          <select value={parent} onChange={(e) => setParent(e.target.value)} aria-label={mode} disabled={parents.isPending}>
            <option value="">{parents.isPending ? "Loading…" : `Choose ${mode === "artist" ? "an artist" : mode === "author" ? "an author" : "a series"}`}</option>
            {parents.data?.items.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        )}
      </div>

      {items.isError && <p className="err">{items.error.message}</p>}
      {(items.isFetching || parents.isFetching) && <p className="muted">Loading…</p>}
      {items.data && (
        <>
          <div className="toolbar">
            <input type="search" placeholder={`Search ${thing}s`} value={search} onChange={(e) => setSearch(e.target.value)}
                   aria-label={`Search ${thing}s`} />
            <span className="muted">{picked.size} picked</span>
            <button onClick={() => setPicked(new Set([...picked, ...shown.map((i) => i.id)]))}>Pick all shown</button>
            <button disabled={!picked.size} onClick={() => setPicked(new Set())}>Clear</button>
          </div>
          <div className="pick-list">
            {!all.length && <p className="muted">Nothing here.</p>}
            {shown.map((i) => (
              <label key={i.id} className={`pick ${picked.has(i.id) ? "on" : ""}`}>
                <input type="checkbox" checked={picked.has(i.id)} onChange={() => toggle(i.id)} />
                <Art image={i.image} className="thumb music" icon={iconFor(i.id)} />
                <span><b>{i.name}</b> <span className="muted">{[i.artist, i.year].filter(Boolean).join(" · ")}</span></span>
                {hasCard.has(i.id) && <span className="pill good">Has a card</span>}
              </label>
            ))}
          </div>
        </>
      )}

      <div className="toolbar">
        <select value={speaker} onChange={(e) => setSpeaker(e.target.value)} aria-label="Speaker the cards play on">
          <option value="">Choose the speaker they play on</option>
          {speakerList.map((s) => <option key={s.id} value={s.id}>{s.name}{s.group ? " (group)" : ""}</option>)}
        </select>
        <label className="row">
          <input type="checkbox" checked={skipLinked} onChange={(e) => setSkipLinked(e.target.checked)} />
          Skip {thing}s that already have a card
        </label>
      </div>
      <div className="row scan-start">
        <button className="primary" disabled={!canManage || !queue.length || !speaker || start.isPending}
                title={!speaker ? "Choose a speaker first" : ""} onClick={() => start.mutate(queue)}>
          {queue.length ? `Scan ${queue.length} card${queue.length === 1 ? "" : "s"}` : "Scan cards"}
        </button>
        {items.data && skipLinked && all.some((i) => picked.has(i.id) && hasCard.has(i.id)) && (
          <span className="muted">{all.filter((i) => picked.has(i.id) && hasCard.has(i.id)).length} already have a card</span>
        )}
      </div>
    </section>
  );
}
