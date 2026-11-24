import { useEffect, useState } from "react";
import { AppleTVs } from "./AppleTVs";
import { Book } from "./Book";
import { BooksLibraryPage, BooksSourcesPage } from "./Books";
import { Cards } from "./Cards";
import { Devices, PairingRequests } from "./Devices";
import { Readers } from "./Readers";
import { MediaBook } from "./MediaBook";
import { MusicLibraryPage, MusicSourcesPage } from "./Music";
import { useAction, useOverview } from "./hooks";
import { NowPlaying } from "./NowPlaying";
import { Sources } from "./Sources";
import { Tags } from "./Tags";

/** Movies, Music and Books each have the same pages: Library, Cards, Book and Sources. */
const SECTIONS = [
  { id: "home", label: "Now Playing" },
  { id: "movies", label: "Movies" },
  { id: "music", label: "Music" },
  { id: "books", label: "Books" },
  { id: "devices", label: "Devices" },
] as const;
type Section = (typeof SECTIONS)[number]["id"];
const PAGES = [
  { id: "library", label: "Library" },
  { id: "cards", label: "Cards" },
  { id: "book", label: "Book" },
  { id: "sources", label: "Sources" },
] as const;
type Page = (typeof PAGES)[number]["id"];
type Place = { section: Section; page: Page };

// Addresses from before the sections, so bookmarks still land somewhere sensible
const OLD: Record<string, string> = {
  tags: "movies/library", cards: "movies/cards", book: "movies/book", sources: "movies/sources", music: "music/library",
};

/** Where the panel is, in the address (#music/cards), so a reload or a bookmark comes back to it. */
function usePlace(): [Place, (section: Section, page?: Page) => void] {
  const read = (): Place => {
    const raw = location.hash.slice(1);
    const [section, page] = (OLD[raw] ?? raw).split("/");
    const s = SECTIONS.find((x) => x.id === section)?.id ?? "home";
    return { section: s, page: PAGES.find((x) => x.id === page)?.id ?? "library" };
  };
  const [place, setPlace] = useState<Place>(read);
  useEffect(() => {
    const onHash = () => setPlace(read());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  return [place, (section, page = "library") =>
    (location.hash = section === "home" || section === "devices" ? section : `${section}/${page}`)];
}

export default function App() {
  const [{ section, page }, go] = usePlace();
  const overview = useOverview();
  const data = overview.data;
  const waiting = data?.pairing_requests.length ?? 0;
  const media = section === "movies" || section === "music" || section === "books";

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden>🎬</span>
          <div>
            <h1>Marquee</h1>
            <p className="muted">Control panel</p>
          </div>
        </div>
        <Connection failed={overview.isError} />
      </header>

      <nav className="tabs" role="tablist">
        {SECTIONS.map((t) => (
          <button key={t.id} role="tab" aria-selected={section === t.id} className={section === t.id ? "on" : ""}
                  onClick={() => go(t.id, page)}>
            {t.label}
            {t.id === "devices" && waiting > 0 && <span className="badge">{waiting}</span>}
          </button>
        ))}
      </nav>
      {media && (
        <nav className="subtabs" role="tablist" aria-label={`${section} pages`}>
          {PAGES.map((p) => (
            <button key={p.id} role="tab" aria-selected={page === p.id} className={page === p.id ? "on" : ""}
                    onClick={() => go(section, p.id)}>{p.label}</button>
          ))}
        </nav>
      )}

      {overview.isPending && <p className="muted">Loading…</p>}
      {overview.isError && !data && <p className="err">Can't reach the server: {overview.error.message}</p>}

      {data && (
        <main>
          {!data.can_manage && (
            <p className="note">You can look around from here. To pair devices or change anything, open the control panel
              on the server itself, or in Home Assistant.</p>
          )}
          {/* Pairing requests are waiting on someone, so they show everywhere */}
          {section !== "devices" && <PairingRequests overview={data} />}
          {section === "home" && (
            <>
              <Status />
              <NowPlaying overview={data} />
            </>
          )}
          {section === "devices" && (
            <>
              <PairingRequests overview={data} />
              <Devices overview={data} />
              <Readers overview={data} />
              <AppleTVs overview={data} />
            </>
          )}
          {section === "movies" && (
            <>
              {page === "library" && <Tags overview={data} />}
              {page === "cards" && <Cards overview={data} section="movies" />}
              {page === "book" && <Book overview={data} />}
              {page === "sources" && <Sources overview={data} />}
            </>
          )}
          {section === "music" && (
            <>
              {page === "library" && <MusicLibraryPage overview={data} />}
              {page === "cards" && <Cards overview={data} section="music" />}
              {page === "book" && <MediaBook overview={data} kind="music" />}
              {page === "sources" && <MusicSourcesPage overview={data} />}
            </>
          )}
          {section === "books" && (
            <>
              {page === "library" && <BooksLibraryPage overview={data} />}
              {page === "cards" && <Cards overview={data} section="books" />}
              {page === "book" && <MediaBook overview={data} kind="books" />}
              {page === "sources" && <BooksSourcesPage overview={data} />}
            </>
          )}
        </main>
      )}
    </div>
  );
}

function Connection({ failed }: { failed: boolean }) {
  return (
    <span className={`pill ${failed ? "bad" : "good"}`} title={failed ? "The server isn't answering" : "Up to date"}>
      <span className="dot" /> {failed ? "Reconnecting…" : "Live"}
    </span>
  );
}

/** Plex sign-in, where tags point and where they play. */
function Status() {
  const { data } = useOverview();
  const signOut = useAction<void>(() => "plex/logout", undefined, () => "Signed out of Plex");
  if (!data) return null;
  return (
    <section className="card">
      <h2>Server</h2>
      <dl className="facts">
        <dt>Plex <a className="muted" href="#movies/sources" title="Plex, Jellyfin and folders">· sources</a></dt>
        <dd>
          {data.plex.signed_in ? (
            <>
              <span className="pill good"><span className="dot" /> Signed in</span>{" "}
              {data.plex.signed_in_here && data.can_manage && (
                <button className="link" disabled={signOut.isPending}
                        onClick={() => confirm("Sign out of Plex? Tags stop working until you sign in again.") && signOut.mutate()}>
                  Sign out
                </button>
              )}
            </>
          ) : (
            <>
              <span className="pill bad"><span className="dot" /> Not signed in</span>{" "}
              <a className="button primary" href="plex/login">Sign in with Plex</a>
            </>
          )}
        </dd>
        <dt>Tags point at</dt>
        <dd>
          <code>{data.tag_address}</code>
          {data.tag_address_is_local && (
            <p className="err">That's this machine's own name for itself, which phones can't reach. Set the tag address to this
              machine's network address, e.g. http://192.168.1.20:8080.</p>
          )}
        </dd>
        <dt>Taps play on</dt>
        <dd>{data.default_player_label || <span className="err">No player chosen yet</span>}</dd>
      </dl>
    </section>
  );
}
