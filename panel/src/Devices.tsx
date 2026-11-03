import { useState } from "react";
import type { Device, Kind, Overview, PairingRequest } from "./api";
import { useAction } from "./hooks";

export const KIND_NAMES: Record<Kind, string> = {
  tv: "Marquee",
  phone: "Marquee Remote",
  homeassistant: "Home Assistant",
};

/** Devices waiting for their code to be approved. */
export function PairingRequests({ overview }: { overview: Overview }) {
  if (overview.pairing_requests.length === 0) return null;
  return (
    <section className="card attention">
      <h2>Waiting to pair</h2>
      <p className="muted">Check the code matches the one on the device, name it (Marquee players after their room or
        owner), then approve.</p>
      {overview.pairing_requests.map((r) => <Request key={r.pair_id} request={r} canManage={overview.can_manage} />)}
    </section>
  );
}

function Request({ request, canManage }: { request: PairingRequest; canManage: boolean }) {
  const [name, setName] = useState(request.name);
  const approve = useAction<void>(() => `pairings/${request.pair_id}/approve`, () => ({ name }),
                                  () => `Paired ${name || request.name}`);
  return (
    <form className="request" onSubmit={(e) => { e.preventDefault(); approve.mutate(); }}>
      <span className="code" aria-label="Code">{request.code}</span>
      <span className="kind">{KIND_NAMES[request.kind]}</span>
      <input value={name} onChange={(e) => setName(e.target.value)} maxLength={40} aria-label="Name"
             disabled={!canManage} />
      {canManage && <button className="primary" disabled={approve.isPending}>Approve</button>}
    </form>
  );
}

function describe(device: Device): string {
  if (device.kind === "phone") return `Taps go to ${device.player ?? "the default player"}`;
  if (device.kind === "homeassistant") return "Media players and triggers in Home Assistant";
  if (device.status) return `${device.status.state === "paused" ? "Paused" : "Playing"}: ${device.status.title}`;
  return device.player ? `Plays on ${device.player}` : "Marquee player";
}

/** Everything paired with this server. */
export function Devices({ overview }: { overview: Overview }) {
  const order = Object.keys(KIND_NAMES);
  const devices = [...overview.devices].sort((a, b) =>
    order.indexOf(a.kind) - order.indexOf(b.kind) || a.name.localeCompare(b.name));
  return (
    <section className="card">
      <h2>Paired</h2>
      <p className="muted">In Marquee (Apple TV, iPhone, iPad), Marquee Remote or Home Assistant's Marquee integration,
        connect to <code>{overview.tag_address.split("://")[1]}</code> and approve the code it shows. Tags pick a
        Marquee player with <code>?client=app:NAME</code>.</p>
      {devices.length === 0 ? (
        <p className="muted">Nothing paired yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Name</th><th>App</th><th>Status</th><th>Paired</th><th /></tr>
            </thead>
            <tbody>
              {devices.map((d) => <DeviceRow key={d.id} device={d} canManage={overview.can_manage} />)}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function DeviceRow({ device, canManage }: { device: Device; canManage: boolean }) {
  const [name, setName] = useState(device.name);
  const rename = useAction<void>(() => `devices/${device.id}/rename`, () => ({ name }),
    () => device.kind === "tv"
      ? `Renamed. If a setting or tag names app:${device.name}, change it to the new name too.`
      : "Renamed");
  const remove = useAction<void>(() => `devices/${device.id}/remove`, undefined, () => `Removed ${device.name}`);
  const changed = name.trim() !== device.name && name.trim() !== "";

  return (
    <tr>
      <td>
        {canManage ? (
          <form className="row" onSubmit={(e) => { e.preventDefault(); rename.mutate(); }}>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={40} aria-label="Name" />
            {changed && <button disabled={rename.isPending}>Save</button>}
          </form>
        ) : device.name}
      </td>
      <td><span className={`kind ${device.kind}`}>{KIND_NAMES[device.kind]}</span></td>
      <td className="muted">{describe(device)}</td>
      <td className="muted">{new Date(device.paired * 1000).toLocaleDateString()}</td>
      <td>
        {canManage && (
          <button className="danger" disabled={remove.isPending}
                  onClick={() => confirm(`Remove ${device.name}? It will need pairing again.`) && remove.mutate()}>
            Remove
          </button>
        )}
      </td>
    </tr>
  );
}
