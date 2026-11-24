import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { EditMovie } from "./EditMovie";
import { posterURL, type Movie, type Overview } from "./api";
import { useAction, useCardAction, useMovies, usePlayers } from "./hooks";
import { useToast } from "./toast";

const column = createColumnHelper<Movie>();

// Web NFC: only Chrome on Android, and only over HTTPS
declare global {
  interface Window { NDEFReader?: new () => { write(message: object): Promise<void> } }
}
const canWriteTags = typeof window !== "undefined" && "NDEFReader" in window;

/** Every movie's tag address: search, sort, copy, write, or try one on a player. */
export function Tags({ overview }: { overview: Overview }) {
  const [collection, setCollection] = useState("");
  const [typed, setTyped] = useState("");
  const movies = useMovies(collection);
  const manySources = new Set(movies.data?.movies.map((m) => m.source)).size > 1;
  const [filter, setFilter] = useState("");
  const [sorting, setSorting] = useState<SortingState>([{ id: "title", desc: false }]);
  const [player, setPlayer] = useState("");
  const [editing, setEditing] = useState<Movie | null>(null);
  const [posters, setPosters] = useState(0);  // changes when a movie's poster may have changed
  const players = usePlayers();
  const toast = useToast();
  // One movie's card: a scan of just that movie, on the Cards tab
  const linkCard = useCardAction<Movie>(() => "cards/scan", (m) => ({ movies: [{ key: m.key, title: m.title, year: m.year }] }));
  const play = useAction<Movie>(() => "play", (m) => ({ key: m.key, player: player || null }),
                                (m) => `Starting ${m.title}`);

  const copy = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      toast("Copied");
    } catch {
      toast("Couldn't copy: select the address instead", "error");
    }
  };
  const write = async (url: string) => {
    try {
      await new window.NDEFReader!().write({ records: [{ recordType: "url", data: url }] });
      toast(`Written: ${url}`);
    } catch (e) {
      toast(`Couldn't write the tag: ${e}`, "error");
    }
  };

  const columns = useMemo(() => [
    column.display({
      id: "poster",
      header: "",
      cell: ({ row }) => <img className="thumb" alt="" loading="lazy"
                                 src={posterURL(row.original.key, 80, 120, posters ? `${posters}` : "")} />,
    }),
    column.accessor("title", { header: "Title", cell: (c) => <b>{c.getValue()}</b> }),
    column.accessor("year", { header: "Year", cell: (c) => c.getValue() ?? "—", sortUndefined: "last" }),
    ...(manySources ? [column.accessor("source", { header: "Source", cell: (c) => c.getValue() })] : []),
    column.accessor("url", {
      header: "Tag address",
      enableSorting: false,
      cell: (c) => <code className="url">{c.getValue()}</code>,
    }),
    column.display({
      id: "actions",
      header: "",
      cell: ({ row }) => (
        <div className="row end">
          <button onClick={() => copy(row.original.url)}>Copy</button>
          {canWriteTags && <button onClick={() => write(row.original.url)}>Write tag</button>}
          {overview.can_manage && (
            <button disabled={linkCard.isPending} title={row.original.cards.length
                      ? `Has card ${row.original.cards.join(", ")}` : "Link a blank card with a Movie Remote"}
                    onClick={() => linkCard.mutate(row.original, { onSuccess: () => { location.hash = "movies/cards"; } })}>
              {row.original.cards.length ? "✓ Card" : "Link card"}
            </button>
          )}
          {overview.can_manage && row.original.files.length > 0 && (
            <button onClick={() => setEditing(row.original)} title="Match it with TMDB, or change its details">
              {row.original.key.startsWith("t-") ? "Match…" : "Edit"}
            </button>
          )}
          {overview.can_manage && (
            <button className="primary" disabled={play.isPending} onClick={() => play.mutate(row.original)}
                    title="Start it, as tapping the tag would">▶ Try</button>
          )}
        </div>
      ),
    }),
  ], [overview.can_manage, play.isPending, linkCard.isPending, player, manySources, posters]);

  const table = useReactTable({
    data: movies.data?.movies ?? [],
    columns,
    state: { globalFilter: filter, sorting },
    onGlobalFilterChange: setFilter,
    onSortingChange: setSorting,
    globalFilterFn: (row, _id, value: string) => {
      const m = row.original;
      return `${m.title} ${m.year ?? ""} ${m.key} ${m.source}`.toLowerCase().includes(value.toLowerCase());
    },
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 25 } },
  });

  const csv = `tags.csv${collection ? `?collection=${encodeURIComponent(collection)}` : ""}`;
  const shown = table.getFilteredRowModel().rows.length;

  return (
    <section className="card">
      <h2>Tags</h2>
      <p className="muted">Write each address onto the tag stuck to that movie's poster. Taps start the movie from the
        beginning; add <code>?resume=1</code> to a tag to carry on where it was left off.</p>

      <div className="toolbar">
        <input type="search" placeholder="Search titles" value={filter} onChange={(e) => setFilter(e.target.value)}
               aria-label="Search titles" />
        <form className="row" onSubmit={(e) => { e.preventDefault(); setCollection(typed.trim()); }}>
          <input placeholder="Collection (all movies)" value={typed} onChange={(e) => setTyped(e.target.value)}
                 aria-label="Collection" />
          <button>Show</button>
        </form>
        {overview.can_manage && (
          <label className="row">Try on
            <select value={player} onChange={(e) => setPlayer(e.target.value)} disabled={players.isPending}>
              <option value="">{overview.default_player_label || "The default player"} (default)</option>
              {players.data?.players.filter((p) => p.id !== overview.default_player).map((p) => (
                <option key={p.id} value={p.id}>{p.name} ({p.kind})</option>
              ))}
            </select>
          </label>
        )}
        <a className="button" href={csv}>Download CSV</a>
      </div>

      {movies.isPending && <p className="muted">Loading the library…</p>}
      {movies.isError && <p className="err">{movies.error.message}</p>}
      {movies.data && (
        <>
          <div className="table-wrap">
            <table className="tags">
              <thead>
                {table.getHeaderGroups().map((group) => (
                  <tr key={group.id}>
                    {group.headers.map((h) => (
                      <th key={h.id} aria-sort={h.column.getIsSorted() === "asc" ? "ascending"
                        : h.column.getIsSorted() === "desc" ? "descending" : undefined}>
                        {h.column.getCanSort() ? (
                          <button className="sort" onClick={h.column.getToggleSortingHandler()}>
                            {flexRender(h.column.columnDef.header, h.getContext())}
                            {{ asc: " ▲", desc: " ▼" }[h.column.getIsSorted() as string] ?? ""}
                          </button>
                        ) : flexRender(h.column.columnDef.header, h.getContext())}
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {table.getRowModel().rows.map((row) => (
                  <tr key={row.id}>
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pager">
            <span className="muted">{shown} of {movies.data.movies.length} movies</span>
            <div className="row">
              <button disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()}>‹ Previous</button>
              <span>Page {table.getState().pagination.pageIndex + 1} of {Math.max(1, table.getPageCount())}</span>
              <button disabled={!table.getCanNextPage()} onClick={() => table.nextPage()}>Next ›</button>
              <select value={table.getState().pagination.pageSize} aria-label="Movies per page"
                      onChange={(e) => table.setPageSize(Number(e.target.value))}>
                {[25, 50, 100, 500].map((n) => <option key={n} value={n}>{n} per page</option>)}
              </select>
            </div>
          </div>

          <h3>Control tags</h3>
          <p className="muted">For the back cover: they act on whatever the default player is playing.</p>
          <div className="table-wrap">
            <table>
              <tbody>
                {movies.data.controls.map((c) => (
                  <tr key={c.action}>
                    <td><b>{c.action[0].toUpperCase() + c.action.slice(1)}</b></td>
                    <td><code className="url">{c.url}</code></td>
                    <td className="row end">
                      <button onClick={() => copy(c.url)}>Copy</button>
                      {canWriteTags && <button onClick={() => write(c.url)}>Write tag</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {editing && <EditMovie movie={editing} onClose={() => setEditing(null)} onSaved={() => setPosters((n) => n + 1)} />}
    </section>
  );
}
