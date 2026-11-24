import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { get, post, type Overview, type Reader, type ReadersState } from "./api";
import { useToast } from "./toast";

const useReaders = () => useQuery({ queryKey: ["readers"], queryFn: () => get<ReadersState>("readers"), refetchInterval: 15_000 });

/** NFC readers, each paired to a default output: the speakers its music and book cards play on. */
export function Readers({ overview }: { overview: Overview }) {
  const readers = useReaders();
  const list = readers.data?.readers ?? [];
  return (
    <section className="card">
      <h2>Readers</h2>
      <p className="muted">Each NFC reader plays music and book cards on its own speakers, whatever speaker the card
        was linked with, so a card plays in the room where it's held. Choose its <b>default output</b> here. On the
        reader (a Movie Remote's speaker screen, or the controls Home Assistant gives a Tessera screen) the speakers
        can be changed, and are remembered until <b>Restore default</b>, or <b>Make default</b> keeps them.</p>
      {readers.isError && <p className="err">{readers.error.message}</p>}
      {readers.data && !list.length && (
        <p className="muted">No readers yet. Pair a Movie Remote above, or hold any card to a reader in Home Assistant
          (with the Marquee integration) and it shows up here.</p>
      )}
      <div className="readers">
        {list.map((r) => <ReaderCard key={r.id} reader={r} state={readers.data!} canManage={overview.can_manage} />)}
      </div>
    </section>
  );
}

function ReaderCard({ reader, state, canManage }: { reader: Reader; state: ReadersState; canManage: boolean }) {
  const client = useQueryClient();
  const toast = useToast();
  const [page, setPage] = useState(reader.page ? String(reader.page) : "");
  useEffect(() => setPage(reader.page ? String(reader.page) : ""), [reader.page]);
  const save = useMutation({
    mutationFn: (body: object) => post<ReadersState>(`readers/${encodeURIComponent(reader.id)}`, body),
    onSuccess: (next) => { client.setQueryData(["readers"], next); toast(`${reader.name} saved`); },
    onError: (e) => toast(e.message, "error"),
  });
  const remove = useMutation({
    mutationFn: () => post<ReadersState>(`readers/${encodeURIComponent(reader.id)}/remove`, {}),
    onSuccess: (next) => client.setQueryData(["readers"], next),
    onError: (e) => toast(e.message, "error"),
  });
  const toggle = (id: string) => {
    const next = reader.default.includes(id) ? reader.default.filter((s) => s !== id) : [...reader.default, id];
    save.mutate({ default: next });
  };
  const known = new Set(state.speakers.map((s) => s.id));
  const missing = reader.default.filter((s) => !known.has(s));
  return (
    <div className="reader">
      <div className="row between">
        <div>
          <b>{reader.name}</b> <span className={`kind ${reader.kind}`}>{reader.kind === "remote" ? "Movie Remote" : "Home Assistant reader"}</span>
          <div className="muted small">
            {reader.now ? `${reader.now.state === "paused" ? "Paused" : "Playing"}: ${reader.now.title} · ` : ""}
            Plays on {reader.speaker_names.join(" + ") || "each card's own speaker"}
            {!reader.is_default && reader.default.length > 0 && " (chosen on the reader)"}
          </div>
        </div>
        {canManage && reader.kind === "homeassistant" && (
          <button className="link danger" disabled={remove.isPending}
                  onClick={() => confirm(`Forget ${reader.name}? It shows up again when it reads a card.`) && remove.mutate()}>
            Forget</button>
        )}
      </div>
      <fieldset disabled={!canManage || save.isPending}>
        <legend className="muted small">Default output (music and books)</legend>
        <div className="row wrap">
          {state.speakers.map((s) => (
            <label key={s.id} className={`speaker-pick ${reader.default.includes(s.id) ? "on" : ""}`}>
              <input type="checkbox" checked={reader.default.includes(s.id)} onChange={() => toggle(s.id)} />
              {s.name}{s.group ? " (group)" : ""}
            </label>
          ))}
          {missing.map((s) => (
            <label key={s} className="speaker-pick on" title="Not found right now">
              <input type="checkbox" checked onChange={() => toggle(s)} />{s} (away)
            </label>
          ))}
          {!state.speakers.length && <span className="muted">No speakers found.</span>}
        </div>
        <div className="row wrap">
          <label className="row">Movies play on
            <select value={reader.movie_player ?? ""} aria-label={`Movie player for ${reader.name}`}
                    onChange={(e) => save.mutate({ movie_player: e.target.value || null })}>
              <option value="">The default player</option>
              {state.players.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          {reader.kind === "homeassistant" && (
            <form className="row" onSubmit={(e) => { e.preventDefault(); save.mutate({ page: page ? Number(page) : null }); }}>
              <label className="row">Tessera page
                <input type="number" min={1} max={99} value={page} onChange={(e) => setPage(e.target.value)}
                       placeholder="none" style={{ width: "5em" }} aria-label={`Tessera page for ${reader.name}`} />
              </label>
              {page !== (reader.page ? String(reader.page) : "") && <button>Save</button>}
            </form>
          )}
        </div>
      </fieldset>
    </div>
  );
}
