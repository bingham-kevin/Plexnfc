import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { get, type LibItem, type Overview } from "./api";
import { useMusicSources } from "./Music";

/** The printable book of albums or audiobooks, in the movie book's style. It opens in a new
 * tab at the tag address, like the movie book. */
export function MediaBook({ overview, kind }: { overview: Overview; kind: "music" | "books" }) {
  const books = kind === "books";
  const sources = useMusicSources();
  const [title, setTitle] = useState(books ? "Story Time" : "Music");
  const [source, setSource] = useState("");  // music: "" is every album with a card
  const [what, setWhat] = useState(books ? "" : "album");  // music: album or playlist; books: "", author:ID, series:ID
  const [layout, setLayout] = useState("grid");
  const [paper, setPaper] = useState("letter");
  const [qr, setQr] = useState(true);
  const [withCards, setWithCards] = useState(true);
  const authors = useQuery({
    queryKey: ["music-items", "abs", "author", "", 500],
    queryFn: () => get<{ items: LibItem[] }>("music/browse?source=abs&kind=author&limit=500"),
    enabled: books, retry: false,
  });
  const series = useQuery({
    queryKey: ["music-items", "abs", "series", "", 500],
    queryFn: () => get<{ items: LibItem[] }>("music/browse?source=abs&kind=series&limit=500"),
    enabled: books, retry: false,
  });
  const ref = (i: LibItem) => i.id.split("|")[2];
  const cardsOnly = !books && !source ? true : withCards;
  const query = new URLSearchParams({ title, layout, page_size: paper, qr: qr ? "1" : "0", cards: cardsOnly ? "1" : "0",
                                      what, ...(books ? {} : { source }) });
  const url = `${overview.tag_address}/book/${kind}?${query}`;

  return (
    <section className="card">
      <h2>Make the book</h2>
      <div className="book">
        <form className="fields" onSubmit={(e) => { e.preventDefault(); open(url, "_blank"); }}>
          <label>Title <input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
          {books ? (
            <label>Books
              <select value={what} onChange={(e) => setWhat(e.target.value)}>
                <option value="">Every audiobook</option>
                {!!authors.data?.items.length && (
                  <optgroup label="By author">
                    {authors.data.items.map((a) => <option key={a.id} value={`author:${ref(a)}`}>{a.name}</option>)}
                  </optgroup>
                )}
                {!!series.data?.items.length && (
                  <optgroup label="A series">
                    {series.data.items.map((s) => <option key={s.id} value={`series:${ref(s)}`}>{s.name}</option>)}
                  </optgroup>
                )}
              </select>
            </label>
          ) : (
            <>
              <label>From
                <select value={source} onChange={(e) => setSource(e.target.value)}>
                  <option value="">Everything with a card</option>
                  {sources.data?.sources.filter((s) => !s.search_only).map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
              </label>
              {source && (
                <label>Pages for
                  <select value={what} onChange={(e) => setWhat(e.target.value)}>
                    <option value="album">Albums</option>
                    <option value="playlist">Playlists</option>
                  </select>
                </label>
              )}
            </>
          )}
          <label>Layout
            <select value={layout} onChange={(e) => setLayout(e.target.value)}>
              <option value="full">1 cover per page</option>
              <option value="grid">4 covers per page</option>
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
            QR code for {books ? "books" : "music"} with a card, for phones</label>
          {(books || source) && (
            <label className="check"><input type="checkbox" checked={withCards}
                                            onChange={(e) => setWithCards(e.target.checked)} />
              Only {books ? "books" : "ones"} with a card linked</label>
          )}
          <button className="primary">Open printable book</button>
        </form>
        <div className="muted">
          <p>Each page has the cover and a gold circle where its card goes. Hold the circle on a Movie Remote and it
            plays on the card's speaker{books ? ", picking up where you left off" : ""}. Link cards under{" "}
            <a href={`#${kind}/cards`}>{books ? "Books" : "Music"} › Cards</a> first.</p>
          <p>The QR code plays the card from a phone without NFC. Print with <b>Background graphics</b> on.</p>
        </div>
      </div>
    </section>
  );
}
