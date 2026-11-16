// The server's control panel API (/panel/api in plexnfc/server.py). Addresses are relative,
// so they work under Home Assistant's ingress path too.

export type Kind = "tv" | "remote" | "phone" | "homeassistant";

export interface PlayerStatus {
  state: "playing" | "paused";
  title?: string;
  key?: string;
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
  /** count: how many movies Plex has in it */
  collections: { title: string; count: number | null }[];
  labels: string[];
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

export const posterURL = (key: string, w: number, h: number) => `poster/${encodeURIComponent(key)}?w=${w}&h=${h}`;
