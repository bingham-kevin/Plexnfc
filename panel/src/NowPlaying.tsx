import { useEffect, useState } from "react";
import { posterURL, type Device, type Overview } from "./api";
import { useAction } from "./hooks";

const clock = (s: number) => {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};

/** Each Marquee player, with what it's playing and its controls. */
export function NowPlaying({ overview }: { overview: Overview }) {
  const players = overview.devices.filter((d) => d.kind === "tv");
  return (
    <section className="card">
      <h2>Now playing</h2>
      {players.length === 0 ? (
        <p className="muted">No Marquee players yet. Pair one under Devices.</p>
      ) : (
        <div className="players">
          {players.map((p) => <PlayerCard key={p.id} device={p} canControl={overview.can_manage} />)}
        </div>
      )}
    </section>
  );
}

function PlayerCard({ device, canControl }: { device: Device; canControl: boolean }) {
  const status = device.status;
  const control = useAction<string>(() => "control", (action) => ({ player: `app:${device.name}`, action }));
  const position = useTicking(status);

  return (
    <article className={`player ${status ? status.state : "idle"}`}>
      {status?.key ? (
        <img className="poster" src={posterURL(status.key, 200, 300)} alt="" loading="lazy" />
      ) : (
        <div className="poster empty" aria-hidden>🎞️</div>
      )}
      <div className="player-body">
        <h3>{device.name}</h3>
        {status ? (
          <>
            <p className="title">{status.title || status.key}</p>
            <p className="muted">{status.state === "paused" ? "Paused" : "Playing"}</p>
            {!!status.duration && (
              <div className="progress" aria-label={`${clock(position)} of ${clock(status.duration)}`}>
                <div style={{ width: `${Math.min(100, (position / status.duration) * 100)}%` }} />
                <span>{clock(position)} / {clock(status.duration)}</span>
              </div>
            )}
            {canControl && (
              <div className="row">
                {status.state === "playing" ? (
                  <button disabled={control.isPending} onClick={() => control.mutate("pause")}>⏸ Pause</button>
                ) : (
                  <button disabled={control.isPending} onClick={() => control.mutate("play")}>▶ Play</button>
                )}
                <button disabled={control.isPending} onClick={() => control.mutate("stop")}>⏹ Stop</button>
              </div>
            )}
          </>
        ) : (
          <p className="muted">Nothing playing, or Marquee is closed</p>
        )}
      </div>
    </article>
  );
}

/** Marquee reports every few seconds; between reports a playing movie moves on by itself. */
function useTicking(status: Device["status"]) {
  const [now, setNow] = useState(Date.now());
  const [since, setSince] = useState({ at: Date.now(), position: status?.position ?? 0 });
  useEffect(() => setSince({ at: Date.now(), position: status?.position ?? 0 }), [status?.position, status?.key]);
  useEffect(() => {
    if (status?.state !== "playing") return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [status?.state]);
  if (!status) return 0;
  const moved = status.state === "playing" ? (now - since.at) / 1000 : 0;
  return Math.min(since.position + Math.max(0, moved), status.duration || Infinity);
}
