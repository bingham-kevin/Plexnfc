import { useMemo, useState } from "react";
import { posterURL, type Overview } from "./api";
import { useFilters, useMovies } from "./hooks";

/** The printable movie book. It opens in a new tab at the tag address, to print from the browser. */
export function Book({ overview }: { overview: Overview }) {
  const [title, setTitle] = useState("Movie Night");
  const [collection, setCollection] = useState("");
  const [layout, setLayout] = useState("full");
  const [paper, setPaper] = useState("letter");
  const [qr, setQr] = useState(true);
  const [withCards, setWithCards] = useState(false);
  const [source, setSource] = useState("");
  const filters = useFilters();
  const query = new URLSearchParams({ title, collection, source, layout, page_size: paper, qr: qr ? "1" : "0",
                                      cards: withCards ? "1" : "0" });
  const url = `${overview.tag_address}/book?${query}`;
  const reprintURL = (only: Record<string, string>) => `${url}&${new URLSearchParams(only)}`;

  return (
    <section className="card">
      <h2>Make the book</h2>
      <div className="book">
        <form className="fields" onSubmit={(e) => { e.preventDefault(); open(url, "_blank"); }}>
          <label>Title <input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
          <label>Collection <input value={collection} placeholder="All movies" list="book-collections"
                                   onChange={(e) => setCollection(e.target.value)} /></label>
          <datalist id="book-collections">
            {filters.data?.collections.map((c) => <option key={c.title} value={c.title} />)}
          </datalist>
          {!!filters.data?.sources.length && (
            <label>From
              <select value={source} onChange={(e) => setSource(e.target.value)}>
                <option value="">Every source</option>
                {filters.data.sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </label>
          )}
          <label>Layout
            <select value={layout} onChange={(e) => setLayout(e.target.value)}>
              <option value="full">1 poster per page</option>
              <option value="grid">4 posters per page</option>
            </select>
          </label>
          <label>Paper
            <select value={paper} onChange={(e) => setPaper(e.target.value)}>
              <option value="letter">Letter</option>
              <option value="A4">A4</option>
              <option value="legal">Legal</option>
              <option value="A5">A5</option>
            </select>
          </label>
          <label className="check"><input type="checkbox" checked={qr} onChange={(e) => setQr(e.target.checked)} />
            QR code on each page, for phones without NFC</label>
          <label className="check"><input type="checkbox" checked={withCards}
                                          onChange={(e) => setWithCards(e.target.checked)} />
            Only movies with a card linked</label>
          <button className="primary">Open printable book</button>
        </form>
        <div className={`preview ${layout}`} aria-hidden>
          <div className="sheet">
            {Array.from({ length: layout === "grid" ? 4 : 1 }, (_, i) => (
              <div key={i} className="slot">{qr && <span className="qr" />}</div>
            ))}
          </div>
          <p className="muted">{layout === "grid" ? "4 per page" : "1 per page"} · {paper}</p>
        </div>
      </div>
      <p className="muted">Wait for all the posters to load, then print at 100% scale with background graphics on. The
        book sets its own margins.</p>
      <Reprint collection={collection} source={source} open={(only) => open(reprintURL(only), "_blank")} />
    </section>
  );
}

/** Reprint one page of the book (a damaged one): the page a movie is on, or a page by its number,
 * exactly as in the book made with the settings above. */
function Reprint({ collection, source, open }: {
  collection: string; source: string; open: (only: Record<string, string>) => void;
}) {
  const [by, setBy] = useState<"movie" | "page">("movie");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState("");
  const movies = useMovies(collection, "", source);
  const matches = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? (movies.data?.movies ?? []).filter((m) => `${m.title} ${m.year ?? ""}`.toLowerCase().includes(q)).slice(0, 8) : [];
  }, [movies.data, search]);

  return (
    <div className="reprint">
      <h3>Reprint a page</h3>
      <p className="muted">For a damaged page: it comes out exactly as in the book, with the same page number (and, at 4 per
        page, the same movies beside it), as long as the settings above are the ones the book was made with.</p>
      <div className="segmented" role="radiogroup" aria-label="Reprint by">
        {(["movie", "page"] as const).map((b) => (
          <button key={b} type="button" role="radio" aria-checked={by === b} className={by === b ? "on" : ""}
                  onClick={() => setBy(b)}>{b === "movie" ? "A movie's page" : "Page number"}</button>
        ))}
      </div>
      {by === "movie" ? (
        <>
          <input type="search" placeholder="Find the movie" value={search} onChange={(e) => setSearch(e.target.value)}
                 aria-label="Find the movie whose page to reprint" />
          <div className="matches">
            {matches.map((m) => (
              <button key={m.key} onClick={() => open({ movie: m.key })} title="Open its page to print">
                <img className="thumb" src={posterURL(m.key, 80, 120)} alt="" loading="lazy" />
                <span>{m.title} <span className="muted">{m.year ?? ""}</span></span>
              </button>
            ))}
          </div>
        </>
      ) : (
        <form className="row" onSubmit={(e) => { e.preventDefault(); open({ page }); }}>
          <input value={page} onChange={(e) => setPage(e.target.value.replace(/\D/g, ""))} inputMode="numeric" required
                 placeholder="7" aria-label="Page number" size={5} />
          <button className="primary">Open page</button>
          <span className="muted">The number at the foot of the page; 1 is the cover.</span>
        </form>
      )}
    </div>
  );
}
