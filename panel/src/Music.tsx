import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { get, post, type CardsState, type LibItem, type MusicSources, type MusicStatus, type Overview, type Speaker,
  type Zone } from "./api";
import { Dialog } from "./Dialog";
import { FolderPicker } from "./FolderPicker";
import { useCards, useMusic } from "./hooks";
import { useToast } from "./toast";

const SPEAKERS_KEY = "marquee.music-speakers";
const KIND_LABELS = { album: "Albums", artist: "Artists", playlist: "Playlists", track: "Tracks" } as const;
type Kind = keyof typeof KIND_LABELS;
const SPEAKER_KINDS: Record<Speaker["kind"], string> = {
  music_assistant: "Music Assistant", airplay: "AirPlay", cast: "Cast",
};

const useMusicSources = () =>
  useQuery({ queryKey: ["music-sources"], queryFn: () => get<MusicSources>("music/sources"),
             refetchInterval: (q) => (q.state.data?.folders.some((f) => f.scanning) ? 2000 : 30000) });
const useSpeakers = () =>
  useQuery({ queryKey: ["music-speakers"], queryFn: () => get<{ speakers: Speaker[] }>("music/speakers"),
             refetchInterval: 10000 });
const useZones = () =>
  useQuery({ queryKey: ["music-now"], queryFn: () => get<{ zones: Zone[] }>("music/now"), refetchInterval: 3000 });

/** A POST from the Music tab: toasts its error, refreshes what's playing. */
function useMusicAction<V>(path: string, body: (v: V) => object, done?: (v: V) => string) {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (v: V) => post(path, body(v)),
    onSuccess: (_, v) => {
      client.invalidateQueries({ queryKey: ["music-now"] });
      client.invalidateQueries({ queryKey: ["music-speakers"] });
      if (done) toast(done(v));
    },
    onError: (error) => toast(error.message, "error"),
  });
}

const time = (s?: number | null) =>
  s == null ? "" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

/** Music from every source, played on AirPlay, Cast and Music Assistant speakers. */
export function Music({ overview }: { overview: Overview }) {
  const canManage = overview.can_manage;
  const [selected, setSelected] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem(SPEAKERS_KEY) ?? "[]"); } catch { return []; }
  });
  const choose = (ids: string[]) => {
    setSelected(ids);
    try { localStorage.setItem(SPEAKERS_KEY, JSON.stringify(ids)); } catch { /* fine */ }
  };
  const cards = useCards();
  return (
    <>
      {cards.data?.scan && <CardScan state={cards.data} canManage={canManage} />}
      <Zones canManage={canManage} />
      <Speakers selected={selected} onChange={choose} canManage={canManage} />
      <Library selected={selected} canManage={canManage} unknown={cards.data?.unknown?.uid ?? null} />
      {canManage && <MusicSettings />}
    </>
  );
}

// ---- what's playing ----------------------------------------------------------------

function Zones({ canManage }: { canManage: boolean }) {
  const zones = useZones();
  if (!zones.data?.zones.length) return null;
  return (
    <section className="card">
      <h2>Playing</h2>
      <div className="zones">
        {zones.data.zones.map((z) => <ZoneView key={z.id} zone={z} canManage={canManage} />)}
      </div>
    </section>
  );
}

function ZoneView({ zone, canManage }: { zone: Zone; canManage: boolean }) {
  const control = useMusicAction<{ action: string; value?: number }>("music/control",
                                                                      (v) => ({ zone: zone.id, ...v }));
  const [showQueue, setShowQueue] = useState(false);
  const paused = zone.state === "paused";
  return (
    <div className="zone">
      <Art image={zone.track.image} className="zone-art" />
      <div className="zone-body">
        <b>{zone.track.name || "—"}</b>
        <div className="muted">{[zone.track.artist, zone.track.album].filter(Boolean).join(" · ")}</div>
        <div className="muted small">
          {zone.names.join(" + ")}
          {zone.track.duration ? ` · ${time(zone.position)} / ${time(zone.track.duration)}` : ""}
          {zone.kind === "marquee" && zone.speakers.length > 1 && " · not in sync"}
        </div>
        {zone.error && <div className="err small">{zone.error}</div>}
        {canManage && (
          <div className="row controls">
            <button onClick={() => control.mutate({ action: "previous" })} aria-label="Previous">⏮</button>
            <button className="primary" onClick={() => control.mutate({ action: paused ? "play" : "pause" })}
                    aria-label={paused ? "Play" : "Pause"}
                    title={paused && zone.restarts_on_resume ? "AirPlay starts the track again" : ""}>
              {paused ? "▶" : "⏸"}
            </button>
            <button onClick={() => control.mutate({ action: "next" })} aria-label="Next">⏭</button>
            <button onClick={() => control.mutate({ action: "stop" })} aria-label="Stop">⏹</button>
            <button className="link" onClick={() => setShowQueue(!showQueue)}>{showQueue ? "Hide queue" : "Queue"}</button>
          </div>
        )}
        {showQueue && <Queue zone={zone} canManage={canManage} onJump={(i) => control.mutate({ action: "jump", value: i })} />}
      </div>
    </div>
  );
}

function Queue({ zone, canManage, onJump }: { zone: Zone; canManage: boolean; onJump: (i: number) => void }) {
  const queue = useQuery({
    queryKey: ["music-queue", zone.id],
    queryFn: () => get<{ index: number; items: { name: string; artist: string; duration?: number | null }[] }>(
      `music/queue?zone=${encodeURIComponent(zone.id)}`),
    refetchInterval: 5000,
  });
  if (queue.isError) return <p className="err small">{queue.error.message}</p>;
  return (
    <ol className="queue">
      {queue.data?.items.map((t, i) => (
        <li key={i} className={i === queue.data.index ? "on" : ""}>
          <button className="link" disabled={!canManage} onClick={() => onJump(i)}>
            {t.name}{t.artist && <span className="muted"> · {t.artist}</span>}
          </button>
          <span className="muted small">{time(t.duration)}</span>
        </li>
      ))}
    </ol>
  );
}

// ---- speakers ----------------------------------------------------------------------

function Speakers({ selected, onChange, canManage }:
  { selected: string[]; onChange: (ids: string[]) => void; canManage: boolean }) {
  const speakers = useSpeakers();
  const list = speakers.data?.speakers ?? [];
  const groups = useMemo(() => {
    const out: Partial<Record<Speaker["kind"], Speaker[]>> = {};
    for (const s of list) if (!s.synced_to) (out[s.kind] ??= []).push(s);
    return out;
  }, [list]);
  const toggle = (id: string) => onChange(selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id]);
  const chosen = list.filter((s) => selected.includes(s.id));
  const mixed = new Set(chosen.map((s) => s.kind)).size > 1;

  return (
    <section className="card">
      <div className="card-head">
        <h2>Speakers</h2>
        {selected.length > 0 && <button className="link" onClick={() => onChange([])}>Clear</button>}
      </div>
      {speakers.isPending && <p className="muted">Looking for speakers…</p>}
      {speakers.data && !list.length && (
        <p className="muted">No speakers yet. AirPlay and Cast speakers on the network show up here on their own; for
          Music Assistant's, connect it under <b>Music settings</b> below.</p>
      )}
      {(Object.keys(groups) as Speaker["kind"][]).map((kind) => (
        <div key={kind} className="speaker-group">
          <div className="muted small">{SPEAKER_KINDS[kind]}</div>
          <div className="row wrap">
            {groups[kind]!.map((s) => (
              <SpeakerChip key={s.id} speaker={s} on={selected.includes(s.id)} onToggle={() => toggle(s.id)}
                           canManage={canManage} all={list} />
            ))}
          </div>
        </div>
      ))}
      {chosen.length > 1 && (
        <p className="muted small">
          {mixed ? "These play the same music, but AirPlay, Cast and Music Assistant speakers aren't in sync with each other."
            : chosen[0].kind === "music_assistant" ? "Music Assistant plays these in sync, as a group."
              : "Marquee sends these the same tracks; they may drift apart a little. For Cast speakers in sync, use a speaker group from Google Home."}
        </p>
      )}
    </section>
  );
}

function SpeakerChip({ speaker, on, onToggle, canManage, all }:
  { speaker: Speaker; on: boolean; onToggle: () => void; canManage: boolean; all: Speaker[] }) {
  const [level, setLevel] = useState<number | null>(null);
  const volume = useMusicAction<number>("music/volume", (l) => ({ speaker: speaker.id, level: l }));
  const ungroup = useMusicAction<void>("music/group", () => ({ leader: speaker.id, members: [] }), () => "Ungrouped");
  useEffect(() => {
    if (level == null) return;
    const t = setTimeout(() => volume.mutate(level), 300);
    return () => clearTimeout(t);
  }, [level]);
  const members = speaker.members.map((m) => all.find((s) => s.id === m)?.name ?? m);
  const shown = level ?? speaker.volume;
  return (
    <div className={`speaker${on ? " on" : ""}`}>
      <label>
        <input type="checkbox" checked={on} onChange={onToggle} />
        <span>
          {speaker.name}{speaker.group && " (group)"}
          {members.length > 0 && <span className="muted small"> + {members.join(", ")}</span>}
          {speaker.state === "playing" && <span className="playing-dot" title="Playing" />}
        </span>
      </label>
      {canManage && shown != null && (
        <input type="range" min={0} max={100} value={shown} aria-label={`${speaker.name} volume`}
               onChange={(e) => setLevel(Number(e.target.value))} />
      )}
      {canManage && members.length > 0 && (
        <button className="link small" onClick={() => ungroup.mutate()}>Ungroup</button>
      )}
      {canManage && (speaker.pairing === "needed" || speaker.pairing === "paired") && <AirPlayPairing speaker={speaker} />}
    </div>
  );
}

/** Apple TVs ask for pairing before Marquee can stream to them: they show a PIN, typed in here. */
function AirPlayPairing({ speaker }: { speaker: Speaker }) {
  const [pairId, setPairId] = useState<string | null>(null);
  const [pin, setPin] = useState("");
  const start = useMusicAction<void>("music/airplay/pair", () => ({ speaker: speaker.id }));
  const finish = useMusicAction<void>("music/airplay/pair/finish", () => ({ pair_id: pairId, pin }),
                                      () => `Paired ${speaker.name}`);
  const unpair = useMusicAction<void>("music/airplay/unpair", () => ({ speaker: speaker.id }),
                                      () => `Unpaired ${speaker.name}`);
  if (speaker.pairing === "paired") {
    return <button className="link small" onClick={() => unpair.mutate()}>Unpair</button>;
  }
  if (!pairId) {
    return (
      <button className="small" disabled={start.isPending}
              onClick={() => start.mutate(undefined, { onSuccess: (r) => setPairId(String(r.pair_id)) })}>
        {start.isPending ? "Asking…" : "Pair"}
      </button>
    );
  }
  return (
    <form className="pair-pin" onSubmit={(e) => {
      e.preventDefault();
      finish.mutate(undefined, { onSettled: () => { setPairId(null); setPin(""); } });
    }}>
      <input inputMode="numeric" autoComplete="one-time-code" placeholder="PIN on the TV" value={pin} autoFocus
             onChange={(e) => setPin(e.target.value)} aria-label={`PIN shown by ${speaker.name}`} />
      <button type="submit" className="small" disabled={pin.trim().length < 4 || finish.isPending}>Pair</button>
      <button type="button" className="link small" onClick={() => setPairId(null)}>Cancel</button>
    </form>
  );
}

// ---- the library -------------------------------------------------------------------

function Library({ selected, canManage, unknown }: { selected: string[]; canManage: boolean; unknown: string | null }) {
  const sources = useMusicSources();
  const list = sources.data?.sources ?? [];
  const [source, setSource] = useState("");
  const [kind, setKind] = useState<Kind>("album");
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(60);
  const [open, setOpen] = useState<string[]>([]);  // the items opened, last on top
  useEffect(() => {
    const t = setTimeout(() => setQuery(typed.trim()), 350);
    return () => clearTimeout(t);
  }, [typed]);
  useEffect(() => setLimit(60), [source, kind, query]);
  const current = list.find((s) => s.id === source) ?? null;
  const browsing = !!current && !current.search_only;
  const items = useQuery({
    queryKey: ["music-items", source, kind, query, limit],
    queryFn: () => browsing
      ? get<{ items: LibItem[] }>(`music/browse?source=${encodeURIComponent(source)}&kind=${kind}` +
                                  `&q=${encodeURIComponent(query)}&limit=${limit}`)
      : get<{ items: LibItem[] }>(`music/search?q=${encodeURIComponent(query)}` +
                                  (source ? `&source=${encodeURIComponent(source)}` : "")),
    enabled: browsing || query.length > 1,
    placeholderData: (previous) => previous,
    retry: false,
  });

  if (sources.data && !list.length) {
    return (
      <section className="card">
        <h2>Library</h2>
        <p className="muted">No music yet. Add a music folder or connect Music Assistant under <b>Music settings</b>;
          Plex music libraries and Jellyfin servers added on the Sources tab show up here too.</p>
      </section>
    );
  }
  const top = open[open.length - 1];
  return (
    <section className="card">
      <h2>Library</h2>
      {top ? (
        <ItemView id={top} selected={selected} canManage={canManage} unknown={unknown}
                  onOpen={(id) => setOpen([...open, id])} onBack={() => setOpen(open.slice(0, -1))} />
      ) : (
        <>
          <div className="toolbar">
            <select value={source} onChange={(e) => setSource(e.target.value)} aria-label="Source">
              <option value="">Search everywhere</option>
              {list.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            {browsing && (
              <div className="segmented" role="radiogroup" aria-label="Show">
                {(Object.keys(KIND_LABELS) as Kind[]).map((k) => (
                  <button key={k} role="radio" aria-checked={kind === k} className={kind === k ? "on" : ""}
                          onClick={() => setKind(k)}>{KIND_LABELS[k]}</button>
                ))}
              </div>
            )}
            <input type="search" value={typed} onChange={(e) => setTyped(e.target.value)}
                   placeholder={browsing ? `Filter ${KIND_LABELS[kind].toLowerCase()}` : "Search artists, albums, tracks…"}
                   aria-label="Search music" />
          </div>
          {!browsing && query.length < 2 && <p className="muted">Type to search every source, or choose one to browse.</p>}
          {items.isError && <p className="err">{items.error.message}</p>}
          {items.isFetching && !items.data && <p className="muted">Loading…</p>}
          {items.data && !items.data.items.length && (browsing || query.length > 1) && <p className="muted">Nothing found.</p>}
          <div className="music-grid">
            {items.data?.items.map((item) => (
              <Tile key={item.id} item={item} showSource={!browsing ? list.find((s) => s.id === item.source)?.name : undefined}
                    onOpen={() => setOpen([item.id])} />
            ))}
          </div>
          {browsing && items.data && items.data.items.length >= limit && (
            <p className="row end"><button onClick={() => setLimit(limit + 60)}>More</button></p>
          )}
        </>
      )}
    </section>
  );
}

function Tile({ item, onOpen, showSource }: { item: LibItem; onOpen: () => void; showSource?: string }) {
  return (
    <button className={`tile${item.kind === "artist" ? " round" : ""}`} onClick={onOpen}>
      <Art image={item.image} className="tile-art" />
      <b>{item.name}</b>
      <span className="muted small">
        {[item.kind !== "album" && item.kind, item.artist, item.year, showSource].filter(Boolean).join(" · ")}
      </span>
    </button>
  );
}

function ItemView({ id, selected, canManage, unknown, onOpen, onBack }:
  { id: string; selected: string[]; canManage: boolean; unknown: string | null; onOpen: (id: string) => void;
    onBack: () => void }) {
  const opened = useQuery({
    queryKey: ["music-item", id],
    queryFn: () => get<{ item: LibItem; albums?: LibItem[]; tracks?: LibItem[] }>(`music/item?id=${encodeURIComponent(id)}`),
    retry: false,
  });
  const play = useMusicAction<{ ids: string[]; option: string; start?: number }>(
    "music/play", (v) => ({ ...v, speakers: selected }),
    (v) => (v.option === "replace" ? "Playing" : v.option === "next" ? "Playing next" : "Added to the queue"));
  const item = opened.data?.item;
  const noSpeaker = selected.length === 0;
  return (
    <>
      <p><button className="link" onClick={onBack}>‹ Back</button></p>
      {opened.isError && <p className="err">{opened.error.message}</p>}
      {opened.isPending && <p className="muted">Loading…</p>}
      {item && (
        <div className="item-head">
          <Art image={item.image} className={`item-art${item.kind === "artist" ? " round" : ""}`} />
          <div>
            <div className="muted small">{item.kind}</div>
            <h3>{item.name}</h3>
            <div className="muted">{[item.artist, item.year].filter(Boolean).join(" · ")}</div>
            {canManage && (
              <div className="row wrap actions">
                <button className="primary" disabled={noSpeaker || play.isPending} title={noSpeaker ? "Choose a speaker first" : ""}
                        onClick={() => play.mutate({ ids: [item.id], option: "replace" })}>Play</button>
                <button disabled={noSpeaker || play.isPending} onClick={() => play.mutate({ ids: [item.id], option: "next" })}>
                  Play next</button>
                <button disabled={noSpeaker || play.isPending} onClick={() => play.mutate({ ids: [item.id], option: "add" })}>
                  Add to queue</button>
                <LinkCard item={item} selected={selected} unknown={unknown} />
              </div>
            )}
          </div>
        </div>
      )}
      {opened.data?.albums && (
        <div className="music-grid">
          {opened.data.albums.map((a) => <Tile key={a.id} item={a} onOpen={() => onOpen(a.id)} />)}
        </div>
      )}
      {opened.data?.tracks && item?.kind !== "track" && (
        <ol className="tracks">
          {opened.data.tracks.map((t, i) => (
            <li key={`${t.id}-${i}`}>
              <span className="muted num">{t.number ?? i + 1}</span>
              <span className="grow">
                {t.name}
                {t.artist && t.artist !== item?.artist && <span className="muted"> · {t.artist}</span>}
              </span>
              <span className="muted small">{time(t.duration)}</span>
              {canManage && (
                <span className="row nowrap">
                  <button className="link" disabled={noSpeaker} title="Play from here"
                          onClick={() => play.mutate({ ids: [id], option: "replace", start: i })}>▶</button>
                  <button className="link" disabled={noSpeaker} title="Add to the queue"
                          onClick={() => play.mutate({ ids: [t.id], option: "add" })}>+</button>
                </span>
              )}
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

/** Link a card to this item on the chosen speaker: the card just read, or the next one. */
function LinkCard({ item, selected, unknown }: { item: LibItem; selected: string[]; unknown: string | null }) {
  const client = useQueryClient();
  const toast = useToast();
  const music = { id: item.id, name: item.name, artist: item.artist, image: item.image };
  const link = useMutation({
    mutationFn: (uid: string | null) => uid
      ? post<CardsState>("cards/link", { uid, music, speaker: selected[0] })
      : post<CardsState>("cards/scan", { movies: [{ music, speaker: selected[0] }] }),
    onSuccess: (state, uid) => {
      client.setQueryData(["cards"], state);
      toast(uid ? `Card ${uid} linked` : "Hold a blank card on a reader");
    },
    onError: (error) => toast(error.message, "error"),
  });
  const one = selected.length === 1;
  const why = one ? "" : "Choose one speaker for the card";
  return (
    <>
      {unknown && (
        <button disabled={!one || link.isPending} title={why || `Link card ${unknown}, which was just read`}
                onClick={() => link.mutate(unknown)}>Link card {unknown}</button>
      )}
      <button disabled={!one || link.isPending} title={why || "Then hold a blank card on a Movie Remote or a reader in Home Assistant"}
              onClick={() => link.mutate(null)}>Link next card</button>
    </>
  );
}

function CardScan({ state, canManage }: { state: CardsState; canManage: boolean }) {
  const client = useQueryClient();
  const stop = useMutation({
    mutationFn: () => post<CardsState>("cards/scan/stop"),
    onSuccess: (s) => client.setQueryData(["cards"], s),
  });
  const scan = state.scan!;
  return (
    <section className="card attention">
      <div className="card-head">
        <h2>Linking a card</h2>
        {canManage && <button onClick={() => stop.mutate()}>Stop</button>}
      </div>
      <p>Hold a blank card on a Movie Remote or an NFC reader in Home Assistant for
        {" "}<b>{scan.current?.title ?? "the next item"}</b>
        {scan.current?.speaker_name && <> on {scan.current.speaker_name}</>}.</p>
      {scan.last && <p className="muted">Card {scan.last.uid} plays {scan.last.title}.</p>}
    </section>
  );
}

// ---- settings ----------------------------------------------------------------------

function MusicSettings() {
  const sources = useMusicSources();
  const status = useMusic();
  const client = useQueryClient();
  const toast = useToast();
  const [picking, setPicking] = useState(false);
  const [assistant, setAssistant] = useState(false);
  const done = (message: string) => ({
    onSuccess: (data: unknown) => {
      client.setQueryData(["music-sources"], data);
      client.invalidateQueries({ queryKey: ["music-items"] });
      toast(message);
    },
    onError: (error: Error) => toast(error.message, "error"),
  });
  const add = useMutation({ mutationFn: (path: string) => post("music/folders", { path }), ...done("Folder added") });
  const remove = useMutation({ mutationFn: (id: string) => post(`music/folders/${id}/remove`), ...done("Folder removed") });
  const rescan = useMutation({ mutationFn: (id: string) => post(`music/folders/${id}/rescan`), ...done("Reading the folder again") });
  const data = sources.data;
  const ma = status.data;
  return (
    <section className="card">
      <h2>Music settings</h2>
      <h3>Music folders</h3>
      <p className="muted">Folders of music files on this server or a mounted share. Albums come from the files' tags,
        or from Artist/Album folders; .m3u files become playlists.</p>
      {data?.folders.map((f) => (
        <div key={f.id} className="row folder-row">
          <span className="grow"><b>{f.name}</b> <span className="muted small">{f.path}</span></span>
          <span className="muted small">{f.scanning ? "Reading…" : `${f.tracks} tracks`}</span>
          <button className="link" onClick={() => rescan.mutate(f.id)}>Read again</button>
          <button className="link danger" onClick={() => confirm(`Remove ${f.name}? Its files stay where they are.`) && remove.mutate(f.id)}>
            Remove</button>
        </div>
      ))}
      <p><button onClick={() => setPicking(true)}>Add a folder…</button></p>
      {picking && <FolderPicker start="" onClose={() => setPicking(false)}
                                onPick={(path) => { setPicking(false); add.mutate(path); }} />}

      <h3>Music Assistant</h3>
      <div className="row">
        {ma?.assistant.connected
          ? <span className="pill good"><span className="dot" /> Connected directly</span>
          : ma?.assistant.url
            ? <span className="pill bad" title={ma.assistant.message ?? ""}><span className="dot" /> Can't reach it</span>
            : <span className="pill"><span className="dot" /> Not connected directly</span>}
        {ma?.home_assistant && (
          <span className={`pill${ma.via === "home_assistant" ? " good" : ""}`}>
            <span className="dot" /> Home Assistant{ma.via === "home_assistant" ? " (in use)" : ""}
          </span>
        )}
        <button className="link" onClick={() => setAssistant(true)}>{ma?.assistant.url ? "Change" : "Connect"}</button>
      </div>
      {ma && !ma.assistant.connected && ma.assistant.message && <p className="err">{ma.assistant.message}</p>}
      {assistant && ma && <MusicAssistantSetup status={ma} onClose={() => setAssistant(false)} />}

      <h3>AirPlay and Cast</h3>
      <p className="muted">
        AirPlay: {data?.speakers.airplay ? "on" : "needs pyatv on the server"} · Cast: {data?.speakers.cast ? "on" : "needs pychromecast on the server"}.
        {" "}HomePods and Apple TVs need <b>Allow speaker access</b> set to everyone on the same network (in the Home app).
        AirPlay can't pause a stream, so ▶ after ⏸ starts the track again. File types an AirPlay or Cast speaker can't play
        go through ffmpeg, if the server has it.
      </p>
    </section>
  );
}

/** Music Assistant's address and a token, so Marquee reaches it without Home Assistant. */
export function MusicAssistantSetup({ status, onClose }: { status: MusicStatus; onClose: () => void }) {
  const client = useQueryClient();
  const toast = useToast();
  const [url, setUrl] = useState(status.assistant.url);
  const [token, setToken] = useState("");
  const save = useMutation({
    mutationFn: (body: { url: string; token?: string }) => post<MusicStatus & { ok: boolean }>("music/assistant", body),
    onSuccess: (s, body) => {
      client.setQueryData(["music"], s);
      for (const key of ["music-sources", "music-speakers", "music-items"]) client.invalidateQueries({ queryKey: [key] });
      toast(body.url ? "Connected to Music Assistant" : "Music Assistant disconnected");
      onClose();
    },
    onError: (error) => toast(error.message, "error"),
  });
  return (
    <Dialog title="Music Assistant" onClose={onClose}>
      <p className="muted">Marquee can talk to Music Assistant itself, for its library and speakers, even when Home
        Assistant is down. Give it Music Assistant's address (port 8095, such as http://homeassistant.local:8095) and a
        long-lived token: in Music Assistant, open <b>Settings › Profile</b>, and under <b>Long-lived access
        tokens</b> make one called Marquee. The token is kept on this server only.</p>
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(token ? { url, token } : { url }); }}>
        <p className="row nowrap">
          <input value={url} onChange={(e) => setUrl(e.target.value)} required className="grow" autoComplete="off"
                 aria-label="Music Assistant's address" placeholder="http://homeassistant.local:8095" />
        </p>
        <p className="row nowrap">
          <input value={token} onChange={(e) => setToken(e.target.value)} required={!status.assistant.token}
                 type="password" autoComplete="off" className="grow" aria-label="Music Assistant token"
                 placeholder={status.assistant.token ? "Keep the token there is" : "Long-lived token"} />
          <button className="primary" disabled={save.isPending}>{save.isPending ? "Checking…" : "Connect"}</button>
        </p>
      </form>
      {status.assistant.url && (
        <p className="row end dialog-foot">
          <button className="danger" disabled={save.isPending}
                  onClick={() => confirm("Stop talking to Music Assistant directly? Music cards then go through Home Assistant, if it's connected.")
                    && save.mutate({ url: "" })}>Disconnect</button>
        </p>
      )}
    </Dialog>
  );
}

/** A picture, or a note if there isn't one (or it doesn't load). */
export function Art({ image, className }: { image?: string | null; className: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [image]);
  return image && !failed ? (
    <img className={className} src={image} alt="" loading="lazy" onError={() => setFailed(true)} />
  ) : (
    <div className={`${className} no-art`} aria-hidden>♪</div>
  );
}
