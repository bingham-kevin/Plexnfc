// The server's control panel API (/panel/api in plexnfc/server.py). Addresses are relative,
// so they work under Home Assistant's ingress path too.

export type Kind = "tv" | "remote" | "phone" | "homeassistant";

export interface PlayerStatus {
  state: "playing" | "paused";
  title?: string;
  key?: string;
  /** The source it's playing from ("Plex", or a Jellyfin server's or folder's name), if this server started it */
  source?: string | null;
  position?: number;
  duration?: number;
}

export interface Device {
  id: string;
  name: string;
  kind: Kind;
  paired: number;
  player: string | null;
  status: PlayerStatus | null;
}

export interface PairingRequest {
  pair_id: string;
  name: string;
  kind: Kind;
  code: string;
}

export interface LinkedAppleTV {
  player: string;
  name: string;
  address: string;
  identifier: string;
}

export interface Overview {
  can_manage: boolean;
  tag_address: string;
  tag_address_is_local: boolean;
  default_player: string;
  default_player_label: string;
  off_tag: boolean;
  plex: { signed_in: boolean; signed_in_here: boolean };
  pairing_requests: PairingRequest[];
  devices: Device[];
  apple_tvs: { available: boolean; linked: LinkedAppleTV[] };
}

export interface Movie {
  key: string;
  title: string;
  year: number | null;
  url: string;
  /** IDs of the cards linked to it */
  cards: string[];
  /** Where it comes from: "Plex", or Jellyfin servers' or folders' names, joined */
  source: string;
  /** Every source that has it, in the order they're used */
  sources: string[];
  /** Its files in folder sources, which can be matched with TMDB or edited */
  files: { id: string; source: string; matched: string }[];
}

/** A folder movie's details, from its file (and .nfo) and the changes made here. */
export interface FolderMovie {
  id: string;
  key: string;
  source: string;
  /** Its file, inside the folder source */
  path: string;
  title: string;
  year: number | null;
  summary: string;
  collections: string[];
  labels: string[];
  ids: Record<string, string>;
  /** "manual" or "auto" if matched with TMDB */
  matched: string;
  edited: boolean;
  /** What its file and .nfo say */
  file: { title?: string; year?: number | null };
}

export interface TmdbResult {
  id: number;
  title: string;
  year: number | null;
  summary: string;
  poster: string | null;
}

export interface Browse {
  path: string;
  parent: string | null;
  dirs: string[];
  /** Movie files right in this folder */
  videos: number;
  roots: { name: string; path: string; network: boolean }[];
}

export interface MatchJob {
  running: boolean;
  done: number;
  total: number;
  matched: number;
  error: string | null;
}

export interface Card {
  uid: string;
  key: string;
  title: string;
  linked: number;
}

export interface ScanMovie {
  key: string;
  title: string;
  year?: number | null;
}

export interface Scan {
  index: number;
  total: number;
  linked: number;
  current: ScanMovie | null;
  next: ScanMovie | null;
  done: boolean;
  last: { uid: string; title: string; key: string; moved_from: string | null; at: number } | null;
}

export interface CardsState {
  cards: Card[];
  scan: Scan | null;
  /** The last card read that isn't linked to a movie */
  unknown: { uid: string; at: number } | null;
}

export interface Filters {
  /** count: how many movies it holds */
  collections: { title: string; count: number | null }[];
  labels: string[];
  /** The places movies come from, when there's more than one ("plex", or a source's ID) */
  sources: { id: string; name: string }[];
}

export interface Source {
  id: string;
  kind: "jellyfin" | "folder";
  name: string;
  /** Why its movies couldn't be read last time, if they couldn't */
  error: string | null;
  /** How many movies it has, once read */
  count: number | null;
  url?: string;
  library?: string;
  /** The Jellyfin user signed in as, if not an API key */
  user?: string | null;
  path?: string;
}

export interface PlexSource {
  id: "plex";
  kind: "plex";
  enabled: boolean;
  signed_in: boolean;
  library: string;
}

export interface Sources {
  plex: PlexSource;
  sources: Source[];
  /** Source IDs ("plex" for Plex) in the order movies play from them */
  order: string[];
  ordered: (PlexSource | Source)[];
  /** Whether a TMDB API key is set, for matching folder movies */
  tmdb: boolean;
  /** "Match all" progress, by folder source ID */
  matching: Record<string, MatchJob>;
}

export interface Movies {
  movies: Movie[];
  controls: { action: string; url: string }[];
}

export interface Player {
  id: string;
  name: string;
  kind: string;
}

export interface FoundAppleTV {
  identifier: string;
  name: string;
  address: string;
  linked: string | null;
}

export class ApiError extends Error {}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`panel/api/${path}`, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(body.message || `The server answered ${response.status}`);
  return body as T;
}

export const get = <T>(path: string) => call<T>(path);

export const post = <T = { ok: boolean; message?: string; [field: string]: unknown }>(path: string, body: object = {}) =>
  call<T>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const posterURL = (key: string, w: number, h: number, version = "") =>
  `poster/${encodeURIComponent(key)}?w=${w}&h=${h}${version ? `&v=${encodeURIComponent(version)}` : ""}`;
