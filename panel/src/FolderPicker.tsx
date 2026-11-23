import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { get, type Browse } from "./api";
import { Dialog } from "./Dialog";

/** Choose a folder on the server: start at a mounted share, a disk or home, and click down to it. */
export function FolderPicker({ start, onPick, onClose }:
  { start: string; onPick: (path: string) => void; onClose: () => void }) {
  const [path, setPath] = useState(start);
  const browse = useQuery({
    queryKey: ["browse", path],
    queryFn: () => get<Browse & { ok: boolean }>(`browse${path ? `?path=${encodeURIComponent(path)}` : ""}`),
    retry: false,
  });
  const data = browse.data;
  const join = (dir: string) => `${path.replace(/\/$/, "")}/${dir}`;

  return (
    <Dialog title="Choose a folder" onClose={onClose}>
      <div className="row wrap roots">
        {(data?.roots ?? []).map((r) => (
          <button key={r.path} className={path === r.path ? "on" : ""} onClick={() => setPath(r.path)}
                  title={r.path}>
            {r.network ? "🌐" : r.name === "Home" ? "🏠" : "💽"} {r.name}
          </button>
        ))}
      </div>
      {data && !data.roots.length && <p className="muted">No shares or disks are mounted on the server.</p>}
      {path && (
        <p className="row wrap">
          <button className="link" disabled={!data?.parent} onClick={() => data?.parent && setPath(data.parent)}
                  aria-label="Up a folder">⬆︎ Up</button>
          <code className="url">{path}</code>
        </p>
      )}
      {browse.isPending && <p className="muted">Opening…</p>}
      {browse.isError && <p className="err">{browse.error.message}</p>}
      {data && path && (
        <>
          <div className="pick-list folders">
            {data.dirs.map((d) => (
              <button key={d} className="pick" onClick={() => setPath(join(d))}>📁 {d}</button>
            ))}
            {!data.dirs.length && <p className="muted pad">No folders in here.</p>}
          </div>
          <div className="row end dialog-foot">
            <span className="muted">{data.videos ? `${data.videos} movie file${data.videos === 1 ? "" : "s"} right in this folder` : ""}</span>
            <button onClick={onClose}>Cancel</button>
            <button className="primary" onClick={() => onPick(path)}>Use this folder</button>
          </div>
        </>
      )}
      {!path && <p className="muted">Choose where to start. A network share appears here once it's mounted on the
        server (on a Mac, connect to it in the Finder with Go › Connect to Server).</p>}
    </Dialog>
  );
}
