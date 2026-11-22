import { useEffect, useState } from "react";
import { AppleTVs } from "./AppleTVs";
import { Book } from "./Book";
import { Cards } from "./Cards";
import { Devices, PairingRequests } from "./Devices";
import { useAction, useOverview } from "./hooks";
import { NowPlaying } from "./NowPlaying";
import { Sources } from "./Sources";
import { Tags } from "./Tags";

const TABS = [
  { id: "home", label: "Now Playing" },
  { id: "devices", label: "Devices" },
  { id: "tags", label: "Tags" },
  { id: "cards", label: "Cards" },
  { id: "book", label: "Book" },
  { id: "sources", label: "Sources" },
] as const;
type Tab = (typeof TABS)[number]["id"];

/** The tab in the address (#devices), so a reload or a bookmark comes back to it. */
function useTab(): [Tab, (t: Tab) => void] {
  const read = () => (TABS.find((t) => `#${t.id}` === location.hash)?.id ?? "home") as Tab;
  const [tab, setTab] = useState<Tab>(read);
  useEffect(() => {
    const onHash = () => setTab(read());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  return [tab, (t) => (location.hash = t)];
}

export default function App() {
  const [tab, setTab] = useTab();
  const overview = useOverview();
  const data = overview.data;
  const waiting = data?.pairing_requests.length ?? 0;

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
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? "on" : ""}
                  onClick={() => setTab(t.id)}>
            {t.label}
            {t.id === "devices" && waiting > 0 && <span className="badge">{waiting}</span>}
          </button>
        ))}
      </nav>

      {overview.isPending && <p className="muted">Loading…</p>}
      {overview.isError && !data && <p className="err">Can't reach the server: {overview.error.message}</p>}

      {data && (
        <main>
          {!data.can_manage && (
            <p className="note">You can look around from here. To pair devices or change anything, open the control panel
              on the server itself, or in Home Assistant.</p>
          )}
          {/* Pairing requests are waiting on someone, so they show on every tab */}
          {tab !== "devices" && <PairingRequests overview={data} />}
          {tab === "home" && (
            <>
              <Status />
              <NowPlaying overview={data} />
            </>
          )}
          {tab === "devices" && (
            <>
              <PairingRequests overview={data} />
              <Devices overview={data} />
              <AppleTVs overview={data} />
            </>
          )}
          {tab === "tags" && <Tags overview={data} />}
          {tab === "cards" && <Cards overview={data} />}
          {tab === "book" && <Book overview={data} />}
          {tab === "sources" && <Sources overview={data} />}
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
        <dt>Plex <a className="muted" href="#sources" title="Plex, Jellyfin and folders">· sources</a></dt>
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
