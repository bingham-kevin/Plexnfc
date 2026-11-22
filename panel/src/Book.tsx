import { useState } from "react";
import type { Overview } from "./api";
import { useFilters } from "./hooks";

/** The printable movie book. It opens in a new tab at the tag address, since Home Assistant's
 * sidebar won't serve a page opened outside it. */
export function Book({ overview }: { overview: Overview }) {
  const [title, setTitle] = useState("Movie Night");
  const [collection, setCollection] = useState("");
  const [layout, setLayout] = useState("full");
  const [paper, setPaper] = useState("letter");
  const [qr, setQr] = useState(true);
  const [source, setSource] = useState("");
  const filters = useFilters();
  const query = new URLSearchParams({ title, collection, source, layout, page_size: paper, qr: qr ? "1" : "0" });
  const url = `${overview.tag_address}/book?${query}`;

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
    </section>
  );
}
