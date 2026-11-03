import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { post, type FoundAppleTV, type Overview } from "./api";
import { useAction } from "./hooks";
import { useToast } from "./toast";

/** Apple TVs this server switches on and opens Marquee on itself, with no Home Assistant script. */
export function AppleTVs({ overview }: { overview: Overview }) {
  const toast = useToast();
  const scan = useMutation({
    mutationFn: () => post<{ found: FoundAppleTV[] }>("appletvs/scan"),
    onError: (e) => toast(e.message, "error"),
  });
  if (!overview.apple_tvs.available) return null;
  const marquees = overview.devices.filter((d) => d.kind === "tv").map((d) => d.name);

  return (
    <section className="card">
      <h2>Open Marquee automatically</h2>
      <p className="muted">Pair an Apple TV here and a tap switches it on and opens Marquee.</p>
      {overview.apple_tvs.linked.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Marquee player</th><th>Apple TV</th><th /></tr></thead>
            <tbody>
              {overview.apple_tvs.linked.map((tv) => (
                <LinkedRow key={tv.player} player={tv.player} name={tv.name} address={tv.address}
                           canManage={overview.can_manage} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {overview.can_manage && (
        <>
          <button className="primary" disabled={scan.isPending} onClick={() => scan.mutate()}>
            {scan.isPending ? "Looking…" : "Find Apple TVs"}
          </button>
          {scan.data && (
            scan.data.found.length === 0
              ? <p className="muted">No Apple TVs found. Are they on, and on this network?</p>
              : (
                <div className="found">
                  <datalist id="marquee-players">{marquees.map((m) => <option key={m} value={m} />)}</datalist>
                  {scan.data.found.map((tv) => <Found key={tv.identifier} tv={tv} onPaired={() => scan.reset()} />)}
                </div>
              )
          )}
        </>
      )}
    </section>
  );
}

function LinkedRow({ player, name, address, canManage }: {
  player: string; name: string; address: string; canManage: boolean;
}) {
  const open = useAction<void>(() => "appletvs/open", () => ({ player }), () => `Opened Marquee on ${name}`);
  const connect = useAction<void>(() => "appletvs/connect", () => ({ player }), (_, r) => r.message);
  const unpair = useAction<void>(() => "appletvs/remove", () => ({ player }), () => `Unpaired ${name}`);
  return (
    <tr>
      <td>{player}</td>
      <td>{name} <span className="muted">{address}</span></td>
      <td className="row end">
        {canManage && (
          <>
            <button disabled={open.isPending} onClick={() => open.mutate()}>Open Marquee</button>
            <button disabled={connect.isPending} onClick={() => connect.mutate()}
                    title="Open Marquee there and have it pair with this server">Connect Marquee</button>
            <button className="danger" disabled={unpair.isPending}
                    onClick={() => confirm(`Unpair ${name}?`) && unpair.mutate()}>Unpair</button>
          </>
        )}
      </td>
    </tr>
  );
}

/** An Apple TV found on the network: name the Marquee player it runs, pair, then type the PIN from the TV. */
function Found({ tv, onPaired }: { tv: FoundAppleTV; onPaired: () => void }) {
  const toast = useToast();
  const [player, setPlayer] = useState(tv.linked ?? tv.name);
  const [pairId, setPairId] = useState<string | null>(null);
  const [pin, setPin] = useState("");
  const start = useAction<void>(() => "appletvs/pair", () => ({ identifier: tv.identifier, player }));
  const finish = useAction<void>(() => `appletvs/pair/${pairId}`, () => ({ pin }), (_, r) => r.message);

  return (
    <div className="found-tv">
      <div>
        <b>{tv.name}</b> <span className="muted">{tv.address}</span>
      </div>
      {pairId ? (
        <form className="row" onSubmit={(e) => {
          e.preventDefault();
          finish.mutate(undefined, { onSuccess: () => { setPairId(null); onPaired(); } });
        }}>
          <label>PIN on the TV <input value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))}
                                      inputMode="numeric" autoComplete="one-time-code" maxLength={4} autoFocus
                                      required className="pin" /></label>
          <button className="primary" disabled={finish.isPending || pin.length !== 4}>Pair</button>
          <button type="button" onClick={() => setPairId(null)}>Cancel</button>
        </form>
      ) : (
        <form className="row" onSubmit={(e) => {
          e.preventDefault();
          start.mutate(undefined, {
            onSuccess: (r) => { setPairId(r.pair_id as string); toast("The TV is showing a PIN"); },
          });
        }}>
          <input value={player} onChange={(e) => setPlayer(e.target.value)} list="marquee-players" required
                 maxLength={40} aria-label="Marquee player" />
          <button disabled={start.isPending}>{start.isPending ? "Asking…" : tv.linked ? "Pair again" : "Pair"}</button>
        </form>
      )}
    </div>
  );
}
