// The server's control panel API (/panel/api in plexnfc/server.py). Addresses are relative,
// so the panel works from whatever address it was opened at.

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

/** Something in Music Assistant a card can play: an album, playlist, artist, track or radio station */
export interface MusicItem {
  /** source|kind|ref, for music (and audiobooks) linked from the Music and Books pages */
  id?: string;
  uri: string;
  media_type: string;
  name: string;
  artist: string;
  image: string | null;
}

export interface Card {
  uid: string;
  key: string;
  title: string;
  linked: number;
  /** A music card: what it plays (``id`` if linked on the Music tab), and on which speaker */
  music?: MusicItem & { id?: string };
  speaker?: string;
  speaker_name?: string;
}

export interface ScanMovie {
  key: string;
  title: string;
  year?: number | null;
  music?: MusicItem;
  speaker_name?: string;
}

/** How music cards reach Music Assistant: directly, or through the Marquee integration in
 * Home Assistant */
export interface MusicStatus {
  connected: boolean;
  music_assistant: boolean;
  /** Which way music goes now */
  via: "music_assistant" | "home_assistant" | null;
  /** Speakers by Music Assistant player ID */
  speakers: { id: string; name: string }[];
  /** The direct connection's settings (the token itself is never sent) */
  assistant: { url: string; token: boolean; connected: boolean; message: string | null };
  home_assistant: boolean;
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

/** Something in the music library, from any source */
export interface LibItem {
  /** source|kind|ref */
  id: string;
  source: string;
  /** Audiobooks (source ``abs``): book, author or series; their tracks are files */
  kind: "artist" | "album" | "playlist" | "track" | "radio" | "book" | "author" | "series" | "file";
  name: string;
  /** The artist, or a book's author */
  artist: string;
  /** The album, or a book's series */
  album: string;
  image: string | null;
  duration: number | null;
  number: number | null;
  year?: number | null;
  narrator?: string;
  subtitle?: string;
  description?: string;
  /** How many books an author or a series has */
  count?: number | null;
  /** A book's file: where it starts in the book */
  offset?: number;
}

export interface Chapter { title: string; start: number; end: number }

/** An opened item: an artist's albums (an author's or series' books), an album's tracks, a book's files */
export interface OpenedItem {
  item: LibItem;
  albums?: LibItem[];
  tracks?: LibItem[];
  chapters?: Chapter[];
  /** Where you are in a book, from Audiobookshelf */
  progress?: { position: number; finished: boolean } | null;
}

export interface BooksState {
  audiobookshelf: { configured: boolean; url: string; user: string | null; message: string | null;
                    libraries: { id: string; name: string }[] };
  sources: { id: string; name: string; kind: string }[];
  speakers: { airplay: boolean; cast: boolean; music_assistant: boolean };
}

export interface MusicSources {
  sources: { id: string; name: string; kind: string; search_only?: boolean }[];
  folders: { id: string; name: string; path: string; tracks: number; scanning: boolean }[];
  speakers: { airplay: boolean; cast: boolean; music_assistant: boolean };
}

export interface Speaker {
  /** ma:…, airplay:… or cast:… */
  id: string;
  name: string;
  kind: "music_assistant" | "airplay" | "cast";
  group: boolean;
  volume: number | null;
  state: string | null;
  members: string[];
  can_group: string[];
  synced_to: string | null;
  limited?: boolean;
  /** AirPlay: "needed" until it's paired with the PIN it shows, then "paired" ("linked" when
   *  paired in the Apple TVs section instead, which is good for music too) */
  pairing?: "needed" | "paired" | "linked" | null;
}

/** Music playing on one speaker, or a group of them */
export interface Zone {
  id: string;
  kind: "music_assistant" | "marquee";
  speakers: string[];
  names: string[];
  state: "playing" | "paused";
  can_pause: boolean;
  restarts_on_resume?: boolean;
  track: { id?: string; name: string; artist: string; album: string; image: string | null; duration: number | null };
  position: number | null;
  index?: number;
  error?: string | null;
  /** An audiobook: where it's got to in the whole book */
  book?: { id: string; position: number | null; duration: number } | null;
}
